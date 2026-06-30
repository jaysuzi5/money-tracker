import os

import psycopg2
from django.core.management.base import BaseCommand, CommandError

from tracker.models import NetWorthSnapshot


class Command(BaseCommand):
    help = 'Import net worth history from the homelab-hub Postgres (financial_networth).'

    def add_arguments(self, parser):
        parser.add_argument('--dsn', default=os.environ.get('HOMELAB_DB_URL'),
                            help='Postgres DSN for homelab-hub (or set HOMELAB_DB_URL).')

    def handle(self, *args, **opts):
        dsn = opts['dsn']
        if not dsn:
            raise CommandError('Provide --dsn or set HOMELAB_DB_URL.')
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        cur.execute('SELECT date, net_worth, comments FROM financial_networth ORDER BY date')
        rows = cur.fetchall()
        conn.close()

        created = updated = 0
        for d, nw, comments in rows:
            _, was_created = NetWorthSnapshot.objects.update_or_create(
                snapshot_date=d,
                defaults={'net_worth': nw, 'notes': (comments or '')[:300]})
            created += was_created
            updated += not was_created
        self.stdout.write(self.style.SUCCESS(
            f'Imported {len(rows)} rows ({created} new, {updated} updated).'))
