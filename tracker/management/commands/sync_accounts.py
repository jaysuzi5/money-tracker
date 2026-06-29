from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tracker.connectors.simplefin import SimpleFinConnector
from tracker.models import ConnectionCredential, ConnectorType
from tracker.sync import apply_result


class Command(BaseCommand):
    help = 'Pull accounts/transactions/balances from SimpleFIN and upsert them.'

    def add_arguments(self, parser):
        parser.add_argument('--since-days', type=int, default=30,
                            help='Fetch transactions from N days ago (default 30).')

    def _access_url(self) -> str:
        cred = (ConnectionCredential.objects
                .filter(connector_type=ConnectorType.SIMPLEFIN)
                .order_by('-id').first())
        if cred and cred.encrypted_secret:
            return cred.get_secret()
        if settings.SIMPLEFIN_ACCESS_URL:
            return settings.SIMPLEFIN_ACCESS_URL
        raise CommandError(
            'No SimpleFIN access URL. Run claim_simplefin or set SIMPLEFIN_ACCESS_URL.')

    def handle(self, *args, **opts):
        access_url = self._access_url()
        start = date.today() - timedelta(days=opts['since_days'])
        result = SimpleFinConnector().fetch(access_url, start_date=start)
        if result.errors:
            self.stderr.write(self.style.WARNING('SimpleFIN errors: ' + '; '.join(result.errors)))
        run = apply_result(result, connector_type=ConnectorType.SIMPLEFIN)
        self.stdout.write(self.style.SUCCESS(
            f'Synced {run.accounts_synced} accounts, added {run.transactions_added} transactions '
            f'(ok={run.ok}).'))
