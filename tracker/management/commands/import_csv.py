"""Import a Quicken (Mac) register CSV export into an account.

Layout (after a few metadata lines):
  ,<S>,<Date>,<Payee>,<Category>,<Clr>,<Amount>,<Balance>,<Memo>
Split transactions appear as consecutive rows marked 'S' sharing date+payee;
their amounts sum to the real transaction total.
"""
import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tracker.models import (Account, Category, Transaction, TransactionSplit,
                            TxnSource, TxnStatus)

DATE_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')

# Map Quicken's top-level category names onto our existing seeded roots.
TOP_ALIAS = {
    'food & dining': 'Food',
    'bills & utilities': 'Utilities',
    'auto & transport': 'Auto',
    'health & fitness': 'Health',
    'personal income': 'Income',
    'gifts & donations': 'Giving',
    'household': 'Home',
    'personal care': 'Personal',
}
NO_CATEGORY = {'', 'uncategorized'}

CLR_STATUS = {'R': TxnStatus.RECONCILED, 'C': TxnStatus.CLEARED}


class _Rollback(Exception):
    pass


class Command(BaseCommand):
    help = 'Import a Quicken Mac CSV register export (handles splits + category mapping).'

    def add_arguments(self, parser):
        parser.add_argument('csv_file')
        parser.add_argument('--account-id', type=int,
                            help='Target Account id (default: PSECU Checking).')
        parser.add_argument('--dry-run', action='store_true')

    # ---- helpers ----
    def _amount(self, raw):
        return Decimal(raw.replace(',', '').replace('"', '').strip())

    def _date(self, raw):
        return datetime.strptime(raw.strip(), '%m/%d/%Y').date()

    def _category(self, raw, create):
        path = (raw or '').strip()
        if path.lower() in NO_CATEGORY or '[' in path:  # '[Account]' = transfer, no category
            return None
        segs = [s.strip() for s in path.split(':') if s.strip()]
        if not segs:
            return None
        segs[0] = TOP_ALIAS.get(segs[0].lower(), segs[0])
        parent = None
        leaf = None
        for name in segs:
            if parent is not None and name.lower() == parent.name.lower():
                continue  # skip redundant repeat of parent name
            existing = Category.objects.filter(parent=parent, name__iexact=name).first()
            if existing:
                leaf = existing
            elif create:
                leaf = Category.objects.create(parent=parent, name=name)
                self.created_cats += 1
            else:
                leaf = parent  # dry-run: stop descending at what exists
            parent = leaf
        return leaf

    def _parse_rows(self, path):
        """Yield dicts {split, date, payee, category, clr, amount, memo}."""
        with open(path, encoding='utf-8-sig', newline='') as fh:
            for row in csv.reader(fh):
                # locate the date cell
                di = next((i for i, c in enumerate(row) if DATE_RE.match(c.strip())), None)
                if di is None:
                    continue
                try:
                    amount = self._amount(row[di + 4])
                except (IndexError, InvalidOperation):
                    continue
                yield {
                    'split': (di >= 1 and row[di - 1].strip().upper() == 'S'),
                    'date': self._date(row[di]),
                    'payee': row[di + 1].strip() if di + 1 < len(row) else '',
                    'category': row[di + 2].strip() if di + 2 < len(row) else '',
                    'clr': row[di + 3].strip() if di + 3 < len(row) else '',
                    'amount': amount,
                    'memo': row[di + 6].strip() if di + 6 < len(row) else '',
                }

    def handle(self, *args, **opts):
        path = Path(opts['csv_file'])
        if not path.exists():
            raise CommandError(f'File not found: {path}')

        if opts['account_id']:
            account = Account.objects.get(pk=opts['account_id'])
        else:
            account = (Account.objects.filter(institution__name__icontains='psecu',
                                              name__icontains='checking').first())
            if not account:
                raise CommandError('PSECU Checking not found; pass --account-id.')

        create = True  # creates run inside atomic(); dry-run rolls them back
        self.created_cats = 0
        stats = {'txns': 0, 'splits': 0, 'split_txns': 0, 'dup': 0}
        # dedup only against rows already in the DB (not rows we add this run, so
        # legitimately identical historical transactions are preserved)
        existing = set(Transaction.objects.filter(account=account)
                       .values_list('date', 'amount', 'payee'))

        rows = list(self._parse_rows(path))

        # group consecutive split rows sharing (date, payee)
        groups = []          # each: ('single', row) or ('split', [rows])
        i = 0
        while i < len(rows):
            r = rows[i]
            if r['split']:
                grp = [r]
                j = i + 1
                while j < len(rows) and rows[j]['split'] \
                        and rows[j]['date'] == r['date'] and rows[j]['payee'] == r['payee']:
                    grp.append(rows[j]); j += 1
                groups.append(('split', grp)); i = j
            else:
                groups.append(('single', r)); i += 1

        def exists(date, amount, payee):
            return (date, amount, payee) in existing

        try:
            with transaction.atomic():
                for kind, g in groups:
                    if kind == 'single':
                        if exists(g['date'], g['amount'], g['payee']):
                            stats['dup'] += 1
                            continue
                        Transaction.objects.create(
                            account=account, date=g['date'], amount=g['amount'],
                            payee=g['payee'], memo=g['memo'][:400],
                            category=self._category(g['category'], create),
                            status=CLR_STATUS.get(g['clr'], TxnStatus.UNCLEARED),
                            source=TxnSource.CSV)
                        stats['txns'] += 1
                    else:
                        total = sum((x['amount'] for x in g), Decimal('0'))
                        first = g[0]
                        if exists(first['date'], total, first['payee']):
                            stats['dup'] += 1
                            continue
                        txn = Transaction.objects.create(
                            account=account, date=first['date'], amount=total,
                            payee=first['payee'], memo=first['memo'][:400], category=None,
                            status=CLR_STATUS.get(first['clr'], TxnStatus.UNCLEARED),
                            source=TxnSource.CSV)
                        for x in g:
                            TransactionSplit.objects.create(
                                transaction=txn,
                                category=self._category(x['category'], create),
                                amount=x['amount'], memo=x['memo'][:400])
                            stats['splits'] += 1
                        stats['txns'] += 1
                        stats['split_txns'] += 1
                if opts['dry_run']:
                    raise _Rollback
        except _Rollback:
            pass

        if not opts['dry_run']:
            from tracker.models import recompute_balance
            recompute_balance(account)

        prefix = '[DRY RUN] ' if opts['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}account="{account.name}" rows={len(rows)} txns={stats["txns"]} '
            f'(split_txns={stats["split_txns"]}, split_lines={stats["splits"]}) '
            f'dup_skipped={stats["dup"]} categories_created={self.created_cats}'))
