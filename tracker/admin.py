from django.contrib import admin

from . import models


@admin.register(models.Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ('name', 'connector_type', 'is_active')
    list_filter = ('connector_type', 'is_active')


@admin.register(models.ConnectionCredential)
class ConnectionCredentialAdmin(admin.ModelAdmin):
    list_display = ('connector_type', 'institution', 'status', 'last_synced_at')
    list_filter = ('connector_type', 'status')
    exclude = ('encrypted_secret',)


@admin.register(models.Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'institution', 'type', 'online_balance', 'current_balance',
                    'is_reconcilable', 'is_investment', 'is_manual', 'is_active',
                    'balance_synced_at')
    list_filter = ('type', 'institution', 'is_reconcilable', 'is_manual', 'is_active')
    list_editable = ('is_manual',)
    search_fields = ('name', 'external_id')


@admin.register(models.Bucket)
class BucketAdmin(admin.ModelAdmin):
    list_display = ('name', 'account', 'balance', 'target_amount', 'is_active')
    list_filter = ('account', 'is_active')


@admin.register(models.BucketEntry)
class BucketEntryAdmin(admin.ModelAdmin):
    list_display = ('bucket', 'date', 'amount', 'memo')
    list_filter = ('bucket',)
    date_hierarchy = 'date'


@admin.register(models.Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'parent', 'is_active')
    list_filter = ('is_active', 'parent')
    search_fields = ('name',)


@admin.register(models.Payee)
class PayeeAdmin(admin.ModelAdmin):
    list_display = ('name', 'default_category')
    search_fields = ('name',)


@admin.register(models.Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('date', 'account', 'payee', 'amount', 'category',
                    'bucket', 'status', 'source')
    list_filter = ('status', 'source', 'account', 'category')
    search_fields = ('payee', 'memo', 'external_id')
    date_hierarchy = 'date'


@admin.register(models.Holding)
class HoldingAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'account', 'quantity', 'price', 'market_value', 'as_of')
    list_filter = ('account',)
    search_fields = ('symbol', 'description')


@admin.register(models.BalanceSnapshot)
class BalanceSnapshotAdmin(admin.ModelAdmin):
    list_display = ('account', 'date', 'balance')
    list_filter = ('account',)
    date_hierarchy = 'date'


@admin.register(models.SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ('started_at', 'connector_type', 'institution', 'accounts_synced',
                    'transactions_added', 'transactions_matched', 'ok')
    list_filter = ('connector_type', 'ok')


@admin.register(models.ReconciliationSession)
class ReconciliationSessionAdmin(admin.ModelAdmin):
    list_display = ('account', 'statement_date', 'statement_balance',
                    'cleared_balance', 'difference', 'completed')
    list_filter = ('account', 'completed')


admin.site.register(models.Transfer)
admin.site.site_header = 'Money Tracker'
admin.site.site_title = 'Money Tracker'
admin.site.index_title = 'Administration'
