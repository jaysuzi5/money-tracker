from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass
class NormAccount:
    external_id: str
    org: str                      # institution / org name from the source
    name: str
    balance: Decimal
    balance_date: date
    currency: str = 'USD'


@dataclass
class NormTxn:
    account_ext_id: str
    external_id: str
    date: date
    amount: Decimal               # signed: negative = outflow
    payee: str = ''
    memo: str = ''
    pending: bool = False         # provider still processing (in-flight)


@dataclass
class NormHolding:
    account_ext_id: str
    symbol: str
    description: str
    quantity: Decimal
    price: Decimal
    market_value: Decimal


@dataclass
class SyncResult:
    accounts: list[NormAccount] = field(default_factory=list)
    transactions: list[NormTxn] = field(default_factory=list)
    holdings: list[NormHolding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Connector(Protocol):
    connector_type: str

    def fetch(self, secret: str, *, start_date: date | None = None) -> SyncResult:
        ...
