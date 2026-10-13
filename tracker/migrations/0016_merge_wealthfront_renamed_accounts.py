"""Wealthfront renamed both joint accounts and reissued their SimpleFIN ids, so sync.py
created brand-new duplicate accounts ("Joint (UENK)" and "Joint <rate>% APY") instead of
updating "Joint Investment Account" / "Joint Cash Account". tracker.sync now aliases the new
names so this can't recur; this merges the duplicates created before that fix shipped.
"""
import re
from decimal import Decimal

from django.db import migrations

_NAME_ALIASES = [
    (re.compile(r'^joint\s*\(uenk\)$', re.I), 'Joint Investment Account'),
    (re.compile(r'^joint\s+\d+(?:\.\d+)?\s*%\s*apy$', re.I), 'Joint Cash Account'),
]


def _canonical_name(name):
    for pattern, canonical in _NAME_ALIASES:
        if pattern.match(name):
            return canonical
    return name


def _recompute_current_balance(account, Transaction, BucketEntry):
    """Mirrors tracker.models.recompute_balance for a non-ledger ('other' type) account:
    current_balance = online_balance + uncleared txns - allocated (bucket) total."""
    if account.manual_balance:
        return
    inflight = sum((t.amount for t in Transaction.objects.filter(
        account=account, status='uncleared')), Decimal('0'))
    allocated = sum((e.amount for e in BucketEntry.objects.filter(bucket__account=account)),
                    Decimal('0'))
    account.current_balance = (account.online_balance or Decimal('0')) + inflight - allocated
    account.save(update_fields=['current_balance'])


def merge(apps, schema_editor):
    Account = apps.get_model('tracker', 'Account')
    Transaction = apps.get_model('tracker', 'Transaction')
    BalanceSnapshot = apps.get_model('tracker', 'BalanceSnapshot')
    Holding = apps.get_model('tracker', 'Holding')
    BucketEntry = apps.get_model('tracker', 'BucketEntry')

    pairs = []
    for dup in Account.objects.filter(institution__name='Wealthfront'):
        canon = _canonical_name(dup.name)
        if canon == dup.name:
            continue
        keep = Account.objects.filter(institution=dup.institution, name=canon).first()
        if keep:
            pairs.append((keep, dup))

    for keep, dup in pairs:
        seen = set(Transaction.objects.filter(account=keep).exclude(external_id='')
                   .values_list('external_id', flat=True))
        for t in Transaction.objects.filter(account=dup):
            if t.external_id and t.external_id in seen:
                t.delete()
                continue
            t.account = keep
            t.save(update_fields=['account'])

        keep_dates = set(BalanceSnapshot.objects.filter(account=keep)
                         .values_list('date', flat=True))
        for s in BalanceSnapshot.objects.filter(account=dup):
            if s.date in keep_dates:
                s.delete()
            else:
                s.account = keep
                s.save(update_fields=['account'])

        # Every sync wholly replaces an account's holdings, so the duplicate's set (just
        # synced under the new feed) is current; the keeper's is stale.
        if Holding.objects.filter(account=dup).exists():
            Holding.objects.filter(account=keep).delete()
            Holding.objects.filter(account=dup).update(account=keep)

        new_ext, new_bal, synced = dup.external_id, dup.online_balance, dup.balance_synced_at
        dup.delete()
        keep.external_id = new_ext
        if not keep.is_manual:
            keep.online_balance = new_bal if new_bal is not None else keep.online_balance
        keep.balance_synced_at = synced or keep.balance_synced_at
        keep.save()
        _recompute_current_balance(keep, Transaction, BucketEntry)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tracker', '0015_scheduledtransaction'),
    ]
    operations = [migrations.RunPython(merge, noop)]
