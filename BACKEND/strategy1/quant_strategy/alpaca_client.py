"""READ-ONLY Alpaca client (REST via requests). This class deliberately has no method that can place, replace or cancel an order: it only issues HTTP GET.
The bot analyses and suggests; every trade is made by a human."""
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests


class AlpacaError(RuntimeError):
    pass


class AlpacaReadOnly:
    READ_ONLY = True

    def __init__(self, key, secret, trading_url='https://paper-api.alpaca.markets', data_url='https://data.alpaca.markets', feed='iex', session=None, retries=3):
        self.trading_url, self.data_url, self.feed, self.retries = trading_url.rstrip('/'), data_url.rstrip('/'), feed, retries
        self.session = session or requests.Session()
        self.headers = {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret}

    def _get(self, base, path, params=None):
        last = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(base + path, headers=self.headers, params=params, timeout=30)
            except requests.RequestException as exc:
                last = f'{type(exc).__name__}'
                time.sleep(1 + attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last = f'HTTP {response.status_code}'
                time.sleep(1 + attempt)
                continue
            if response.status_code != 200:
                raise AlpacaError(f'Alpaca {path} -> HTTP {response.status_code}: {response.text[:200]}')
            return response.json()
        raise AlpacaError(f'Alpaca {path} failed after {self.retries} tries ({last})')

    # ---- account (read only) -----------------------------------------------------------------------------------------------------------
    def account(self):
        return self._get(self.trading_url, '/v2/account')

    def positions(self):
        """[{symbol, qty, market_value, cost_basis, avg_entry_price, current_price}] with numbers parsed."""
        rows = self._get(self.trading_url, '/v2/positions')
        keys = ('qty', 'market_value', 'cost_basis', 'avg_entry_price', 'current_price')
        return [{**r, **{k: float(r[k]) for k in keys if r.get(k) is not None}} for r in rows]

    def fills(self, days=400):
        """Executed trades (activity type FILL) of the last `days`, newest first."""
        after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00Z')
        out, token = [], None
        for _ in range(50):
            params = dict(after=after, direction='desc', page_size=100)
            if token:
                params['page_token'] = token
            page = self._get(self.trading_url, '/v2/account/activities/FILL', params)
            out.extend(page)
            if len(page) < 100:
                break
            token = page[-1]['id']
        return out

    def calendar(self, start, end):
        return self._get(self.trading_url, '/v2/calendar', dict(start=str(start), end=str(end)))

    def clock(self):
        return self._get(self.trading_url, '/v2/clock')

    # ---- market data ---------------------------------------------------------------------------------------------------------------------
    def daily_bars(self, symbols, start, end=None, adjustment='all', chunk=40) -> pd.DataFrame:
        """Long frame: symbol, date (UTC midnight), open, high, low, close, volume. `adjustment='all'` = split and dividend adjusted."""
        symbols = sorted({s.upper() for s in symbols})
        rows = []
        if self.feed == 'sip' and not end:
            end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime('%Y-%m-%dT%H:%M:%SZ')    # a free account may query SIP only for data at least 15 minutes old
        for i in range(0, len(symbols), chunk):
            token = None
            for _ in range(100):
                params = dict(symbols=','.join(symbols[i:i + chunk]), timeframe='1Day', start=str(start), adjustment=adjustment, feed=self.feed, limit=10000)
                if end:
                    params['end'] = str(end)
                if token:
                    params['page_token'] = token
                data = self._get(self.data_url, '/v2/stocks/bars', params)
                for sym, bars in (data.get('bars') or {}).items():
                    rows.extend(dict(symbol=sym, date=pd.Timestamp(b['t']).tz_convert('UTC').normalize(), open=b['o'], high=b['h'], low=b['l'], close=b['c'], volume=b['v']) for b in bars)
                token = data.get('next_page_token')
                if not token:
                    break
        return pd.DataFrame(rows, columns=['symbol', 'date', 'open', 'high', 'low', 'close', 'volume'])
