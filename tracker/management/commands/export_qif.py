from pathlib import Path

from django.core.management.base import BaseCommand

from tracker.models import Account
from tracker.qif_export import build_qif


class Command(BaseCommand):
    help = 'Export all accounts to a QIF file (portable, Quicken-importable archive).'

    def add_arguments(self, parser):
        parser.add_argument('--output', default='-', help='Output path, or - for stdout.')

    def handle(self, *args, **opts):
        accounts = Account.objects.filter(is_active=True).select_related('institution')
        qif = build_qif(accounts)
        if opts['output'] == '-':
            self.stdout.write(qif)
        else:
            Path(opts['output']).write_text(qif)
            self.stdout.write(self.style.SUCCESS(
                f'Wrote {len(qif)} bytes for {accounts.count()} accounts to {opts["output"]}'))
