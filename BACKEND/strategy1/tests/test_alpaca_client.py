import inspect

import pytest

from quant_strategy.alpaca_client import AlpacaError, AlpacaReadOnly


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, str(payload)

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)

    def __getattr__(self, name):                                   # any other HTTP verb must never be reachable
        if name in ('post', 'put', 'delete', 'patch', 'request'):
            raise AssertionError(f'read-only client used {name}')
        raise AttributeError(name)


def test_client_has_no_order_or_write_methods():
    names = [n for n, _ in inspect.getmembers(AlpacaReadOnly, inspect.isfunction)]
    assert not [n for n in names if any(w in n.lower() for w in ('order', 'submit', 'cancel', 'place', 'buy', 'sell', 'close_position', 'liquidate'))]
    source = inspect.getsource(AlpacaReadOnly)
    assert '.post(' not in source and '.delete(' not in source and '.put(' not in source


def test_positions_are_parsed_to_numbers():
    s = FakeSession([Resp(200, [dict(symbol='NVDA', qty='2', market_value='300.5', cost_basis='250', avg_entry_price='125', current_price='150.25')])])
    p = AlpacaReadOnly('k', 's', session=s).positions()
    assert p[0]['market_value'] == 300.5 and p[0]['qty'] == 2.0


def test_bars_paginate_and_chunk():
    bar = lambda t, c: dict(t=t, o=c, h=c, l=c, c=c, v=1000)
    s = FakeSession([Resp(200, dict(bars=dict(AAA=[bar('2026-01-02T05:00:00Z', 10)]), next_page_token='x')),
                     Resp(200, dict(bars=dict(AAA=[bar('2026-01-05T05:00:00Z', 11)]), next_page_token=None))])
    df = AlpacaReadOnly('k', 's', session=s).daily_bars(['aaa'], '2026-01-01')
    assert list(df.close) == [10, 11] and len(s.calls) == 2 and s.calls[1][1]['page_token'] == 'x'


def test_http_errors_raise_and_retry_on_429(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda *_: None)
    ok = AlpacaReadOnly('k', 's', session=FakeSession([Resp(429, 'slow'), Resp(200, dict(cash='1'))]))
    assert ok.account()['cash'] == '1'
    with pytest.raises(AlpacaError):
        AlpacaReadOnly('k', 's', session=FakeSession([Resp(403, 'forbidden')])).account()


def test_sip_requests_stay_16_minutes_behind_so_free_accounts_can_use_them(monkeypatch):
    from datetime import datetime, timedelta, timezone

    seen = []

    def fake_get(self, base, path, params=None):
        seen.append(dict(params or {}))
        return {'bars': {}, 'next_page_token': None}
    monkeypatch.setattr(AlpacaReadOnly, '_get', fake_get)
    AlpacaReadOnly('k', 's', feed='sip').daily_bars(['AAA'], '2026-01-01')
    end = datetime.strptime(seen[0]['end'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    assert timedelta(minutes=15) < datetime.now(timezone.utc) - end < timedelta(minutes=18) and seen[0]['feed'] == 'sip'
    AlpacaReadOnly('k', 's', feed='iex').daily_bars(['AAA'], '2026-01-01')
    assert 'end' not in seen[1]                                                                 # the free IEX feed needs no end time


@pytest.mark.parametrize('end,expected', [
    ('2026-10-21T00:00:00Z', '2026-10-20T21:19:00Z'),
    ('2026-10-20T21:30:00Z', '2026-10-20T21:19:00Z'),
    ('2026-10-20T21:19:00Z', '2026-10-20T21:19:00Z'),
    ('2026-10-19T00:00:00Z', '2026-10-19T00:00:00Z'),
    ('2026-10-21', '2026-10-20T21:19:00Z'),
    ('2026-10-20T23:30:00+02:00', '2026-10-20T21:19:00Z'),
])
def test_explicit_sip_end_obeys_delay_on_every_page(monkeypatch, end, expected):
    from datetime import datetime, timezone
    from quant_strategy import alpaca_client

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 20, 21, 35, tzinfo=timezone.utc)

    monkeypatch.setattr(alpaca_client, 'datetime', Clock)
    s = FakeSession([Resp(200, {'bars': {}, 'next_page_token': 'next'}),
                     Resp(200, {'bars': {}, 'next_page_token': None})])
    AlpacaReadOnly('k', 's', feed='sip', session=s).daily_bars(['QQQ'], '2014-01-01', end=end)
    assert [params['end'] for _, params in s.calls] == [expected, expected]
    assert all(params['feed'] == 'sip' for _, params in s.calls)


def test_explicit_iex_end_is_preserved():
    s = FakeSession([Resp(200, {'bars': {}})])
    end = '2099-01-01T00:00:00Z'
    AlpacaReadOnly('k', 's', feed='iex', session=s).daily_bars(['QQQ'], '2014-01-01', end=end)
    assert s.calls[0][1]['end'] == end
