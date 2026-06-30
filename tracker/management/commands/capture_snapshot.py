from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from tracker.models import Account, PortfolioSnapshot


class Command(BaseCommand):
    help = 'Capture a portfolio snapshot (balance per in-portfolio account) for a date.'

    def add_arguments(self, parser):
        parser.add_argument('--date', default='', help='YYYY-MM-DD (default today).')

    def handle(self, *args, **opts):
        d = opts['date'] or timezone.now().date().isoformat()
        n = 0
        for a in Account.objects.filter(is_active=True, in_portfolio=True):
            PortfolioSnapshot.objects.update_or_create(
                account=a, snapshot_date=d,
                defaults={'balance': a.online_balance or Decimal('0'), 'notes': 'captured'})
            n += 1
        self.stdout.write(self.style.SUCCESS(f'Captured {n} portfolio snapshots for {d}.'))
