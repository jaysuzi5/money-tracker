"""Reconciliation matcher.

Pairs imported bank transactions (source=simplefin) against hand-entered
transactions (any non-bank source) with the SAME amount to the penny. On a
confident payee match the manual entry is kept (preserving its category/memo),
promoted to CLEARED, stamped with the bank's external_id, and the duplicate
import is deleted so nothing is double-counted.

Confidence:
- amount must match to the penny AND be within the date window.
- payee similarity is boosted by learned PayeeAlias rows (bank name → local payee).
- >= AUTO_MATCH_SIM  → auto-merge.
- <  AUTO_MATCH_SIM  → left for the user to confirm on the Matches review page;
  confirming there records a PayeeAlias so it auto-matches next time.
"""
import logging
import re
from datetime import timedelta
from difflib import SequenceMatcher

from .models import PayeeAlias, Transaction, TxnSource, TxnStatus

logger = logging.getLogger('tracker')

DATE_WINDOW_DAYS = 5
AUTO_MATCH_SIM = 0.55     # >= this similarity (or an alias) auto-matches; below → confirm


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9 ]', '', (s or '').lower()).strip()


def _fuzzy(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _alias_hit(bank_name: str, local_payee: str, account) -> bool:
    nb, nl = _norm(bank_name), _norm(local_payee)
    for al in PayeeAlias.objects.filter(account__in=[account, None]):
        if _norm(al.bank_name) == nb and _norm(al.local_payee) == nl:
            return True
    return False


def payee_sim(imported: Transaction, candidate: Transaction) -> float:
    """1.0 if a learned alias links them, else fuzzy string similarity."""
    if _alias_hit(imported.payee, candidate.payee, imported.account):
        return 1.0
    return _fuzzy(imported.payee, candidate.payee)


def find_candidates(imported: Transaction):
    """Hand-entered txns in the same account, exact amount, within the date window,
    not already matched (external_id empty), and NOT reconciled. Reconciled transactions
    are locked — never matched, mutated, or deleted."""
    lo = imported.date - timedelta(days=DATE_WINDOW_DAYS)
    hi = imported.date + timedelta(days=DATE_WINDOW_DAYS)
    return list(
        Transaction.objects.filter(
            account=imported.account,
            amount=imported.amount,
            date__gte=lo, date__lte=hi,
            external_id='', exclude_match=False,
        ).exclude(source=TxnSource.SIMPLEFIN).exclude(status=TxnStatus.RECONCILED)
    )


def merge(manual: Transaction, imported: Transaction):
    """Keep the manual entry (its category/memo), fold in the bank's id, drop the import."""
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
    """Auto-match confident pairs. Ambiguous ones (exact amount, weak name) are left
    in place for the user to confirm on the Matches page."""
    qs = Transaction.objects.filter(
        source=TxnSource.SIMPLEFIN, status=TxnStatus.CLEARED, external_id__gt='')
    if account is not None:
        qs = qs.filter(account=account)

    matched, ambiguous = 0, 0
    for imported in qs:
        candidates = find_candidates(imported)
        if not candidates:
            continue
        scored = sorted(((payee_sim(imported, c), c) for c in candidates),
                        key=lambda t: t[0], reverse=True)
        best_score, best = scored[0]
        if best_score >= AUTO_MATCH_SIM:
            merge(best, imported)
            matched += 1
        else:
            ambiguous += 1   # surfaced for confirmation, not auto-merged

    if matched or ambiguous:
        logger.info('matcher run', extra={'ctx_matched': matched, 'ctx_ambiguous': ambiguous})
    return {'matched': matched, 'ambiguous': ambiguous}


def pending_suggestions(account=None):
    """Exact-amount pairs the matcher would NOT auto-merge (weak payee match), for the
    user to confirm. Returns [{imported, candidate, score}] best candidate per import."""
    qs = Transaction.objects.filter(
        source=TxnSource.SIMPLEFIN, status=TxnStatus.CLEARED, external_id__gt='')
    if account is not None:
        qs = qs.filter(account=account)
    out = []
    for imported in qs.select_related('account'):
        candidates = find_candidates(imported)
        if not candidates:
            continue
        scored = sorted(((payee_sim(imported, c), c) for c in candidates),
                        key=lambda t: t[0], reverse=True)
        best_score, best = scored[0]
        if best_score < AUTO_MATCH_SIM:
            out.append({'imported': imported, 'candidate': best, 'score': round(best_score, 2)})
    return out
