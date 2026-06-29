"""Minimal QIF (Quicken Interchange Format) parser.

Handles the cash-flow account types Quicken Windows still exports (Bank, CCard,
Cash). Investment sections (!Type:Invst) are recognized but parsed best-effort.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass
class QifSplit:
    category: str = ''
    memo: str = ''
    amount: Decimal = Decimal('0')


@dataclass
class QifTxn:
    date: date | None = None
    amount: Decimal = Decimal('0')
    payee: str = ''
    memo: str = ''
    category: str = ''      # 'L' line; '[Account]' indicates a transfer
    cleared: str = ''
    number: str = ''
    splits: list[QifSplit] = field(default_factory=list)

    @property
    def is_transfer(self) -> bool:
        return self.category.startswith('[') and self.category.endswith(']')

    @property
    def transfer_account(self) -> str:
        return self.category[1:-1] if self.is_transfer else ''


@dataclass
class QifAccountBlock:
    name: str
    type: str = ''
    transactions: list[QifTxn] = field(default_factory=list)


def _parse_amount(raw: str) -> Decimal:
    raw = raw.replace(',', '').strip()
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal('0')


def parse_date(raw: str) -> date | None:
    raw = raw.strip().replace("'", '/').replace('.', '/').replace(' ', '')
    parts = [p for p in raw.split('/') if p]
    if len(parts) != 3:
        return None
    try:
        m, d, y = (int(p) for p in parts)
    except ValueError:
        return None
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse(text: str) -> tuple[list[QifAccountBlock], list[str]]:
    """Return (account blocks, category names). Categories come from !Type:Cat."""
    blocks: list[QifAccountBlock] = []
    categories: list[str] = []
    current: QifAccountBlock | None = None
    txn = QifTxn()
    split = None
    mode = None  # 'cat' | 'acct' | 'txn'
    pending_account_name = None
    pending_account_type = ''

    def flush_txn():
        nonlocal txn, split
        if current is not None and (txn.date or txn.amount or txn.payee):
            if split:
                txn.splits.append(split)
            current.transactions.append(txn)
        txn = QifTxn()
        split = None

    for line in text.splitlines():
        line = line.rstrip('\n').rstrip('\r')
        if not line:
            continue
        if line.startswith('!'):
            header = line[1:].strip().lower()
            if header == 'type:cat':
                mode = 'cat'
            elif header == 'account':
                mode = 'acct'
                pending_account_name = None
                pending_account_type = ''
            elif header.startswith('type:'):
                mode = 'txn'
                # transactions that follow belong to the last-named account
                if pending_account_name:
                    current = QifAccountBlock(pending_account_name, pending_account_type)
                    blocks.append(current)
                    pending_account_name = None
            continue

        code, value = line[0], line[1:].strip()

        if mode == 'cat':
            if code == 'N':
                categories.append(value)
            elif code == '^':
                pass
            continue

        if mode == 'acct':
            if code == 'N':
                pending_account_name = value
            elif code == 'T':
                pending_account_type = value
            continue

        # transaction mode
        if code == '^':
            flush_txn()
        elif code == 'D':
            txn.date = parse_date(value)
        elif code in ('T', 'U'):
            txn.amount = _parse_amount(value)
        elif code == 'P':
            txn.payee = value
        elif code == 'M':
            txn.memo = value
        elif code == 'L':
            txn.category = value
        elif code == 'C':
            txn.cleared = value
        elif code == 'N':
            txn.number = value
        elif code == 'S':
            if split:
                txn.splits.append(split)
            split = QifSplit(category=value)
        elif code == 'E' and split:
            split.memo = value
        elif code == '$' and split:
            split.amount = _parse_amount(value)

    flush_txn()
    return blocks, categories
