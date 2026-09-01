import logging
import re
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .connectors.base import SyncResult
from .matching import run_matcher
from .models import (Account, AccountType, BalanceSnapshot, Holding, Institution,
                     SyncRun, Transaction, TxnSource, TxnStatus, is_ledger_account,
                     recompute_balance)

logger = logging.getLogger('tracker')

_TYPE_HINTS = [
    ('checking', AccountType.CHECKING),
    ('credit', AccountType.CREDIT_CARD),
    ('discover', AccountType.CREDIT_CARD),
    ('money market', AccountType.MONEY_MARKET),
    ('savings', AccountType.SAVINGS),
    ('cash', AccountType.CASH),
    ('401', AccountType.K401),
    ('roth', AccountType.IRA),
    ('ira', AccountType.IRA),
    ('hsa', AccountType.HSA),
    ('pension', AccountType.PENSION),
    ('brokerage', AccountType.BROKERAGE),
    ('invest', AccountType.BROKERAGE),
]
_INVESTMENT_TYPES = {AccountType.BROKERAGE, AccountType.K401, AccountType.IRA,
                     AccountType.HSA, AccountType.PENSION}

# The Discover card was reissued under Capital One and now syncs with org "Capital One"
# (account name "Discover More"). Map it back onto the existing "Discover Credit Card"
# institution so its transactions keep loading into the same Discover account.
_ORG_ALIASES = {
    'Capital One': 'Discover Credit Card',
}

# Deprecated SimpleFIN feeds to skip entirely (e.g. the old Discover connection after the
# card was reissued under Capital One). Its transactions already live on the Discover
# account; re-importing them just creates a duplicate ghost account.
_IGNORE_EXTERNAL_IDS = {
    'ACT-05811165-00ec-4c4c-95ac-7dadd83c6df9',  # old Discover feed -> use Capital One "Discover More"
}


# A value account's balance is the bank's number plus anything still in flight, so an
# uncleared row keeps adding on top of it. Accounts with no transaction feed (Wealthfront's
# investment account only reports holdings) never get an import for the matcher to merge, so
# a hand-entered transfer leg would sit uncleared forever and double-count. Once the bank's
# balance is dated this many days past the transaction, the money has landed and is in that
# number already.
_SETTLE_DAYS = 3


def _clear_settled(acct, balance_date) -> int:
    """Clear in-flight rows on a value account that the bank balance already reflects."""
    # is_manual/manual_balance accounts carry a hand-set number that the feed never touches,
    # so nothing there proves the money landed — leave their in-flight rows alone.
    if is_ledger_account(acct) or acct.manual_balance or acct.is_manual:
        return 0
    return Transaction.objects.filter(
        account=acct, status=TxnStatus.UNCLEARED,
        date__lte=balance_date - timedelta(days=_SETTLE_DAYS)).update(status=TxnStatus.CLEARED)


def _clean_name(name: str) -> str:
    """Strip a trailing account number like ' (0004)' from a SimpleFIN account name."""
    return re.sub(r'\s*\(\d+\)\s*$', '', name or '').strip()


def _guess_type(name: str) -> str:
    low = name.lower()
    for needle, atype in _TYPE_HINTS:
        if needle in low:
            return atype
    return AccountType.OTHER


def apply_result(result: SyncResult, *, connector_type: str, institution=None) -> SyncRun:
    run = SyncRun(connector_type=connector_type, institution=institution,
                  started_at=timezone.now())
    added = 0
    by_ext = {}
    bal_dates = {}

    for na in result.accounts:
        if na.external_id in _IGNORE_EXTERNAL_IDS:
            continue
        org = _ORG_ALIASES.get(na.org, na.org)
        inst, _ = Institution.objects.get_or_create(
            name=org, defaults={'connector_type': connector_type})
        clean = _clean_name(na.name)
        acct, created = Account.objects.get_or_create(
            institution=inst, external_id=na.external_id,
            defaults={'name': clean, 'type': _guess_type(clean)})
        if created:
            acct.is_investment = acct.type in _INVESTMENT_TYPES
            acct.is_reconcilable = acct.type in {AccountType.CHECKING, AccountType.CREDIT_CARD}
        if not acct.is_manual:  # manual accounts keep their hand-entered balance
            acct.online_balance = na.balance
        acct.balance_synced_at = timezone.now()
        acct.save()
        by_ext[na.external_id] = acct
        bal_dates[na.external_id] = na.balance_date
        BalanceSnapshot.objects.update_or_create(
            account=acct, date=na.balance_date, defaults={'balance': na.balance})

    for nh_acct_id in {h.account_ext_id for h in result.holdings}:
        if nh_acct_id in by_ext:
            Holding.objects.filter(account=by_ext[nh_acct_id]).delete()
    for nh in result.holdings:
        acct = by_ext.get(nh.account_ext_id)
        if not acct:
            continue
        Holding.objects.create(
            account=acct, symbol=nh.symbol, description=nh.description,
            quantity=nh.quantity, price=nh.price, market_value=nh.market_value)

    for nt in result.transactions:
        acct = by_ext.get(nt.account_ext_id)
        if not acct:
            continue
        if nt.amount == Decimal('0'):
            continue  # skip $0 placeholders (e.g. PSECU scheduled checks)
        # Skip if this transaction was already imported on the account by date + amount +
        # payee under a different bank id. Covers provider double-sends and a reissued
        # card's feed re-reporting transactions already recorded here. Only rows that
        # already carry a bank id count — a hand-entered twin (external_id '') must still
        # let the import through so run_matcher can merge and clear it.
        if Transaction.objects.filter(
                account=acct, date=nt.date, amount=nt.amount, payee=nt.payee,
                external_id__gt='').exclude(external_id=nt.external_id).exists():
            continue
        # Never import pending items. Posted items come in cleared; the online_balance
        # already reflects them, so no starting-balance adjustment is ever needed.
        # Pending charges are added by hand and stay uncleared until they post for real.
        if nt.pending:
            continue
        obj, created = Transaction.objects.get_or_create(
            account=acct, external_id=nt.external_id,
            defaults={'date': nt.date, 'amount': nt.amount, 'payee': nt.payee,
                      'memo': nt.memo, 'status': TxnStatus.CLEARED,
                      'source': TxnSource.SIMPLEFIN, 'is_new': True})
        if created:
            added += 1
            # Inherit the category last used for this same payee, so an imported txn is
            # pre-categorized even if it never matches a manual entry. If it does match,
            # merge() keeps the manual entry's category, so this only shows on unmatched rows.
            if nt.payee:
                prev_cat = (Transaction.objects.filter(payee=nt.payee, category__isnull=False)
                            .exclude(pk=obj.pk).order_by('-date', '-id')
                            .values_list('category_id', flat=True).first())
                if prev_cat:
                    obj.category_id = prev_cat
                    obj.save(update_fields=['category'])
        # existing rows keep their status (you control clearing of hand-entered in-flight items)

    matched = 0
    settled = 0
    for ext_id, acct in by_ext.items():
        if acct.is_reconcilable:
            matched += run_matcher(account=acct)['matched']
        settled += _clear_settled(acct, bal_dates[ext_id])
        recompute_balance(acct)

    run.accounts_synced = len(by_ext)
    run.transactions_added = added
    run.transactions_matched = matched
    run.ended_at = timezone.now()
    run.ok = not result.errors
    run.error = '\n'.join(result.errors)
    run.save()
    logger.info('sync applied', extra={'ctx_accounts': len(by_ext), 'ctx_added': added,
                                       'ctx_settled': settled})
    return run
