import logging
import re
from decimal import Decimal

from django.utils import timezone

from .connectors.base import SyncResult
from .matching import run_matcher
from .models import (Account, AccountType, BalanceSnapshot, Holding, Institution,
                     SyncRun, Transaction, TxnSource, TxnStatus, recompute_balance)

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

    for na in result.accounts:
        inst, _ = Institution.objects.get_or_create(
            name=na.org, defaults={'connector_type': connector_type})
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
        # skip provider double-sends: same content already imported under a different id
        if Transaction.objects.filter(
                account=acct, date=nt.date, amount=nt.amount, payee=nt.payee,
                source=TxnSource.SIMPLEFIN).exclude(external_id=nt.external_id).exists():
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
            # Fidelity: inherit the category last used for this same payee
            if nt.payee and 'fidelity' in (acct.institution.name or '').lower():
                prev_cat = (Transaction.objects.filter(payee=nt.payee, category__isnull=False)
                            .exclude(pk=obj.pk).order_by('-date', '-id')
                            .values_list('category_id', flat=True).first())
                if prev_cat:
                    obj.category_id = prev_cat
                    obj.save(update_fields=['category'])
        # existing rows keep their status (you control clearing of hand-entered in-flight items)

    matched = 0
    for acct in by_ext.values():
        if acct.is_reconcilable:
            matched += run_matcher(account=acct)['matched']
        recompute_balance(acct)

    run.accounts_synced = len(by_ext)
    run.transactions_added = added
    run.transactions_matched = matched
    run.ended_at = timezone.now()
    run.ok = not result.errors
    run.error = '\n'.join(result.errors)
    run.save()
    logger.info('sync applied', extra={'ctx_accounts': len(by_ext), 'ctx_added': added})
    return run
