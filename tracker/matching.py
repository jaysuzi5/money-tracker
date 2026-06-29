"""Reconciliation matcher.

Pairs imported bank transactions (source=simplefin, status=cleared) against
manually-entered receipts (source=manual, status=uncleared). On a confident
match the manual entry is kept (preserving its category/memo), promoted to
CLEARED, stamped with the bank's external_id, and the duplicate import is
deleted so balances aren't double-counted.
"""
import logging
import re
from datetime import timedelta
from difflib import SequenceMatcher

from .models import Transaction, TxnSource, TxnStatus

logger = logging.getLogger('tracker')

DATE_WINDOW_DAYS = 4
PAYEE_SIM_MIN = 0.45      # below this, don't auto-pick among multiple candidates


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9 ]', '', (s or '').lower()).strip()


def _payee_sim(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def find_candidates(imported: Transaction):
    """Manual uncleared txns in the same account with equal amount, within the
    date window, not already matched."""
    lo = imported.date - timedelta(days=DATE_WINDOW_DAYS)
    hi = imported.date + timedelta(days=DATE_WINDOW_DAYS)
    return list(
        Transaction.objects.filter(
            account=imported.account,
            source=TxnSource.MANUAL,
            status=TxnStatus.UNCLEARED,
            amount=imported.amount,
            date__gte=lo, date__lte=hi,
        )
    )


def _merge(manual: Transaction, imported: Transaction):
    ext_id, imp_payee, imp_memo = imported.external_id, imported.payee, imported.memo
    imported.delete()                      # free the unique (account, external_id) slot first
    manual.external_id = ext_id
    manual.status = TxnStatus.CLEARED
    if not manual.payee:
        manual.payee = imp_payee
    if imp_memo and imp_memo not in manual.memo:
        manual.memo = (manual.memo + ' | ' + imp_memo).strip(' |')[:400]
    manual.matched_to = None
    manual.is_new = True
    manual.save()


def run_matcher(account=None) -> dict:
    """Auto-match imported txns to manual receipts. Returns counts dict."""
    qs = Transaction.objects.filter(
        source=TxnSource.SIMPLEFIN, status=TxnStatus.CLEARED, external_id__gt='')
    if account is not None:
        qs = qs.filter(account=account)

    matched, ambiguous = 0, 0
    for imported in qs:
        candidates = find_candidates(imported)
        if not candidates:
            continue
        if len(candidates) == 1:
            _merge(candidates[0], imported)
            matched += 1
            continue
        # multiple equal-amount candidates: pick best payee match if clear enough
        scored = sorted(
            ((_payee_sim(imported.payee, c.payee), c) for c in candidates),
            key=lambda t: t[0], reverse=True)
        best_score, best = scored[0]
        second = scored[1][0]
        if best_score >= PAYEE_SIM_MIN and best_score > second:
            _merge(best, imported)
            matched += 1
        else:
            ambiguous += 1   # leave for manual review queue

    if matched or ambiguous:
        logger.info('matcher run', extra={'ctx_matched': matched, 'ctx_ambiguous': ambiguous})
    return {'matched': matched, 'ambiguous': ambiguous}
