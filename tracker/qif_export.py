"""Build QIF (Quicken Interchange Format) text — the portable, Quicken-importable
representation of accounts + transactions + splits."""
from .models import AccountType

QIF_TYPE = {
    AccountType.CHECKING: 'Bank',
    AccountType.SAVINGS: 'Bank',
    AccountType.MONEY_MARKET: 'Bank',
    AccountType.CASH: 'Cash',
    AccountType.CREDIT_CARD: 'CCard',
    AccountType.BROKERAGE: 'Oth A',
    AccountType.K401: 'Oth A',
    AccountType.IRA: 'Oth A',
    AccountType.HSA: 'Oth A',
    AccountType.PENSION: 'Oth A',
    AccountType.PROPERTY: 'Oth A',
    AccountType.OTHER: 'Bank',
}


def _cat_path(cat):
    parts = []
    while cat is not None:
        parts.append(cat.name)
        cat = cat.parent
    return ':'.join(reversed(parts))


def _cleared(status):
    return 'X' if status == 'reconciled' else ('*' if status == 'cleared' else '')


def build_qif(accounts):
    out = []
    for a in accounts:
        qtype = QIF_TYPE.get(a.type, 'Bank')
        out.append('!Account')
        out.append('N' + a.name)
        out.append('T' + qtype)
        out.append('^')
        out.append('!Type:' + qtype)
        txns = (a.transactions.select_related('category', 'category__parent')
                .prefetch_related('splits__category__parent').order_by('date', 'id'))
        for t in txns:
            out.append('D' + t.date.strftime('%m/%d/%Y'))
            out.append('T%.2f' % t.amount)
            if t.payee:
                out.append('P' + t.payee)
            if t.memo:
                out.append('M' + t.memo)
            c = _cleared(t.status)
            if c:
                out.append('C' + c)
            splits = list(t.splits.all())
            if splits:
                for s in splits:
                    out.append('S' + (_cat_path(s.category) if s.category else ''))
                    if s.memo:
                        out.append('E' + s.memo)
                    out.append('$%.2f' % s.amount)
            elif t.category:
                out.append('L' + _cat_path(t.category))
            out.append('^')
    return '\n'.join(out) + '\n'
