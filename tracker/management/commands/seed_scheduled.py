from decimal import Decimal

from django.core.management.base import BaseCommand

from tracker.models import (Account, Bucket, Category, ScheduledKind,
                            ScheduledTransaction)


class Command(BaseCommand):
    help = 'Seed the standard monthly scheduled transactions (idempotent by name).'

    def handle(self, *args, **opts):
        cash = Account.objects.get(name='Joint Cash Account')
        invest = Account.objects.get(name='Joint Investment Account')
        checking = Account.objects.get(name='Checking')

        def bkt(name, acct):
            return Bucket.objects.get(name=name, account=acct)

        def cat(parent, name):
            return Category.objects.get(parent__name=parent, name=name)

        order = 0
        rows = []

        def add(**kw):
            nonlocal order
            order += 1
            kw['order'] = order
            rows.append(kw)

        # Wealthfront Cash → holding buckets (day 20)
        for bname, amt in [('Vacation', 500), ('Propane', 180), ('Appliance', 300),
                           ('Insurance', 310), ('Tax: City', 64), ('Tax: County', 106),
                           ('Tax: School', 470), ('Lawn', 130), ('Christmas', 120)]:
            add(name=f'{bname} set-aside', kind=ScheduledKind.BUCKET, source_account=cash,
                amount=Decimal(amt), day=20, to_bucket=bkt(bname, cash))

        # Wealthfront Cash → account transfers (day 20)
        add(name='Cash → Investment', kind=ScheduledKind.TRANSFER, source_account=cash,
            amount=Decimal('500'), day=20, to_account=invest)
        add(name='Cash → Checking', kind=ScheduledKind.TRANSFER, source_account=cash,
            amount=Decimal('5666'), day=20, to_account=checking)

        # Checking → holding buckets (day 20)
        add(name='Jay set-aside', kind=ScheduledKind.BUCKET, source_account=checking,
            amount=Decimal('500'), day=20, to_bucket=bkt('Jay', checking))
        add(name='Suzanne set-aside', kind=ScheduledKind.BUCKET, source_account=checking,
            amount=Decimal('500'), day=20, to_bucket=bkt('Suzanne', checking))

        # Payments from Checking
        add(name='WageWorks', kind=ScheduledKind.PAYMENT, source_account=checking,
            amount=Decimal('2489.14'), day=1, month_offset=1, payee='WageWorks',
            category=cat('Insurance', 'Medical'))
        add(name='Moravian', kind=ScheduledKind.PAYMENT, source_account=checking,
            amount=Decimal('400'), day=1, month_offset=1, payee='Moravian',
            category=cat('Giving', 'Church'))
        add(name='West Coast Life', kind=ScheduledKind.PAYMENT, source_account=checking,
            amount=Decimal('75.25'), day=22, payee='West Coast Life',
            category=cat('Insurance', 'Life'))
        add(name='New York Life', kind=ScheduledKind.PAYMENT, source_account=checking,
            amount=Decimal('19.25'), day=15, month_offset=1, payee='New York Life',
            category=cat('Insurance', 'Life'))
        add(name='Astound', kind=ScheduledKind.PAYMENT, source_account=checking,
            amount=Decimal('46.92'), day=30, payee='Astound',
            category=cat('Utilities', 'Cable'))
        add(name='Pennsylvania-American Water Company', kind=ScheduledKind.PAYMENT,
            source_account=checking, amount=Decimal('60'), day=30,
            payee='Pennsylvania-American Water Company', category=cat('Utilities', 'Water'))
        add(name='Met-Ed', kind=ScheduledKind.PAYMENT, source_account=checking,
            amount=Decimal('100'), day=30, payee='Met-Ed',
            category=cat('Utilities', 'Electricity'))

        created = 0
        for kw in rows:
            _, was = ScheduledTransaction.objects.update_or_create(
                name=kw['name'], kind=kw['kind'], source_account=kw['source_account'],
                defaults=kw)
            created += was
        self.stdout.write(self.style.SUCCESS(
            f'Seeded {len(rows)} scheduled transactions ({created} new).'))
