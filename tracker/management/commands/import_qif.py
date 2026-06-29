import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tracker import qif
from tracker.models import (Account, Category, Institution, Transaction,
                            TransactionSplit, TxnSource, TxnStatus)
from tracker.sync import _guess_type


class _Rollback(Exception):
    pass


def get_or_make_category(path: str, *, create: bool):
    """'Utilities:Electric' -> nested Category. Returns Category or None."""
    path = path.split('/')[0].strip()  # drop class tags after '/'
    if not path:
        return None
    parts = [p.strip() for p in path.split(':') if p.strip()]
    parent = None
    obj = None
    for name in parts:
        qs = Category.objects.filter(parent=parent, name=name)
        obj = qs.first()
        if obj is None:
            if not create:
                return None
            obj = Category.objects.create(parent=parent, name=name)
        parent = obj
    return obj


class Command(BaseCommand):
    help = 'Import historical transactions from a Quicken QIF export (idempotent, deduped).'

    def add_arguments(self, parser):
        parser.add_argument('qif_file')
        parser.add_argument('--institution', required=True,
                            help='Institution name to attach discovered accounts to.')
        parser.add_argument('--account-map', default='',
                            help='JSON file mapping QIF account name -> our Account name.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        path = Path(opts['qif_file'])
        if not path.exists():
            raise CommandError(f'File not found: {path}')
        text = path.read_text(encoding='utf-8', errors='replace')
        blocks, categories = qif.parse(text)

        acct_map = {}
        if opts['account_map']:
            acct_map = json.loads(Path(opts['account_map']).read_text())

        create = not opts['dry_run']
        stats = {'inserted': 0, 'skipped_dup': 0, 'no_date': 0,
                 'categories': 0, 'transfers': 0, 'accounts': 0, 'splits': 0}

        try:
            with transaction.atomic():
                inst, _ = Institution.objects.get_or_create(
                    name=opts['institution'], defaults={'connector_type': 'manual'})

                for cat in categories:
                    before = Category.objects.count()
                    get_or_make_category(cat, create=create)
                    if create and Category.objects.count() > before:
                        stats['categories'] += 1

                acct_cache = {}
                for block in blocks:
                    target_name = acct_map.get(block.name, block.name)
                    if target_name not in acct_cache:
                        acct, made = Account.objects.get_or_create(
                            institution=inst, name=target_name,
                            defaults={'type': _guess_type(target_name)})
                        acct_cache[target_name] = acct
                        if made:
                            stats['accounts'] += 1
                    account = acct_cache[target_name]

                    for t in block.transactions:
                        if t.date is None:
                            stats['no_date'] += 1
                            continue
                        if Transaction.objects.filter(
                                account=account, date=t.date, amount=t.amount,
                                payee=t.payee).exists():
                            stats['skipped_dup'] += 1
                            continue
                        category = (get_or_make_category(t.category, create=create)
                                    if t.category and not t.is_transfer else None)
                        if t.is_transfer:
                            stats['transfers'] += 1
                        # split transactions: category lives on the split lines
                        txn = Transaction.objects.create(
                            account=account, date=t.date, amount=t.amount,
                            payee=t.payee, memo=t.memo[:400],
                            category=None if t.splits else category,
                            status=TxnStatus.RECONCILED, source=TxnSource.QIF)
                        for s in t.splits:
                            TransactionSplit.objects.create(
                                transaction=txn,
                                category=get_or_make_category(s.category, create=create)
                                if s.category else None,
                                amount=s.amount, memo=s.memo[:400])
                            stats['splits'] += 1
                        stats['inserted'] += 1

                if opts['dry_run']:
                    raise _Rollback
        except _Rollback:
            pass

        if not opts['dry_run']:
            from tracker.models import Account, recompute_balance
            for acct in Account.objects.filter(institution=inst):
                recompute_balance(acct)

        prefix = '[DRY RUN] ' if opts['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}inserted={stats["inserted"]} splits={stats["splits"]} '
            f'skipped_dup={stats["skipped_dup"]} no_date={stats["no_date"]} '
            f'new_accounts={stats["accounts"]} new_categories={stats["categories"]} '
            f'transfers_flagged={stats["transfers"]}'))
