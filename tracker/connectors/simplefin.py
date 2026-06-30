import base64
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from .base import NormAccount, NormHolding, NormTxn, SyncResult


def claim_access_url(setup_token: str) -> str:
    """Exchange a one-time SimpleFIN setup token for a durable access URL.
    Run this once; store the returned URL as the encrypted credential secret."""
    claim_url = base64.b64decode(setup_token).decode()
    resp = requests.post(claim_url, timeout=30)
    resp.raise_for_status()
    return resp.text.strip()


def _dec(value, default='0') -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(default)


def _ts_to_date(ts) -> date:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()


class SimpleFinConnector:
    connector_type = 'simplefin'

    def fetch(self, secret: str, *, start_date: date | None = None) -> SyncResult:
        access_url = secret.rstrip('/')
        params = {'pending': 1}
        if start_date:
            params['start-date'] = int(
                datetime(start_date.year, start_date.month, start_date.day,
                         tzinfo=timezone.utc).timestamp()
            )
        resp = requests.get(f'{access_url}/accounts', params=params, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        result = SyncResult(errors=list(data.get('errors', [])))
        for acct in data.get('accounts', []):
            ext_id = acct['id']
            org = acct.get('org', {})
            org_name = org.get('name') or org.get('domain') or 'Unknown'
            result.accounts.append(NormAccount(
                external_id=ext_id,
                org=org_name,
                name=acct.get('name', ext_id),
                balance=_dec(acct.get('balance')),
                balance_date=_ts_to_date(acct.get('balance-date') or 0)
                if acct.get('balance-date') else date.today(),
                currency=acct.get('currency', 'USD'),
            ))
            for txn in acct.get('transactions', []):
                result.transactions.append(NormTxn(
                    account_ext_id=ext_id,
                    external_id=str(txn['id']),
                    date=_ts_to_date(txn.get('posted') or txn.get('transacted_at') or 0),
                    amount=_dec(txn.get('amount')),
                    payee=txn.get('payee') or '',
                    memo=txn.get('description') or txn.get('memo') or '',
                    pending=bool(txn.get('pending')),
                ))
            for h in acct.get('holdings', []):
                symbol = (h.get('symbol') or h.get('description') or '?')[:40]
                result.holdings.append(NormHolding(
                    account_ext_id=ext_id,
                    symbol=symbol,
                    description=h.get('description', ''),
                    quantity=_dec(h.get('shares')),
                    price=_dec(h.get('purchase_price') or h.get('purchase-price')),
                    market_value=_dec(h.get('market_value') or h.get('market-value')),
                ))
        return result
