from collections import OrderedDict, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (AllocationForm, BucketForm, CategoryForm, ReceiptForm,
                    TransferForm, TxnEditForm)
from .matching import find_candidates, run_matcher
from .models import (Account, AccountType, BalanceSnapshot, Bucket, BucketEntry,
                     Category, Institution, PropertyEntry, ReconciliationSession,
                     SyncRun, Transaction, TransactionSplit, Transfer, TxnSource,
                     TxnStatus)


INSTITUTION_ORDER = ['psecu', 'discover', 'wealthfront', 'fidelity']
PORTFOLIO_INSTS = ('fidelity', 'wealthfront')


def _inst_rank(name):
    low = name.lower()
    if 'property' in low:
        return 900  # always last
    for i, key in enumerate(INSTITUTION_ORDER):
        if key in low:
            return i
    return 500


def _grouped_accounts():
    accounts = list(Account.objects.filter(is_active=True)
                    .select_related('institution').prefetch_related('buckets'))
    new_ids = set(Transaction.objects.filter(is_new=True).values_list('account_id', flat=True))
    by_inst = {}
    for a in accounts:
        a.has_new = a.id in new_ids
        by_inst.setdefault(a.institution, []).append(a)
    insts = sorted(by_inst, key=lambda i: (_inst_rank(i.name), i.name))
    groups = []
    for inst in insts:
        accs = sorted(by_inst[inst], key=lambda x: (x.type, x.name))
        # include money set aside in buckets so the group total = sum of visible rows
        total = sum((a.current_balance + a.allocated_total for a in accs), Decimal('0'))
        groups.append({'name': inst.name, 'accounts': accs, 'total': total,
                       'has_new': any(a.has_new for a in accs)})
    # net worth = real money at the bank/market (online), not the spendable available
    net = sum((a.online_balance for a in accounts), Decimal('0'))
    portfolio = sum((a.online_balance for a in accounts
                     if any(k in a.institution.name.lower() for k in PORTFOLIO_INSTS)),
                    Decimal('0'))
    return groups, net, portfolio


SORT_KEYS = {
    'date': lambda t: (t.date, t.id),
    'payee': lambda t: t.payee.lower(),
    'category': lambda t: t.category_label.lower(),
    'amount': lambda t: t.amount,
    'balance': lambda t: t.running,
    'memo': lambda t: t.memo.lower(),
    # CL then date ascending: cleared/reconciled first, uncleared last
    'cl': lambda t: (1 if t.status == TxnStatus.UNCLEARED else 0, t.date, t.id),
}


def _default_account():
    return (Account.objects.filter(institution__name__icontains='psecu',
                                   name__icontains='checking').first()
            or Account.objects.filter(is_active=True).first())


@login_required
def dashboard(request):
    groups, net_worth, portfolio = _grouped_accounts()
    ctx = {
        'groups': groups,
        'net_worth': net_worth,
        'portfolio': portfolio,
        'review_count': Transaction.objects.filter(
            source=TxnSource.SIMPLEFIN, status=TxnStatus.CLEARED,
            account__is_reconcilable=True).count(),
    }

    bucket_id = request.GET.get('bucket')
    if bucket_id:
        bucket = get_object_or_404(Bucket, pk=bucket_id)
        entries = list(bucket.entries.order_by('date', 'id'))
        running = Decimal('0')
        today = timezone.now().date()
        for e in entries:
            running += e.amount
            e.running = running
        ctx.update({'bucket': bucket, 'bentries': entries, 'today': today})
        return render(request, 'tracker/dashboard.html', ctx)

    acct_id = request.GET.get('account')
    if not acct_id:  # default view = Checking
        default = _default_account()
        if default:
            acct_id = default.id

    if acct_id:
        account = get_object_or_404(Account, pk=acct_id)

        # property accounts get a value-history register instead of a transaction register
        if account.type == AccountType.PROPERTY:
            today = timezone.now().date()
            entries = list(account.property_entries.order_by('date', 'id'))
            running = Decimal('0')
            for e in entries:
                running += e.amount
                e.running = running
            ctx.update({'property_acct': account, 'pentries': entries, 'today': today})
            return render(request, 'tracker/dashboard.html', ctx)

        txns = account.transactions.select_related('category').prefetch_related(
            'splits__category', 'bucket')

        status = request.GET.get('status', '')
        if status in (TxnStatus.UNCLEARED, TxnStatus.CLEARED, TxnStatus.RECONCILED):
            txns = txns.filter(status=status)
        ttype = request.GET.get('type', '')
        if ttype == 'payments':
            txns = txns.filter(amount__lt=0)
        elif ttype == 'deposits':
            txns = txns.filter(amount__gt=0)

        today = timezone.now().date()
        rng = request.GET.get('range', '3m')
        if rng != 'all':
            txns = txns.filter(date__gte=today - timedelta(days=92))

        # running balance always computed in date order, then re-sorted for display
        txns = list(txns.order_by('date', 'id'))
        opening = account.current_balance - sum((t.amount for t in txns), Decimal('0'))
        running = opening
        for t in txns:
            running += t.amount
            t.running = running
            t.is_future = t.date > today
            t.split_list = list(t.splits.all())
            t.category_label = '— Split —' if t.split_list else (str(t.category) if t.category else '')

        sort = request.GET.get('sort', 'cl')
        direction = request.GET.get('dir', 'asc')
        if sort in SORT_KEYS:
            txns.sort(key=SORT_KEYS[sort], reverse=(direction == 'desc'))

        # Credit cards: running balance follows the DISPLAYED order and is owed-based
        # (charges raise the balance, payments/credits lower it). Anchored so the last
        # row = current balance; the last cleared row then shows the cleared balance.
        if account.type == AccountType.CREDIT_CARD:
            run = account.current_balance
            for t in reversed(txns):
                t.running = run
                run -= t.amount

        if account.type == AccountType.CREDIT_CARD:
            cleared = account.transactions.filter(
                status__in=[TxnStatus.CLEARED, TxnStatus.RECONCILED]
            ).aggregate(s=Sum('amount'))['s'] or Decimal('0')
            credits = account.transactions.filter(
                status=TxnStatus.UNCLEARED, amount__gt=0).aggregate(s=Sum('amount'))['s'] or Decimal('0')
            charges = account.transactions.filter(
                status=TxnStatus.UNCLEARED, amount__lt=0).aggregate(s=Sum('amount'))['s'] or Decimal('0')
            recon = {
                'kind': 'cc',
                'cleared': -cleared,  # liability: show owed as negative
                'credits': credits,
                'charges': charges,
                'balance': account.current_balance,
                'online': account.online_balance,
                'difference': account.online_balance - account.current_balance,
            }
        elif account.manual_balance:
            recon = {
                'manual': True,
                'online': account.online_balance,
                'current': account.current_balance,
                'difference': account.online_balance - account.current_balance,
            }
        else:
            uncleared_sum = account.transactions.filter(
                status=TxnStatus.UNCLEARED).aggregate(s=Sum('amount'))['s'] or Decimal('0')
            uncleared_flipped = -uncleared_sum
            dummy_sum = account.allocated_total
            local_total = account.current_balance + uncleared_flipped + dummy_sum
            recon = {
                'online': account.online_balance,
                'current': account.current_balance,
                'uncleared': uncleared_flipped,
                'dummy': dummy_sum,
                'local_total': local_total,
                'difference': account.online_balance - local_total,
            }

        ctx.update({
            'account': account,
            'txns': txns,
            'categories': Category.objects.filter(is_active=True).select_related('parent'),
            'status_filter': status,
            'type_filter': ttype,
            'range_filter': rng,
            'sort': sort,
            'dir': direction,
            'qbase': (f'?account={account.id}'
                      + (f'&status={status}' if status else '')
                      + (f'&type={ttype}' if ttype else '')
                      + (f'&range={rng}' if rng != '3m' else '')),
            'today': today,
            'has_new_here': any(getattr(t, 'is_new', False) for t in txns),
            'recon': recon,
        })
        return render(request, 'tracker/dashboard.html', ctx)

    return render(request, 'tracker/dashboard.html', ctx)


@login_required
@require_POST
def txn_review(request, txn_id):
    txn = get_object_or_404(Transaction, pk=txn_id)
    txn.is_new = False
    txn.save(update_fields=['is_new'])
    ref = request.META.get('HTTP_REFERER')
    return redirect(ref) if ref else _account_redirect(txn.account_id)


def _account_redirect(account_id):
    return redirect(f"{reverse('tracker:dashboard')}?account={account_id}")


def _bucket_redirect(bucket_id):
    return redirect(f"{reverse('tracker:dashboard')}?bucket={bucket_id}")


def _parse_amount(raw):
    return Decimal((raw or '').replace(',', '').strip())


def _recompute_available(account):
    from .models import recompute_balance
    recompute_balance(account)


def _recompute_property(account):
    total = account.property_entries.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    account.online_balance = total
    account.balance_synced_at = timezone.now()
    account.save(update_fields=['online_balance', 'balance_synced_at'])
    _recompute_available(account)


def _transfer_category():
    return Category.objects.get_or_create(parent=None, name='Transfer')[0]


def _create_bucket_entry(bucket, d, amount, memo):
    """Record a bucket move + a mirrored Transfer in the parent account, then
    reduce the parent's available balance so the money isn't double-counted."""
    direction = 'to' if amount >= 0 else 'from'
    txn = Transaction.objects.create(
        account=bucket.account, date=d, amount=-amount,
        payee=f'Transfer {direction} {bucket.name}', memo=memo,
        bucket=bucket, category=_transfer_category(),
        source=TxnSource.BUCKET, status=TxnStatus.CLEARED)
    entry = BucketEntry.objects.create(
        bucket=bucket, date=d, amount=amount, memo=memo, txn=txn)
    _recompute_available(bucket.account)
    return entry


@login_required
def property_add(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        try:
            value = Decimal((request.POST.get('value') or '0').replace(',', ''))
        except InvalidOperation:
            value = Decimal('0')
        if name:
            inst, _ = Institution.objects.get_or_create(
                name='Property', defaults={'connector_type': 'manual'})
            acct = Account.objects.create(
                institution=inst, name=name, type=AccountType.PROPERTY,
                current_balance=value, online_balance=value)
            PropertyEntry.objects.create(
                account=acct, date=request.POST.get('date') or timezone.now().date(),
                amount=value, memo='Initial value')
            messages.success(request, f'Added property "{name}".')
            return _account_redirect(acct.id)
    return render(request, 'tracker/property_add.html', {'today': timezone.now().date()})


@login_required
@require_POST
def property_entry_add(request, account_id):
    acct = get_object_or_404(Account, pk=account_id, type=AccountType.PROPERTY)
    try:
        amount = _parse_amount(request.POST.get('amount'))
    except InvalidOperation:
        messages.error(request, 'Invalid amount.')
        return _account_redirect(acct.id)
    PropertyEntry.objects.create(
        account=acct, date=request.POST.get('date') or timezone.now().date(),
        amount=amount, memo=request.POST.get('memo', '').strip())
    _recompute_property(acct)
    messages.success(request, 'Value change recorded.')
    return _account_redirect(acct.id)


@login_required
@require_POST
def property_entry_update(request, entry_id):
    e = get_object_or_404(PropertyEntry, pk=entry_id)
    try:
        e.amount = _parse_amount(request.POST.get('amount'))
    except InvalidOperation:
        messages.error(request, 'Invalid amount.')
        return _account_redirect(e.account_id)
    e.date = request.POST.get('date') or e.date
    e.memo = request.POST.get('memo', '').strip()
    e.save()
    _recompute_property(e.account)
    messages.success(request, 'Updated.')
    return _account_redirect(e.account_id)


@login_required
@require_POST
def property_entry_delete(request, entry_id):
    e = get_object_or_404(PropertyEntry, pk=entry_id)
    acct = e.account
    e.delete()
    _recompute_property(acct)
    messages.success(request, 'Deleted.')
    return _account_redirect(acct.id)


@login_required
@require_POST
def bucket_entry_add(request, bucket_id):
    bucket = get_object_or_404(Bucket, pk=bucket_id)
    try:
        amount = _parse_amount(request.POST.get('amount'))
    except InvalidOperation:
        messages.error(request, 'Invalid amount.')
        return _bucket_redirect(bucket.id)
    _create_bucket_entry(bucket, request.POST.get('date') or timezone.now().date(),
                         amount, request.POST.get('memo', '').strip())
    messages.success(request, 'Entry added.')
    return _bucket_redirect(bucket.id)


@login_required
@require_POST
def bucket_entry_update(request, entry_id):
    entry = get_object_or_404(BucketEntry, pk=entry_id)
    try:
        entry.amount = _parse_amount(request.POST.get('amount'))
    except InvalidOperation:
        messages.error(request, 'Invalid amount.')
        return _bucket_redirect(entry.bucket_id)
    entry.date = request.POST.get('date') or entry.date
    entry.memo = request.POST.get('memo', '').strip()
    entry.save()
    if entry.txn:  # keep the mirrored parent transfer in sync
        t = entry.txn
        t.amount = -entry.amount
        t.date = entry.date
        t.memo = entry.memo
        t.payee = f'Transfer {"to" if entry.amount >= 0 else "from"} {entry.bucket.name}'
        if t.category_id is None:
            t.category = _transfer_category()
        t.save()
    _recompute_available(entry.bucket.account)
    messages.success(request, 'Entry updated.')
    return _bucket_redirect(entry.bucket_id)


@login_required
@require_POST
def bucket_entry_delete(request, entry_id):
    entry = get_object_or_404(BucketEntry, pk=entry_id)
    bucket = entry.bucket
    if entry.txn:
        entry.txn.delete()
    entry.delete()
    _recompute_available(bucket.account)
    messages.success(request, 'Entry deleted.')
    return _bucket_redirect(bucket.id)


@login_required
@require_POST
def txn_add(request, account_id):
    account = get_object_or_404(Account, pk=account_id)
    try:
        amount = Decimal(request.POST.get('amount', '').replace(',', '').strip())
    except (InvalidOperation, AttributeError):
        messages.error(request, 'Invalid amount.')
        return _account_redirect(account.id)
    Transaction.objects.create(
        account=account,
        date=request.POST.get('date') or timezone.now().date(),
        amount=amount,
        payee=request.POST.get('payee', '').strip(),
        memo=request.POST.get('memo', '').strip(),
        category_id=request.POST.get('category') or None,
        status=TxnStatus.UNCLEARED,
        source=TxnSource.MANUAL,
    )
    _recompute_available(account)
    messages.success(request, 'Transaction added.')
    return _account_redirect(account.id)


@login_required
def account_edit(request, account_id):
    from .forms import AccountForm
    account = get_object_or_404(Account, pk=account_id)
    if request.method == 'POST':
        form = AccountForm(request.POST, instance=account)
        if form.is_valid():
            form.save()
            _recompute_available(account)
            messages.success(request, 'Account updated.')
            return _account_redirect(account.id)
    else:
        form = AccountForm(instance=account)
    return render(request, 'tracker/form.html', {
        'form': form, 'title': f'Edit account: {account.name}'})


@login_required
def account_register(request, account_id):
    account = get_object_or_404(Account, pk=account_id)
    txns = account.transactions.select_related('category', 'bucket')[:300]
    return render(request, 'tracker/register.html', {
        'account': account,
        'txns': txns,
    })


@login_required
def reconcile(request, account_id):
    account = get_object_or_404(Account, pk=account_id)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'lock':
            n = account.transactions.filter(status=TxnStatus.CLEARED).update(
                status=TxnStatus.RECONCILED)
            raw = (request.POST.get('statement_balance') or str(account.current_balance)).replace(',', '')
            statement = Decimal(raw)
            ReconciliationSession.objects.create(
                account=account, statement_date=timezone.now().date(),
                statement_balance=statement, cleared_balance=account.cleared_balance,
                completed=account.cleared_balance == statement)
            messages.success(request, f'Locked {n} cleared transactions as reconciled.')
        return redirect(reverse('tracker:reconcile', args=[account.id]))

    txns = account.transactions.exclude(status=TxnStatus.RECONCILED).select_related('category')
    cleared_balance = account.cleared_balance
    synced = account.current_balance
    return render(request, 'tracker/reconcile.html', {
        'account': account,
        'txns': txns,
        'cleared_balance': cleared_balance,
        'synced_balance': synced,
        'difference': synced - cleared_balance,
        'balance_matches': cleared_balance == synced,
    })


@login_required
@require_POST
def toggle_clear(request, txn_id):
    txn = get_object_or_404(Transaction, pk=txn_id)
    txn.status = (TxnStatus.UNCLEARED if txn.status == TxnStatus.CLEARED
                  else TxnStatus.CLEARED)
    txn.save(update_fields=['status'])
    _recompute_available(txn.account)
    return redirect(request.META.get('HTTP_REFERER',
                                     reverse('tracker:reconcile', args=[txn.account_id])))


@login_required
def review_queue(request):
    """Imported bank txns the matcher could not auto-pair, with manual candidates."""
    imported = Transaction.objects.filter(
        source=TxnSource.SIMPLEFIN, status=TxnStatus.CLEARED,
        account__is_reconcilable=True).select_related('account')[:200]
    rows = [{'imported': t, 'candidates': find_candidates(t)} for t in imported]
    return render(request, 'tracker/review.html', {'rows': rows})


@login_required
@require_POST
def match_pair(request):
    imported = get_object_or_404(Transaction, pk=request.POST['imported_id'],
                                 source=TxnSource.SIMPLEFIN)
    manual = get_object_or_404(Transaction, pk=request.POST['manual_id'],
                               source=TxnSource.MANUAL)
    if imported.account_id != manual.account_id:
        messages.error(request, 'Accounts differ; cannot match.')
    else:
        from .matching import _merge
        _merge(manual, imported)
        messages.success(request, 'Matched.')
    return redirect(reverse('tracker:review_queue'))


@login_required
@require_POST
def accept_import(request, txn_id):
    """Accept an unmatched bank txn as a real (uncategorized) transaction."""
    get_object_or_404(Transaction, pk=txn_id, source=TxnSource.SIMPLEFIN)
    messages.info(request, 'Import kept as-is.')
    return redirect(reverse('tracker:review_queue'))


@login_required
def receipt_new(request):
    initial = {}
    if request.GET.get('account'):
        initial['account'] = request.GET['account']
    if request.method == 'POST':
        form = ReceiptForm(request.POST)
        if form.is_valid():
            txn = form.save()
            _recompute_available(txn.account)
            messages.success(request, 'Receipt logged.')
            return _account_redirect(txn.account_id)
    else:
        form = ReceiptForm(initial=initial)
    return render(request, 'tracker/form.html', {'form': form, 'title': 'Log receipt'})


@login_required
def txn_edit(request, txn_id):
    txn = get_object_or_404(Transaction, pk=txn_id)
    if request.method == 'POST':
        form = TxnEditForm(request.POST, instance=txn)
        if form.is_valid():
            form.save()
            messages.success(request, 'Transaction updated.')
            return _account_redirect(txn.account_id)
    else:
        form = TxnEditForm(instance=txn)
    return render(request, 'tracker/form.html', {'form': form, 'title': 'Edit transaction'})


@login_required
@require_POST
def txn_delete(request, txn_id):
    txn = get_object_or_404(Transaction, pk=txn_id)
    account = txn.account
    account_id = txn.account_id
    txn.delete()
    _recompute_available(account)
    messages.success(request, 'Transaction deleted.')
    return _account_redirect(account_id)


@login_required
@require_POST
def txn_update(request, txn_id):
    """In-place edit of a single (non-split) transaction's date/payee/category/amount/memo."""
    txn = get_object_or_404(Transaction, pk=txn_id)
    if txn.is_split:
        messages.error(request, 'Split transaction — use Split to edit its lines.')
        return _account_redirect(txn.account_id)
    try:
        amount = Decimal(request.POST.get('amount', '').replace(',', '').strip())
    except InvalidOperation:
        messages.error(request, 'Invalid amount.')
        return _account_redirect(txn.account_id)
    txn.date = request.POST.get('date') or txn.date
    txn.payee = request.POST.get('payee', '').strip()
    txn.memo = request.POST.get('memo', '').strip()
    txn.category_id = request.POST.get('category') or None
    txn.amount = amount
    txn.save()
    _recompute_available(txn.account)
    messages.success(request, 'Transaction updated.')
    return _account_redirect(txn.account_id)


@login_required
def split_edit(request, txn_id):
    txn = get_object_or_404(Transaction, pk=txn_id)
    if request.method == 'POST':
        if request.POST.get('action') == 'clear':
            txn.splits.all().delete()
            messages.success(request, 'Splits removed.')
            return _account_redirect(txn.account_id)
        cats = request.POST.getlist('cat')
        amts = request.POST.getlist('amt')
        memos = request.POST.getlist('memo')
        rows = []
        for cat, amt, memo in zip(cats, amts, memos):
            amt = (amt or '').replace(',', '').strip()
            if not amt:
                continue
            try:
                rows.append((cat or None, Decimal(amt), memo.strip()))
            except InvalidOperation:
                messages.error(request, f'Invalid amount: {amt}')
                return redirect(reverse('tracker:split_edit', args=[txn.id]))
        total = sum((d for _, d, _ in rows), Decimal('0'))
        if not rows:
            messages.error(request, 'Add at least one split line.')
        elif total != txn.amount:
            messages.error(request, f'Splits total ${total} must equal the transaction '
                                    f'amount ${txn.amount}. Off by ${txn.amount - total}.')
        else:
            txn.splits.all().delete()
            for cat, d, memo in rows:
                TransactionSplit.objects.create(
                    transaction=txn, category_id=cat, amount=d, memo=memo)
            txn.category = None
            txn.save(update_fields=['category'])
            messages.success(request, f'Split into {len(rows)} lines.')
            return _account_redirect(txn.account_id)
        return redirect(reverse('tracker:split_edit', args=[txn.id]))

    existing = list(txn.splits.select_related('category').all())
    return render(request, 'tracker/split.html', {
        'txn': txn,
        'existing': existing,
        'categories': Category.objects.filter(is_active=True).select_related('parent'),
    })


@login_required
def transfer_new(request):
    if request.method == 'POST':
        form = TransferForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            transfer = Transfer.objects.create(note=d['memo'])
            tcat = _transfer_category()
            Transaction.objects.create(
                account=d['from_account'], date=d['date'], amount=-d['amount'],
                payee=f'Transfer to {d["to_account"].name}', memo=d['memo'],
                category=tcat, status=TxnStatus.UNCLEARED, source=TxnSource.MANUAL,
                transfer=transfer)
            Transaction.objects.create(
                account=d['to_account'], date=d['date'], amount=d['amount'],
                payee=f'Transfer from {d["from_account"].name}', memo=d['memo'],
                category=tcat, status=TxnStatus.UNCLEARED, source=TxnSource.MANUAL,
                transfer=transfer)
            _recompute_available(d['from_account'])
            _recompute_available(d['to_account'])
            messages.success(request, 'Transfer recorded.')
            return redirect(reverse('tracker:dashboard'))
    else:
        form = TransferForm()
    return render(request, 'tracker/form.html', {'form': form, 'title': 'Record transfer'})


@login_required
def buckets(request):
    create_form = BucketForm()
    alloc_form = AllocationForm()
    if request.method == 'POST':
        if 'create_bucket' in request.POST:
            create_form = BucketForm(request.POST)
            if create_form.is_valid():
                create_form.save()
                messages.success(request, 'Bucket created.')
                return redirect(reverse('tracker:buckets'))
        elif 'allocate' in request.POST:
            alloc_form = AllocationForm(request.POST)
            if alloc_form.is_valid():
                b = alloc_form.cleaned_data['bucket']
                amt = alloc_form.cleaned_data['amount']
                signed = amt if alloc_form.cleaned_data['direction'] == 'in' else -amt
                _create_bucket_entry(b, timezone.now().date(), signed, 'Allocation')
                messages.success(request, f'Moved ${signed} to {b.name}.')
                return redirect(reverse('tracker:buckets'))
    accounts = (Account.objects.filter(is_active=True, buckets__isnull=False)
                .distinct().prefetch_related('buckets'))
    return render(request, 'tracker/buckets.html', {
        'accounts': accounts, 'create_form': create_form, 'alloc_form': alloc_form})


def _descendant_ids(cat):
    ids = [cat.id]
    stack = list(cat.children.all())
    while stack:
        c = stack.pop()
        ids.append(c.id)
        stack.extend(c.children.all())
    return ids


def _category_txns(cat):
    """Queryset of transactions in this category or any subcategory, counting
    split lines too (a split transaction shows up under each split's category)."""
    ids = _descendant_ids(cat)
    return Transaction.objects.filter(
        Q(category_id__in=ids) | Q(splits__category_id__in=ids)).distinct()


def _category_txn_count(cat):
    return _category_txns(cat).count()


def _uncategorized_txns():
    """Transactions that could carry a category but don't (non-split, null category)."""
    return Transaction.objects.filter(category__isnull=True, splits__isnull=True)


def _cat_node(cat):
    return {
        'cat': cat,
        'count': _category_txn_count(cat),
        'children': [_cat_node(c) for c in cat.children.all().order_by('name')],
    }


@login_required
def categories(request):
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category added.')
            return redirect(reverse('tracker:categories'))
    else:
        form = CategoryForm()
    roots = [_cat_node(c) for c in
             Category.objects.filter(parent__isnull=True).order_by('name')]
    return render(request, 'tracker/categories.html', {
        'roots': roots,
        'form': form,
        'uncategorized_count': _uncategorized_txns().count(),
    })


SUMMARY_ORDER = {
    'date': 'date', 'payee': 'payee', 'amount': 'amount', 'memo': 'memo',
    'account': 'account__name', 'category': 'category__name',
}


def _apply_summary_filters(request, txns):
    q = request.GET.get('q', '').strip()
    if q:
        txns = txns.filter(Q(payee__icontains=q) | Q(memo__icontains=q))
    rng = request.GET.get('range', 'all')
    if rng != 'all':
        txns = txns.filter(date__gte=timezone.now().date() - timedelta(days=92))
    sort = request.GET.get('sort', 'date')
    direction = request.GET.get('dir', 'desc')
    field = SUMMARY_ORDER.get(sort, 'date')
    prefix = '' if direction == 'asc' else '-'
    txns = txns.order_by(prefix + field, '-id')
    return txns, q, rng, sort, direction


@login_required
def category_summary(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    txns = (_category_txns(cat).select_related('account', 'category')
            .prefetch_related('splits__category'))
    txns, q, rng, sort, direction = _apply_summary_filters(request, txns)
    return render(request, 'tracker/category_summary.html', {
        'cat': cat,
        'txns': txns,
        'count': txns.count(),
        'q': q,
        'range_filter': rng,
        'sort': sort,
        'dir': direction,
        'qbase': '?' + urlencode({'q': q, 'range': rng}),
        'edit_form': CategoryForm(instance=cat, initial={'parent': cat.parent}),
        'all_categories': Category.objects.filter(is_active=True).select_related('parent'),
        'source_category_id': cat.id,
        'is_uncategorized': False,
    })


@login_required
def uncategorized_summary(request):
    txns = _uncategorized_txns().select_related('account')
    txns, q, rng, sort, direction = _apply_summary_filters(request, txns)
    return render(request, 'tracker/category_summary.html', {
        'cat': None,
        'txns': txns,
        'count': txns.count(),
        'q': q,
        'range_filter': rng,
        'sort': sort,
        'dir': direction,
        'qbase': '?' + urlencode({'q': q, 'range': rng}),
        'all_categories': Category.objects.filter(is_active=True).select_related('parent'),
        'source_category_id': '',
        'is_uncategorized': True,
    })


@login_required
@require_POST
def move_transfers(request):
    """One-click: move uncategorized 'Transfer …' / card-payment rows to a Transfer category."""
    transfer_cat, _ = Category.objects.get_or_create(parent=None, name='Transfer')
    ids = list(_uncategorized_txns().filter(
        Q(payee__icontains='transfer') | Q(payee__icontains='card payment')
    ).values_list('id', flat=True))
    n = Transaction.objects.filter(id__in=ids).update(category=transfer_cat)
    messages.success(request, f'Moved {n} transfer transaction(s) into "Transfer".')
    return redirect(reverse('tracker:uncategorized_summary'))


@login_required
@require_POST
def bulk_categorize(request):
    ids = request.POST.getlist('txn')
    target = request.POST.get('target_category') or None
    source = request.POST.get('source_category') or None
    updated = 0
    for txn in Transaction.objects.filter(id__in=ids).prefetch_related('splits'):
        splits = list(txn.splits.all())
        if splits:
            qs = txn.splits.filter(category_id=source) if source else txn.splits.all()
            updated += qs.update(category_id=target)
        else:
            txn.category_id = target
            txn.save(update_fields=['category'])
            updated += 1
    messages.success(request, f'Updated category on {updated} item(s).')
    return redirect(request.POST.get('next') or reverse('tracker:categories'))


@login_required
def category_edit(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    if request.method == 'POST':
        form = CategoryForm(request.POST, instance=cat)
        # prevent making a category its own parent / a child of its own descendant
        form.fields['parent'].queryset = Category.objects.filter(
            parent__isnull=True).exclude(pk=cat.pk)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category updated.')
            return redirect(reverse('tracker:categories'))
    else:
        form = CategoryForm(instance=cat)
        form.fields['parent'].queryset = Category.objects.filter(
            parent__isnull=True).exclude(pk=cat.pk)
    return render(request, 'tracker/form.html', {'form': form, 'title': f'Edit category: {cat}'})


@login_required
@require_POST
def category_delete(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    count = _category_txn_count(cat)
    if count:
        messages.error(
            request, f'Cannot delete "{cat}" — {count} transaction(s) use it or a subcategory. '
                     'Reassign those first.')
    else:
        name = str(cat)
        cat.delete()
        messages.success(request, f'Deleted category "{name}".')
    return redirect(reverse('tracker:categories'))


def _date_range(request):
    """Return (start, end, preset) from request GET. Custom start/end override preset."""
    today = timezone.now().date()
    preset = request.GET.get('range', 'all')
    start = request.GET.get('start') or None
    end = request.GET.get('end') or None
    if start or end:
        return start, end, 'custom'
    if preset == 'ytd':
        return today.replace(month=1, day=1).isoformat(), None, preset
    if preset == '12m':
        return (today - timedelta(days=365)).isoformat(), None, preset
    if preset == '3m':
        return (today - timedelta(days=92)).isoformat(), None, preset
    return None, None, 'all'


def _cat_sum(ids, start, end):
    """Net amount attributable to a category (and descendants), splits-aware."""
    direct = Transaction.objects.filter(category_id__in=ids, splits__isnull=True)
    split = TransactionSplit.objects.filter(category_id__in=ids)
    if start:
        direct = direct.filter(date__gte=start)
        split = split.filter(transaction__date__gte=start)
    if end:
        direct = direct.filter(date__lte=end)
        split = split.filter(transaction__date__lte=end)
    d = direct.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    s = split.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    return d + s


@login_required
def report_categories(request):
    start, end, preset = _date_range(request)
    level = request.GET.get('level', 'sub')  # 'top' or 'sub'
    selected = request.GET.getlist('cats')   # top-level category ids to include

    roots = list(Category.objects.filter(parent__isnull=True).order_by('name')
                 .prefetch_related('children'))
    if selected:
        roots = [r for r in roots if str(r.id) in selected]

    def node(root):
        total = _cat_sum(_descendant_ids(root), start, end)
        children = []
        if level == 'sub':
            for ch in root.children.all().order_by('name'):
                children.append({'cat': ch, 'total': _cat_sum(_descendant_ids(ch), start, end)})
        return {'cat': root, 'total': total, 'children': children}

    income, spending = [], []
    for r in roots:
        (income if r.name.lower() == 'income' else spending).append(node(r))
    income_total = sum((n['total'] for n in income), Decimal('0'))
    spending_total = sum((n['total'] for n in spending), Decimal('0'))

    return render(request, 'tracker/report_categories.html', {
        'income': income, 'spending': spending,
        'income_total': income_total, 'spending_total': spending_total,
        'net_total': income_total + spending_total,
        'level': level, 'preset': preset, 'start': start or '', 'end': end or '',
        'all_roots': Category.objects.filter(parent__isnull=True).order_by('name'),
        'selected': selected,
    })


@login_required
def restore_qif(request):
    from io import StringIO
    from django.core.management import call_command
    result = None
    if request.method == 'POST' and request.FILES.get('qif'):
        import tempfile
        inst = request.POST.get('institution', '').strip() or 'Imported'
        dry = bool(request.POST.get('dry_run'))
        with tempfile.NamedTemporaryFile(suffix='.qif', delete=False, mode='wb') as tmp:
            for chunk in request.FILES['qif'].chunks():
                tmp.write(chunk)
            tmp_path = tmp.name
        out = StringIO()
        try:
            call_command('import_qif', tmp_path, institution=inst, dry_run=dry, stdout=out)
            result = out.getvalue().strip()
            messages.success(request, 'QIF processed.')
        except Exception as e:
            result = f'Error: {e}'
            messages.error(request, 'Import failed — see details below.')
    return render(request, 'tracker/restore_qif.html', {
        'result': result,
        'institutions': Institution.objects.order_by('name'),
    })


@login_required
def export_qif(request, account_id=None):
    from django.http import HttpResponse
    from .qif_export import build_qif
    if account_id:
        accounts = Account.objects.filter(pk=account_id)
        fname = f'{accounts[0].name}.qif' if accounts else 'export.qif'
    else:
        accounts = Account.objects.filter(is_active=True).select_related('institution')
        fname = 'money-tracker-all.qif'
    resp = HttpResponse(build_qif(accounts), content_type='application/qif')
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


@login_required
def reports(request):
    today = timezone.now().date()
    try:
        days = int(request.GET.get('days', 90))
    except ValueError:
        days = 90
    start = today - timedelta(days=days)

    # Net worth over time (sum of all account snapshots per date)
    nw = list(BalanceSnapshot.objects.values('date')
              .annotate(total=Sum('balance')).order_by('date'))
    networth = {
        'labels': [r['date'].isoformat() for r in nw],
        'values': [float(r['total']) for r in nw],
    }

    # Spending by category (expenses) rolled up to top-level category.
    # Split transactions contribute per split line; others by their own category.
    def _rollup_key(cat):
        if cat and cat.parent:
            return cat.parent.name
        if cat:
            return cat.name
        return 'Uncategorized'

    spend = defaultdict(Decimal)
    for t in (Transaction.objects.filter(date__gte=start)
              .select_related('category', 'category__parent')
              .prefetch_related('splits__category__parent')):
        splits = list(t.splits.all())
        if splits:
            for s in splits:
                if s.amount < 0:
                    spend[_rollup_key(s.category)] += -s.amount
        elif t.amount < 0:
            spend[_rollup_key(t.category)] += -t.amount
    spending = sorted(([k, v] for k, v in spend.items()), key=lambda x: x[1], reverse=True)

    # Cash flow by month
    flow = defaultdict(lambda: [Decimal('0'), Decimal('0')])  # month -> [income, expense]
    for t in Transaction.objects.filter(date__gte=start):
        m = t.date.strftime('%Y-%m')
        if t.amount >= 0:
            flow[m][0] += t.amount
        else:
            flow[m][1] += -t.amount
    months = sorted(flow)
    cashflow = {
        'labels': months,
        'income': [float(flow[m][0]) for m in months],
        'expense': [float(flow[m][1]) for m in months],
    }

    bucket_rows = Bucket.objects.filter(is_active=True).select_related('account')

    return render(request, 'tracker/reports.html', {
        'days': days,
        'networth': networth,
        'spending': spending,
        'spending_total': sum((v for _, v in spending), Decimal('0')),
        'cashflow': cashflow,
        'bucket_rows': bucket_rows,
    })


@login_required
@require_POST
def run_sync_now(request):
    from django.conf import settings
    from datetime import date, timedelta
    from .connectors.simplefin import SimpleFinConnector
    from .models import ConnectionCredential, ConnectorType
    from .sync import apply_result
    cred = (ConnectionCredential.objects
            .filter(connector_type=ConnectorType.SIMPLEFIN).order_by('-id').first())
    url = cred.get_secret() if cred and cred.encrypted_secret else settings.SIMPLEFIN_ACCESS_URL
    if not url:
        messages.error(request, 'No SimpleFIN access URL configured.')
        return redirect(reverse('tracker:dashboard'))
    result = SimpleFinConnector().fetch(url, start_date=date.today() - timedelta(days=14))
    run = apply_result(result, connector_type=ConnectorType.SIMPLEFIN)
    messages.success(request, f'Synced {run.accounts_synced} accounts, '
                              f'+{run.transactions_added} txns, {run.transactions_matched} matched.')
    return redirect(reverse('tracker:dashboard'))
