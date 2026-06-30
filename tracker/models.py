from decimal import Decimal

from django.db import models
from django.db.models import Sum
from django.utils import timezone

from . import crypto


class ConnectorType(models.TextChoices):
    SIMPLEFIN = 'simplefin', 'SimpleFIN Bridge'
    PLAID = 'plaid', 'Plaid'
    PLAYWRIGHT = 'playwright', 'Browser scraper'
    MANUAL = 'manual', 'Manual only'


class AccountType(models.TextChoices):
    CHECKING = 'checking', 'Checking'
    SAVINGS = 'savings', 'Savings'
    MONEY_MARKET = 'money_market', 'Money Market'
    CASH = 'cash', 'Cash / HYSA'
    CREDIT_CARD = 'credit_card', 'Credit Card'
    BROKERAGE = 'brokerage', 'Brokerage'
    K401 = '401k', '401(k)'
    IRA = 'ira', 'IRA'
    HSA = 'hsa', 'HSA'
    PENSION = 'pension', 'Pension'
    PROPERTY = 'property', 'Property'
    OTHER = 'other', 'Other'


class TaxTreatment(models.TextChoices):
    PRE_TAX = 'pre_tax', 'Pre-Tax'
    ROTH = 'roth', 'Roth'
    PENSION = 'pension', 'Pension'
    TAXABLE = 'taxable', 'Taxable'
    HSA = 'hsa', 'HSA'
    CASH = 'cash', 'Cash'


class TxnStatus(models.TextChoices):
    UNCLEARED = 'uncleared', 'Uncleared'
    CLEARED = 'cleared', 'Cleared'
    RECONCILED = 'reconciled', 'Reconciled'


class TxnSource(models.TextChoices):
    MANUAL = 'manual', 'Manual entry'
    SIMPLEFIN = 'imported_simplefin', 'Imported (SimpleFIN)'
    QIF = 'imported_qif', 'Imported (QIF history)'
    CSV = 'imported_csv', 'Imported (CSV history)'
    BUCKET = 'bucket_transfer', 'Bucket transfer'


class Institution(models.Model):
    name = models.CharField(max_length=120, unique=True)
    connector_type = models.CharField(
        max_length=20, choices=ConnectorType.choices, default=ConnectorType.SIMPLEFIN
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class ConnectionCredential(models.Model):
    """Encrypted secret for a connector. SimpleFIN: one row holding the access URL
    (institution null = account-wide). Scrapers: per-institution login secrets."""
    institution = models.ForeignKey(
        Institution, null=True, blank=True, on_delete=models.CASCADE, related_name='credentials'
    )
    connector_type = models.CharField(max_length=20, choices=ConnectorType.choices)
    encrypted_secret = models.TextField(blank=True)
    status = models.CharField(max_length=40, default='ok')
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def set_secret(self, plaintext: str):
        self.encrypted_secret = crypto.encrypt(plaintext)

    def get_secret(self) -> str:
        return crypto.decrypt(self.encrypted_secret) if self.encrypted_secret else ''

    def __str__(self):
        return f'{self.get_connector_type_display()} ({self.institution or "account-wide"})'


class Account(models.Model):
    institution = models.ForeignKey(Institution, on_delete=models.CASCADE, related_name='accounts')
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=20, choices=AccountType.choices)
    external_id = models.CharField(max_length=120, blank=True, db_index=True)
    current_balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))  # available (online − allocated)
    online_balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))  # bank-reported
    balance_synced_at = models.DateTimeField(null=True, blank=True)
    is_reconcilable = models.BooleanField(default=False)
    is_investment = models.BooleanField(default=False)
    is_manual = models.BooleanField(default=False)  # online_balance maintained by hand; sync won't overwrite
    manual_balance = models.BooleanField(default=False)  # current_balance set by hand; not recomputed
    is_active = models.BooleanField(default=True)
    in_portfolio = models.BooleanField(default=False)  # include in the Portfolio view + snapshots
    tax_treatment = models.CharField(max_length=10, choices=TaxTreatment.choices, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['institution__name', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['institution', 'external_id'],
                condition=~models.Q(external_id=''),
                name='uniq_account_external_id',
            )
        ]

    def __str__(self):
        return f'{self.institution.name} — {self.name}'

    @property
    def cleared_balance(self):
        agg = self.transactions.filter(
            status__in=[TxnStatus.CLEARED, TxnStatus.RECONCILED]
        ).aggregate(total=Sum('amount'))
        return agg['total'] or Decimal('0')

    @property
    def allocated_total(self):
        return sum((b.balance for b in self.buckets.all()), Decimal('0'))

    @property
    def unallocated(self):
        return self.current_balance - self.allocated_total


class Bucket(models.Model):
    """Virtual envelope inside a real Account (allowances, sinking funds).
    Balance is the running sum of its entries (money moved in/out)."""
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='buckets')
    name = models.CharField(max_length=120)
    target_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['account', 'name']
        unique_together = [('account', 'name')]

    def __str__(self):
        return f'{self.account.name} / {self.name}'

    @property
    def balance(self):
        return self.entries.aggregate(t=Sum('amount'))['t'] or Decimal('0')


class BucketEntry(models.Model):
    """A move of money into (+) or out of (-) a bucket. No category — these are
    temporary earmarks, not real spending."""
    bucket = models.ForeignKey(Bucket, on_delete=models.CASCADE, related_name='entries')
    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # signed: + in, - out
    memo = models.CharField(max_length=400, blank=True)
    # mirrored Transfer transaction in the parent account (keeps available balance honest)
    txn = models.ForeignKey('Transaction', null=True, blank=True,
                            on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-id']

    def __str__(self):
        return f'{self.bucket.name} {self.date} {self.amount}'


class Category(models.Model):
    name = models.CharField(max_length=80)
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.CASCADE, related_name='children'
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['parent__name', 'name']
        unique_together = [('parent', 'name')]
        verbose_name_plural = 'categories'

    def __str__(self):
        return f'{self.parent.name} : {self.name}' if self.parent else self.name


class Payee(models.Model):
    name = models.CharField(max_length=160, unique=True)
    default_category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL, related_name='payees'
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Transaction(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='transactions')
    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # signed: negative = outflow
    payee = models.CharField(max_length=200, blank=True)
    memo = models.CharField(max_length=400, blank=True)
    category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL, related_name='transactions'
    )
    bucket = models.ForeignKey(
        Bucket, null=True, blank=True, on_delete=models.SET_NULL, related_name='transactions'
    )
    status = models.CharField(max_length=12, choices=TxnStatus.choices, default=TxnStatus.UNCLEARED)
    source = models.CharField(max_length=20, choices=TxnSource.choices, default=TxnSource.MANUAL)
    external_id = models.CharField(max_length=160, blank=True, db_index=True)
    transfer = models.ForeignKey(
        'Transfer', null=True, blank=True, on_delete=models.SET_NULL, related_name='legs'
    )
    matched_to = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='matches'
    )
    is_new = models.BooleanField(default=False)  # surfaced by sync, cleared on review
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-id']
        indexes = [models.Index(fields=['account', 'date'])]
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'external_id'],
                condition=~models.Q(external_id=''),
                name='uniq_txn_external_id',
            )
        ]

    def __str__(self):
        return f'{self.date} {self.payee} {self.amount}'

    @property
    def is_split(self):
        return self.splits.exists()


class TransactionSplit(models.Model):
    """One leg of a split transaction (e.g. Walmart receipt = groceries + sundries,
    or a paycheck split across taxes/withholdings)."""
    transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name='splits')
    category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL, related_name='splits')
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # signed
    memo = models.CharField(max_length=400, blank=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.category or "uncategorized"}: {self.amount}'


def recompute_balance(account):
    """Balance = online (posted, from the bank) + in-flight (uncleared, signed) − set-aside.
    Works for all account types: credit-card charges are negative (raise owed), payments
    positive; processing/pending items stay uncleared until they post.
    Accounts with manual_balance keep their hand-set current_balance."""
    if account.manual_balance:
        return account.current_balance
    inflight = account.transactions.filter(
        status=TxnStatus.UNCLEARED).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    bal = (account.online_balance or Decimal('0')) + inflight - account.allocated_total
    Account.objects.filter(pk=account.pk).update(current_balance=bal)
    account.current_balance = bal
    return bal


class PropertyEntry(models.Model):
    """A valuation change for a Property account (home, vehicle). Signed: + increase,
    - decrease; the account's value is the running sum of its entries."""
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name='property_entries')
    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    memo = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-id']

    def __str__(self):
        return f'{self.account.name} {self.date} {self.amount}'


class Transfer(models.Model):
    """Links the two legs of a money move between accounts."""
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Holding(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='holdings')
    symbol = models.CharField(max_length=40)
    description = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=6, default=Decimal('0'))
    price = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal('0'))
    market_value = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal('0'))
    as_of = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['account', 'symbol']

    def __str__(self):
        return f'{self.symbol} x{self.quantity}'


class BalanceSnapshot(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='snapshots')
    date = models.DateField()
    balance = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ['-date']
        unique_together = [('account', 'date')]

    def __str__(self):
        return f'{self.account.name} {self.date} {self.balance}'


class PortfolioSnapshot(models.Model):
    """Curated point-in-time balance per portfolio account (monthly / on demand).
    Separate from the daily BalanceSnapshot so portfolio history is intentional."""
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name='portfolio_snapshots')
    snapshot_date = models.DateField()
    balance = models.DecimalField(max_digits=15, decimal_places=2)
    notes = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-snapshot_date', 'account__name']
        unique_together = [('account', 'snapshot_date')]

    def __str__(self):
        return f'{self.account.name} {self.snapshot_date} {self.balance}'


class NetWorthSnapshot(models.Model):
    """Point-in-time total net worth across all accounts. One row per date."""
    snapshot_date = models.DateField(unique=True)
    net_worth = models.DecimalField(max_digits=15, decimal_places=2)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-snapshot_date']

    def __str__(self):
        return f'{self.snapshot_date} {self.net_worth}'


class SyncRun(models.Model):
    institution = models.ForeignKey(
        Institution, null=True, blank=True, on_delete=models.SET_NULL, related_name='sync_runs'
    )
    connector_type = models.CharField(max_length=20, choices=ConnectorType.choices)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    accounts_synced = models.IntegerField(default=0)
    transactions_added = models.IntegerField(default=0)
    transactions_matched = models.IntegerField(default=0)
    ok = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.connector_type} {self.started_at:%Y-%m-%d %H:%M} ok={self.ok}'


class ReconciliationSession(models.Model):
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name='reconciliations'
    )
    statement_date = models.DateField()
    statement_balance = models.DecimalField(max_digits=14, decimal_places=2)
    cleared_balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-statement_date']

    @property
    def difference(self):
        return self.statement_balance - self.cleared_balance

    def __str__(self):
        return f'{self.account.name} reconcile {self.statement_date}'
