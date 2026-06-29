from decimal import Decimal

from django import forms

from .models import Account, Bucket, Category, Transaction


def _bootstrap(fields):
    for f in fields.values():
        w = f.widget
        cls = 'form-select' if isinstance(w, forms.Select) else 'form-control'
        if isinstance(w, forms.CheckboxInput):
            cls = 'form-check-input'
        w.attrs['class'] = (w.attrs.get('class', '') + ' ' + cls).strip()


class ReceiptForm(forms.ModelForm):
    DIRECTION = [('out', 'Expense (money out)'), ('in', 'Income (money in)')]
    direction = forms.ChoiceField(choices=DIRECTION, initial='out')
    amount = forms.DecimalField(min_value=Decimal('0.01'), max_digits=14, decimal_places=2)

    class Meta:
        model = Transaction
        fields = ['account', 'date', 'amount', 'direction', 'payee',
                  'category', 'bucket', 'memo']
        widgets = {'date': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = Account.objects.filter(is_active=True)
        self.fields['category'].queryset = Category.objects.filter(is_active=True)
        self.fields['bucket'].queryset = Bucket.objects.filter(is_active=True)
        self.fields['bucket'].required = False
        self.fields['category'].required = False
        _bootstrap(self.fields)
        self.fields['category'].widget.attrs['class'] += ' tsearch'

    def save(self, commit=True):
        obj = super().save(commit=False)
        amt = self.cleaned_data['amount']
        obj.amount = -amt if self.cleaned_data['direction'] == 'out' else amt
        obj.source = 'manual'
        if not obj.status:
            obj.status = 'uncleared'
        if commit:
            obj.save()
        return obj


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ['name', 'type', 'is_reconcilable', 'is_investment',
                  'is_manual', 'manual_balance', 'is_active',
                  'online_balance', 'current_balance']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['is_manual'].label = 'Manual online balance (sync won\'t overwrite)'
        self.fields['online_balance'].label = 'Online balance (used when manual online)'
        self.fields['manual_balance'].label = 'Manual current balance (not recomputed)'
        self.fields['current_balance'].label = 'Current balance (used when manual current)'
        _bootstrap(self.fields)


class TxnEditForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = ['date', 'payee', 'category', 'bucket', 'memo']
        widgets = {'date': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = Category.objects.filter(is_active=True)
        self.fields['category'].required = False
        self.fields['bucket'].required = False
        if self.instance and self.instance.account_id:
            self.fields['bucket'].queryset = Bucket.objects.filter(
                account=self.instance.account, is_active=True)
        _bootstrap(self.fields)
        self.fields['category'].widget.attrs['class'] += ' tsearch'


class TransferForm(forms.Form):
    from_account = forms.ModelChoiceField(queryset=Account.objects.filter(is_active=True))
    to_account = forms.ModelChoiceField(queryset=Account.objects.filter(is_active=True))
    date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    amount = forms.DecimalField(min_value=Decimal('0.01'), max_digits=14, decimal_places=2)
    memo = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap(self.fields)

    def clean(self):
        data = super().clean()
        if data.get('from_account') and data.get('from_account') == data.get('to_account'):
            raise forms.ValidationError('From and To accounts must differ.')
        return data


class BucketForm(forms.ModelForm):
    class Meta:
        model = Bucket
        fields = ['account', 'name', 'target_amount']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = Account.objects.filter(is_active=True)
        self.fields['target_amount'].required = False
        _bootstrap(self.fields)


class AllocationForm(forms.Form):
    DIRECTION = [('in', 'Move into bucket'), ('out', 'Move out of bucket')]
    bucket = forms.ModelChoiceField(queryset=Bucket.objects.filter(is_active=True))
    direction = forms.ChoiceField(choices=DIRECTION, initial='in')
    amount = forms.DecimalField(min_value=Decimal('0.01'), max_digits=14, decimal_places=2)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap(self.fields)


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['parent', 'name']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['parent'].queryset = Category.objects.filter(parent__isnull=True)
        self.fields['parent'].required = False
        _bootstrap(self.fields)
        self.fields['parent'].widget.attrs['class'] += ' tsearch'
