from decimal import Decimal

from django.db import migrations
from django.db.models import Sum


def backfill(apps, schema_editor):
    Account = apps.get_model('tracker', 'Account')
    Bucket = apps.get_model('tracker', 'Bucket')
    for a in Account.objects.all():
        if a.manual_balance:
            continue
        txn_sum = a.transactions.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        allocated = Bucket.objects.filter(account=a).aggregate(s=Sum('balance'))['s'] or Decimal('0')
        # choose opening so recompute (opening + txn_sum - allocated) == existing current_balance
        a.opening_balance = (a.current_balance or Decimal('0')) + allocated - txn_sum
        a.save(update_fields=['opening_balance'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tracker', '0012_account_opening_balance_payeealias'),
    ]
    operations = [migrations.RunPython(backfill, noop)]
