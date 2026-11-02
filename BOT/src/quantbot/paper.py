"""Owner-only Strategy 2 paper trading.

Alpaca paper supplies simulated fills; this ledger is the source of truth at the owner's
real scale (the Alpaca paper account keeps its own $100k balance and pays no dividends).
Orders are exactly the S2 live suggestions, are sent only to the paper endpoint and only
after the account number proves it is a paper account. A sale never exceeds the paper
position, so the strategy can never open a short.
"""
import asyncio
import logging
import os
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from html import escape
from time import sleep
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from telegram.constants import ParseMode
from telegram.error import TelegramError

from . import fx
from .db import transaction
from .models import PaperAccount, PaperEvent

log = logging.getLogger(__name__)

PAPER_URL = 'https://paper-api.alpaca.markets'  # never configurable: orders must not reach a live account
DATA_URL = 'https://data.alpaca.markets'
NY = ZoneInfo('America/New_York')
BENCHMARK = 'QQQM'               # same index as the QQQ signal, priced for whole-share DCA
FX_FEE_USD = Decimal(2)        # IBKR manual spot FX minimum per conversion
WITHHOLDING = Decimal('0.15')    # US dividend withholding for an Australian W-8BEN holder
PRICE_GUARD = (Decimal('0.58'), Decimal('1.75'))  # unexplained one-session jump = missed split
TERMINAL = {'filled', 'canceled', 'expired', 'rejected'}
OPEN_ORDER = {'planned', 'submitted'}
STEP = Decimal('0.000001')
OWNER_MENU = [('paper', '模拟盘：S2 Alpaca 模拟账户状态'), ('paper_start', '开始模拟盘'),
              ('paper_deposit', '模拟盘追加入金'), ('paper_monthly', '修改模拟盘每月入金'),
              ('paper_stop', '停止模拟盘')]
COMMANDS = {'/' + command for command, _ in OWNER_MENU} | {'/paper_resume'}


class PaperError(RuntimeError):
    def __init__(self, message, *, definite=True):
        super().__init__(message)
        self.definite = definite  # False: the request may have reached Alpaca


class PaperBroker:
    """Minimal Alpaca client bound to the paper endpoint."""

    def __init__(self, key, secret, session=None, retries=3):
        import requests
        self.session = session or requests.Session()
        self.headers = {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret}
        self.retries = retries

    def _call(self, method, url, *, params=None, body=None, missing_ok=False):
        import requests
        last = None
        # A POST is never repeated blindly; the caller resolves it by client order ID.
        for attempt in range(self.retries if method == 'GET' else 1):
            try:
                response = self.session.request(method, url, headers=self.headers, params=params,
                                                json=body, timeout=30)
            except requests.RequestException as exc:
                last = type(exc).__name__
                if method == 'GET':
                    sleep(1 + attempt)
                continue
            if missing_ok and response.status_code == 404:
                return None
            if method == 'GET' and (response.status_code == 429 or response.status_code >= 500):
                last = f'HTTP {response.status_code}'
                sleep(1 + attempt)
                continue
            if response.status_code >= 500:
                raise PaperError(f'Alpaca HTTP {response.status_code}', definite=False)
            if response.status_code not in (200, 201):
                raise PaperError(f'Alpaca HTTP {response.status_code}: {response.text[:160]}')
            return response.json()
        raise PaperError(f'Alpaca 请求失败（{last}）', definite=False)

    def require_paper(self):
        account = self._call('GET', PAPER_URL + '/v2/account')
        if (not str(account.get('account_number', '')).startswith('PA') or account.get('status') != 'ACTIVE'
                or account.get('trading_blocked') or account.get('account_blocked')):
            raise PaperError('Alpaca 账户不是可交易的模拟账户，已停止下单。')
        return account

    def available(self, symbol):
        row = self._call('GET', f'{PAPER_URL}/v2/positions/{symbol}', missing_ok=True)
        if not row:
            return Decimal(0)
        return max(_d(row.get('qty_available', row.get('qty', 0))), Decimal(0))  # a short is not available

    def order(self, client_order_id):
        return self._call('GET', PAPER_URL + '/v2/orders:by_client_order_id',
                          params={'client_order_id': client_order_id}, missing_ok=True)

    def submit(self, symbol, side, qty, client_order_id):
        return self._call('POST', PAPER_URL + '/v2/orders', body=dict(
            symbol=symbol, qty=_text(qty), side=side, type='market', time_in_force='day',
            client_order_id=client_order_id))

    def clock(self):
        return self._call('GET', PAPER_URL + '/v2/clock')

    def corporate_actions(self, symbols, start, end):
        out, token = {}, None
        for _ in range(20):
            params = dict(symbols=','.join(sorted(symbols)), types='cash_dividend,forward_split,reverse_split',
                          start=start.isoformat(), end=end.isoformat(), limit=1000)
            if token:
                params['page_token'] = token
            data = self._call('GET', DATA_URL + '/v1/corporate-actions', params=params)
            for kind, rows in (data.get('corporate_actions') or {}).items():
                out.setdefault(kind, []).extend(rows)
            token = data.get('next_page_token')
            if not token:
                break
        return out


def _d(value):
    return Decimal(str(value))


def _text(value):
    return format(_d(value).quantize(STEP, rounding=ROUND_DOWN).normalize(), 'f')


def holdings_of(account):
    return {t: _d(q) for t, q in (account.holdings or {}).items() if _d(q) > 0}


def _store(account, holdings):
    account.holdings = {t: _text(q) for t, q in sorted(holdings.items()) if q > 0}


def _event(session, account_id, key):
    return session.scalar(select(PaperEvent).where(PaperEvent.account_id == account_id, PaperEvent.key == key))


def _add(session, account_id, key, kind, day, data):
    if _event(session, account_id, key):
        return None
    row = PaperEvent(account_id=account_id, key=key, kind=kind, session_date=day, data=data)
    session.add(row)
    session.flush()
    return row


def _update(row, **changes):
    row.data = {**row.data, **changes}  # reassign so the JSON change is persisted


def _events(session, account_id, kind):
    return list(session.scalars(select(PaperEvent).where(PaperEvent.account_id == account_id,
                PaperEvent.kind == kind).order_by(PaperEvent.session_date, PaperEvent.id)))


def _latest_mark(session, account_id, *, before=None, valued=False):
    query = select(PaperEvent).where(PaperEvent.account_id == account_id, PaperEvent.kind == 'mark')
    if before:
        query = query.where(PaperEvent.session_date < before)
    marks = session.scalars(query.order_by(PaperEvent.session_date.desc()))
    return next((m for m in marks if not valued or m.data.get('unit') is not None), None)


def _last_prices(session, account):
    mark = _latest_mark(session, account.id)
    return mark.data['prices'] if mark else {}


def active_account(session):
    return session.scalar(select(PaperAccount).where(PaperAccount.active))


def ny_today(now=None):
    return (now or datetime.now(UTC)).astimezone(NY).date()


# ------------------------------------------------------------------ owner actions

def start_account(session, chat_id, amount, currency, monthly_aud, today):
    if active_account(session):
        raise ValueError('已有运行中的模拟盘；先 /paper_stop 再重新开始。')
    _check_monthly(monthly_aud)
    account = PaperAccount(id=uuid.uuid4().hex, started_on=today, active=True, chat_id=chat_id,
                           cash_usd=Decimal(0), monthly_aud=monthly_aud, holdings={},
                           benchmark_cash_usd=Decimal(0), benchmark_shares=Decimal(0), units=Decimal(0),
                           benchmark_units=Decimal(0), deposits_aud=Decimal(0), deposits_usd=Decimal(0),
                           fees_usd=Decimal(0), dividends_usd=Decimal(0), peak_nav_per_unit=Decimal(1),
                           max_drawdown=Decimal(0), benchmark_peak=Decimal(1), benchmark_max_drawdown=Decimal(0))
    session.add(account)
    session.flush()
    queue_deposit(session, account, amount, currency, today, key='deposit:initial')
    return account


def _check_monthly(amount):
    if amount and amount <= FX_FEE_USD * 2:
        raise ValueError('每月入金太小，不足以覆盖换汇费用；0AUD 表示不自动入金。')


def queue_deposit(session, account, amount, currency, today, key=None):
    if currency not in {'AUD', 'USD'} or not amount.is_finite() or amount <= 0:
        raise ValueError('入金须为大于 0 的 AUD 或 USD 金额。')
    if currency == 'AUD' and amount <= FX_FEE_USD * 2:
        raise ValueError('AUD 入金太小，不足以覆盖换汇费用。')
    return _add(session, account.id, key or f'deposit:{uuid.uuid4().hex}', 'deposit', today,
                {'amount': str(amount), 'currency': currency, 'applied': False})


# ------------------------------------------------------------------ ledger steps

def _commission(qty, price):
    from sgs.live import commission
    return _d(commission(float(qty), float(price))) if qty else Decimal(0)


def _apply_fill(account, side, symbol, qty, price):
    holdings = holdings_of(account)
    fee = _commission(qty, price)
    sign = 1 if side == 'buy' else -1
    holdings[symbol] = holdings.get(symbol, Decimal(0)) + sign * qty
    account.cash_usd = _d(account.cash_usd) - sign * qty * price - fee
    account.fees_usd = _d(account.fees_usd) + fee
    _store(account, holdings)
    return fee


def _position_before(session, account, symbol, day, book):
    """Shares held at the close before `day`: replay later fills and applied splits backwards."""
    if book == 'bench':
        shares = _d(account.benchmark_shares) if symbol == BENCHMARK else Decimal(0)
        fills = [(e.session_date, _d(e.data.get('qty', 0))) for e in _events(session, account.id, 'bench')
                 if e.data.get('done') and symbol == BENCHMARK]
    else:
        shares = holdings_of(account).get(symbol, Decimal(0))
        fills = [(e.session_date, _d(e.data['filled']) * (1 if e.data['side'] == 'buy' else -1))
                 for e in _events(session, account.id, 'order')
                 if e.data.get('status') == 'done' and e.data['symbol'] == symbol and _d(e.data.get('filled', 0))]
    splits = [(e.session_date, _d(e.data['factor'])) for e in _events(session, account.id, 'split')
              if e.data['symbol'] == symbol]
    # Same session: the split takes effect at the open, before that day's fills.
    steps = [(d, 0, factor) for d, factor in splits] + [(d, 1, qty) for d, qty in fills]
    for when, order, value in sorted(steps, reverse=True):
        if when < day:
            break
        shares = shares - value if order else shares / value
    return shares


def _parse_splits(actions, started):
    out = []
    for kind in ('forward_splits', 'reverse_splits'):
        for row in actions.get(kind, []):
            ex = date.fromisoformat(row['ex_date'])
            if ex >= started and row.get('old_rate') and row.get('new_rate'):
                out.append((row['id'], row['symbol'], ex, _d(row['new_rate']) / _d(row['old_rate'])))
    return out


def _apply_split(session, account, split):
    """Only shares held through the split change; a late report keeps later fills intact."""
    split_id, symbol, ex, factor = split
    if _event(session, account.id, f'split:{split_id}'):
        return False
    held = _position_before(session, account, symbol, ex, 's2')
    bench = _position_before(session, account, symbol, ex, 'bench')
    _add(session, account.id, f'split:{split_id}', 'split', ex, {'symbol': symbol, 'factor': str(factor)})
    holdings = holdings_of(account)
    if held:
        holdings[symbol] = holdings.get(symbol, Decimal(0)) + held * (factor - 1)
        _store(account, holdings)
    if bench:
        account.benchmark_shares = _d(account.benchmark_shares) + bench * (factor - 1)
    for hold in _events(session, account.id, 'hold'):
        if hold.data['symbol'] == symbol and not hold.data.get('resolved') and ex <= hold.session_date:
            _update(hold, resolved=True, by=f'split:{split_id}')
    return True


def _value(account, prices):
    holdings = holdings_of(account)
    nav = _d(account.cash_usd) + sum(q * _d(prices[t]) for t, q in holdings.items())
    bench = _d(account.benchmark_cash_usd) + _d(account.benchmark_shares) * _d(prices[BENCHMARK])
    return nav, bench


def _mark(session, account, as_of, prices, *, track=True):
    """Record the close. An unexplained price jump must not become a permanent drawdown."""
    nav, bench = _value(account, prices)
    unit = nav / _d(account.units) if account.units else None
    bench_unit = bench / _d(account.benchmark_units) if account.benchmark_units else None
    if unit is not None and track:
        account.peak_nav_per_unit = max(_d(account.peak_nav_per_unit), unit)
        account.max_drawdown = min(_d(account.max_drawdown), unit / _d(account.peak_nav_per_unit) - 1)
    if bench_unit is not None and track:
        account.benchmark_peak = max(_d(account.benchmark_peak), bench_unit)
        account.benchmark_max_drawdown = min(_d(account.benchmark_max_drawdown),
                                             bench_unit / _d(account.benchmark_peak) - 1)
    account.last_valued_on = as_of
    _add(session, account.id, f'mark:{as_of}', 'mark', as_of, {
        'nav': str(nav), 'bench_nav': str(bench), 'unit': str(unit) if unit is not None else None,
        'bench_unit': str(bench_unit) if bench_unit is not None else None, 'cash': str(account.cash_usd),
        'holdings': dict(account.holdings), 'bench_shares': str(account.benchmark_shares),
        'prices': {t: str(p) for t, p in sorted(prices.items())}, 'tracked': track})


def _flag_anomalies(session, account, previous, prices, splits):
    """Hold trading on a price jump since the last mark that no reported split explains."""
    last = _latest_mark(session, account.id, before=previous)
    if last is None:
        return []
    held = set(holdings_of(account)) | ({BENCHMARK} if _d(account.benchmark_shares) else set())
    issues = []
    for symbol in sorted(held):
        then = last.data['prices'].get(symbol)
        if not then or symbol not in prices:
            continue
        factor = Decimal(1)
        for _, split_symbol, ex, split_factor in splits:
            if split_symbol == symbol and last.session_date < ex <= previous:
                factor *= split_factor
        ratio = _d(prices[symbol]) / _d(then) * factor
        if not PRICE_GUARD[0] <= ratio <= PRICE_GUARD[1]:
            text = (f'{symbol} 收盘价变化异常（{_d(then):.2f} → {_d(prices[symbol]):.2f}），疑似未登记的拆股；'
                    '暂停下单，待 Alpaca 登记拆股后自动恢复。若确认是真实涨跌，发送 /paper_resume。')
            if _add(session, account.id, f'hold:{symbol}:{previous}', 'hold', previous,
                    {'symbol': symbol, 'resolved': False, 'text': text}):
                issues.append(text)
    return issues


def _credit_dividends(session, account, actions, today):
    notes = []
    for row in actions.get('cash_dividends', []):
        ex = date.fromisoformat(row['ex_date'])
        paid = date.fromisoformat(row.get('payable_date') or row['process_date'])
        if ex < account.started_on or paid > today or _event(session, account.id, f"dividend:{row['id']}"):
            continue
        rate = _d(row['rate'])
        shares = _position_before(session, account, row['symbol'], ex, 's2')
        bench = _position_before(session, account, row['symbol'], ex, 'bench')
        net, bench_net = rate * shares * (1 - WITHHOLDING), rate * bench * (1 - WITHHOLDING)
        _add(session, account.id, f"dividend:{row['id']}", 'dividend', paid, {
            'symbol': row['symbol'], 'ex_date': ex.isoformat(), 'rate': str(rate), 'shares': str(shares),
            'net': str(net), 'bench_shares': str(bench), 'bench_net': str(bench_net)})
        account.cash_usd = _d(account.cash_usd) + net
        account.dividends_usd = _d(account.dividends_usd) + net
        account.benchmark_cash_usd = _d(account.benchmark_cash_usd) + bench_net
        if net:
            notes.append(f"{row['symbol']} ${net:.2f}")
    return notes


def _apply_deposits(session, account, prices, today):
    notes, failure = [], None
    nav, bench = _value(account, prices)
    for event in _events(session, account.id, 'deposit'):
        if event.data.get('applied') or event.session_date > today:
            continue
        amount, currency = _d(event.data['amount']), event.data['currency']
        try:
            rate = fx.get_quote(session).rate if currency == 'AUD' else None
        except ValueError as exc:
            failure = str(exc)
            continue
        usd = amount * rate - FX_FEE_USD if rate else amount
        unit = nav / _d(account.units) if account.units else Decimal(1)
        bench_unit = bench / _d(account.benchmark_units) if account.benchmark_units else Decimal(1)
        account.units = _d(account.units) + usd / unit
        account.benchmark_units = _d(account.benchmark_units) + usd / bench_unit
        account.cash_usd = _d(account.cash_usd) + usd
        account.benchmark_cash_usd = _d(account.benchmark_cash_usd) + usd
        account.deposits_usd = _d(account.deposits_usd) + usd
        if currency == 'AUD':
            account.deposits_aud = _d(account.deposits_aud) + amount
        nav, bench = nav + usd, bench + usd
        _update(event, applied=True, applied_on=today.isoformat(), usd=str(usd),
                rate=str(rate) if rate else None)
        notes.append(f'{amount:,.2f} {currency} → ${usd:,.2f}' +
                     (f'（1 AUD = {rate:.4f} USD，扣换汇费 ${FX_FEE_USD}）' if rate else ''))
    return notes, failure


def _bench_open(market, day):
    end = (day + timedelta(days=1)).isoformat() + 'T00:00:00Z'
    bars = market.daily_bars([BENCHMARK], day.isoformat(), end=end, adjustment='raw')
    if bars.empty:
        return None
    days = pd.to_datetime(bars['date'], utc=True).dt.tz_localize(None).dt.normalize()
    bars = bars[days == pd.Timestamp(day)]
    return None if bars.empty else _d(bars.iloc[-1]['open'])


def _buy_benchmark(account, event, price):
    """Whole-share DCA at the session open under the same Tiered cost budget as S2."""
    spend = min(_d(event.data['cash']), _d(account.benchmark_cash_usd))
    qty = int(max(spend, Decimal(0)) / price)
    while qty and qty * price + _commission(qty, price) > spend:
        qty -= 1
    fee = _commission(qty, price)
    account.benchmark_shares = _d(account.benchmark_shares) + qty
    account.benchmark_cash_usd = _d(account.benchmark_cash_usd) - qty * price - fee
    _update(event, done=True, qty=str(qty), price=str(price), fee=str(fee))


# ------------------------------------------------------------------ broker sync

def reconcile(factory, broker, account_id):
    with transaction(factory) as session:
        pending = [(e.id, dict(e.data)) for e in _events(session, account_id, 'order')
                   if e.data.get('status') == 'submitted']
    lines = []
    for event_id, data in pending:
        order = broker.order(data['cid'])
        if order is None or order.get('status') not in TERMINAL:
            continue
        filled, price = _d(order.get('filled_qty') or 0), _d(order.get('filled_avg_price') or 0)
        with transaction(factory) as session:
            event, account = session.get(PaperEvent, event_id), session.get(PaperAccount, account_id)
            if event.data.get('status') != 'submitted':
                continue
            fee = _apply_fill(account, data['side'], data['symbol'], filled, price) if filled else Decimal(0)
            _update(event, status='done', broker_status=order['status'], filled=str(filled),
                    price=str(price), fee=str(fee))
            cash = _d(account.cash_usd)
        verb = '买入' if data['side'] == 'buy' else '卖出'
        if filled:
            partial = '（部分成交）' if filled < _d(data['qty']) else ''
            lines.append(f"{verb} {escape(data['symbol'])} {_text(filled)} 股{partial}，均价 ${price:.2f}，"
                         f'估算手续费 ${fee:.2f}；现金 ${cash:,.2f}')
        else:
            lines.append(f"{verb} {escape(data['symbol'])} 未成交（{escape(order['status'])}），下个交易日重新计算。")
    return lines


def submit_planned(factory, broker, account_id, day):
    with transaction(factory) as session:
        planned = [(e.id, dict(e.data)) for e in _events(session, account_id, 'order')
                   if e.session_date == day and e.data.get('status') == 'planned']
        account = session.get(PaperAccount, account_id)
        prices = {**_last_prices(session, account), **{d['symbol']: d['ref'] for _, d in planned}}
        nav = _d(account.cash_usd) + sum(q * _d(prices.get(t, 0)) for t, q in holdings_of(account).items())
        cap = max(nav, _d(account.deposits_usd)) * Decimal('1.5') + 50  # blocks any order far beyond the ledger
    results = []
    for event_id, data in planned:
        qty, status, extra = _d(data['qty']), 'submitted', {}
        try:
            existing = broker.order(data['cid'])
            if existing is None:
                if data['side'] == 'sell':
                    available = broker.available(data['symbol']).quantize(STEP, rounding=ROUND_DOWN)
                    if available < qty:
                        extra['capped_from'] = str(qty)
                        qty = available
                if qty <= 0:
                    status, extra['reason'] = 'skipped', '模拟账户持仓不足，未卖出（不会做空）'
                elif qty * _d(data['ref']) > cap:
                    status, extra['reason'] = 'skipped', '订单金额超过账户规模，已拦截'
                else:
                    existing = broker.submit(data['symbol'], data['side'], qty, data['cid'])
            if existing:
                extra['alpaca_id'] = existing.get('id')
                qty = _d(existing.get('qty') or qty)
        except PaperError as exc:
            if not exc.definite:
                results.append((data, 'unknown', str(exc)))
                continue  # stays planned; a later tick looks it up by client order ID
            status, extra['reason'] = 'failed', str(exc)
            if 'client_order_id' in str(exc) or 'unique' in str(exc).lower():
                extra['attempt'] = 1  # the first request may exist; a retry could double it
        with transaction(factory) as session:
            _update(session.get(PaperEvent, event_id), status=status, qty=str(qty), **extra)
        results.append(({**data, 'qty': str(qty)}, status, extra.get('reason')))
    return results


def resolve_planned(factory, broker, account_id, now):
    """Finish orders whose submission outcome is unknown; never place one after its session."""
    today = ny_today(now)
    with transaction(factory) as session:
        planned = [(e.id, e.session_date, dict(e.data)) for e in _events(session, account_id, 'order')
                   if e.data.get('status') == 'planned']
    days = set()
    for event_id, day, data in planned:
        if day > today or day == today and now.astimezone(NY).hour < 16:
            days.add(day)
            continue
        existing = broker.order(data['cid'])
        with transaction(factory) as session:
            event = session.get(PaperEvent, event_id)
            if existing:
                _update(event, status='submitted', alpaca_id=existing.get('id'))
            else:
                _update(event, status='expired', reason='提交结果未知且交易日已过，未下单')
    results = []
    for day in sorted(days):
        results += submit_planned(factory, broker, account_id, day)
    return results


def retry_failed(factory, broker, account_id, now):
    """Retry a definite rejection once during regular hours (e.g. a queued fractional order)."""
    today = ny_today(now)
    with transaction(factory) as session:
        if not [e for e in _events(session, account_id, 'order') if e.session_date == today
                and e.data.get('status') == 'failed' and e.data.get('attempt', 0) < 1]:
            return []
    if not broker.clock().get('is_open'):
        return []
    with transaction(factory) as session:
        for row in _events(session, account_id, 'order'):
            if row.session_date == today and row.data.get('status') == 'failed' and row.data.get('attempt', 0) < 1:
                _update(row, status='planned', attempt=1, cid=row.data['cid'] + '-r1')
    return submit_planned(factory, broker, account_id, today)


def _order_lines(results, label):
    states = {'submitted': '已提交', 'skipped': '未提交', 'failed': '被拒绝，开盘后重试一次', 'unknown': '提交结果待确认'}
    lines = []
    for order, status, reason in results:
        verb = '买入' if order['side'] == 'buy' else '卖出'
        lines.append(f"{label}{verb} {escape(order['symbol'])} {_text(order['qty'])} 股"
                     f"（参考 ${_d(order['ref']):.2f}）{states[status]}" + (f'：{escape(reason)}' if reason else ''))
    return lines


# ------------------------------------------------------------------ premarket session

def _alert_once(factory, account_id, day, code, text):
    with transaction(factory) as session:
        if _add(session, account_id, f'alert:{day}:{code}', 'alert', day, {'text': text}):
            return text
    return None


def premarket(factory, market, broker, account_id, now, tz, hour, snapshot_fn):
    from sgs.live import TICKERS, recommendations

    from .daily_service import calendar_window
    window = calendar_window(market, now, tz, hour)
    if window is None:
        return None
    today, previous, days, _ = window
    with transaction(factory) as session:
        account = session.get(PaperAccount, account_id)
        if not account or not account.active or account.last_session == today:
            return None
        stuck = [e.key for e in _events(session, account_id, 'order')
                 if e.session_date < today and e.data.get('status') in OPEN_ORDER]
        bench_pending = [(e.id, e.session_date) for e in _events(session, account_id, 'bench')
                         if not e.data.get('done') and e.session_date < today]
        started, valued = account.started_on, account.last_valued_on
        first_run = account.last_session is None
    broker.require_paper()
    sessions = sorted(date.fromisoformat(r['date']) for r in market.calendar(previous - timedelta(days=20), previous))
    before_previous = sessions[-2] if len(sessions) > 1 else None
    opens = {day: _bench_open(market, day) for _, day in bench_pending}
    actions = broker.corporate_actions(set(TICKERS) | {BENCHMARK}, (valued or started) - timedelta(days=40),
                                       today + timedelta(days=7))
    data = snapshot_fn(factory, market, today)
    if data.get('as_of') != previous.isoformat():
        raise PaperError('S2 行情尚未更新到上一交易日。', definite=False)
    prices = data['prices']
    splits = _parse_splits(actions, started)
    with transaction(factory) as session:
        account = session.get(PaperAccount, account_id)
        if account.last_session == today:
            return None
        alerts = []
        for event_id, day in bench_pending:
            if opens.get(day):
                _buy_benchmark(account, session.get(PaperEvent, event_id), opens[day])
        for split in splits:
            if split[2] <= previous and _apply_split(session, account, split) and split[2] < previous:
                alerts.append(f'{split[1]} 拆股（{split[2]}）登记较晚，已按除权日补记。')
        alerts += _flag_anomalies(session, account, previous, prices, splits)
        held_up = any(not h.data.get('resolved') for h in _events(session, account_id, 'hold'))
        _mark(session, account, previous, prices, track=not held_up)
        split_today = [s[1] for s in splits if s[2] == today and _apply_split(session, account, s)]
        dividends = _credit_dividends(session, account, actions, today)
        if account.monthly_aud and (today.year, today.month) > (started.year, started.month):
            queue_deposit(session, account, _d(account.monthly_aud), 'AUD', days[0],
                          key=f'deposit:monthly:{today:%Y-%m}')  # from the month after the start
        deposits, fx_failure = [], None
        if held_up:
            alerts.append('入金待价格异常解除后按正确净值入账。')
        else:
            deposits, fx_failure = _apply_deposits(session, account, prices, today)
        if fx_failure:
            alerts.append('汇率暂不可用，入金顺延：' + fx_failure)
        if _d(account.benchmark_cash_usd) >= _d(prices[BENCHMARK]) * Decimal('1.01') + 1:
            _add(session, account.id, f'bench:{today}', 'bench', today,
                 {'cash': str(account.benchmark_cash_usd), 'done': False})
        halted = []
        if stuck:
            halted.append('上一交易日订单尚未确认结果')
        if split_today and set(split_today) & (set(holdings_of(account)) | set(data['weights'])):
            halted.append('今日拆股（' + '、'.join(split_today) + '），参考价未更新')
        if held_up:
            halted.append('价格异常待核对（见下方提示）')
        planned = 0
        if not halted:
            for action in recommendations(data, holdings_of(account), max(_d(account.cash_usd), Decimal(0))):
                side = action['action'].lower()
                cid = f"s2p-{account.id[:8]}-{today:%Y%m%d}-{action['symbol']}-{side}"
                planned += bool(_add(session, account.id, f"order:{today}:{action['symbol']}:{side}", 'order', today, {
                    'symbol': action['symbol'], 'side': side, 'qty': str(action['shares']),
                    'ref': str(action['reference_price']), 'status': 'planned', 'attempt': 0, 'cid': cid}))
        account.last_session = today
        nav, bench = _value(account, prices)
        view = dict(nav=nav, bench=bench, cash=_d(account.cash_usd),
                    unit=nav / _d(account.units) if account.units else None,
                    bench_unit=bench / _d(account.benchmark_units) if account.benchmark_units else None,
                    max_dd=_d(account.max_drawdown), bench_dd=_d(account.benchmark_max_drawdown))
        chat_id = account.chat_id
    results = submit_planned(factory, broker, account_id, today) if planned else []
    weekly = before_previous is None or previous.isocalendar()[1] != today.isocalendar()[1]
    if not (first_run or results or deposits or dividends or alerts or halted or weekly):
        return None
    return chat_id, _premarket_text(today, data, results, halted, deposits, dividends, alerts, view)


def _premarket_text(today, data, results, halted, deposits, dividends, alerts, view):
    from sgs.live import weights_text
    weights = escape(weights_text(data['weights']))
    lines = [f'🧪 <b>S2 模拟盘 · {today} 盘前</b>',
             f'行情截至 {data["as_of"]} · 趋势 <b>{escape(data["trend"])}</b> · 目标 {weights}', '',
             '<b>订单</b>（开盘市价，Alpaca 模拟成交）']
    lines += _order_lines(results, '')
    if halted:
        lines.append('今日暂停下单：' + escape('；'.join(halted)) + '。')
    elif not results:
        lines.append('今日无订单：持仓偏离未达调仓条件，或资金／整股／手续费门槛未满足。')
    lines += ['', f'<b>账户</b>（{data["as_of"]} 收盘估值，含今日入金）',
              f'S2 ${view["nav"]:,.2f}（现金 ${view["cash"]:,.2f}） · {BENCHMARK} 定投 ${view["bench"]:,.2f}']
    if view['unit'] is not None and view['bench_unit'] is not None:
        lines.append(f'时间加权收益 S2 {view["unit"] - 1:+.2%} / {BENCHMARK} {view["bench_unit"] - 1:+.2%} · '
                     f'最大回撤 S2 {view["max_dd"]:.2%} / {BENCHMARK} {view["bench_dd"]:.2%}')
    if view['cash'] < 0:
        lines.append('现金为负：开盘跳空使成交额超过预算，下次入金或卖出时抵扣。')
    if deposits:
        lines.append('入金：' + escape('；'.join(deposits)))
    if dividends:
        lines.append('股息（已扣 15% 预扣税）：' + escape('、'.join(dividends)))
    lines += [f'⚠️ {escape(a)}' for a in alerts]
    lines.append('模拟盘只使用 Alpaca 模拟账户，不会操作 IBKR 实盘。/paper 查看详情。')
    return '\n'.join(lines)


# ------------------------------------------------------------------ job

def _services():
    from . import strategy_service as s1
    settings = s1.strategy_settings()
    market = s1.build_engine(settings).market
    broker = PaperBroker(os.getenv('ALPACA_PAPER_KEY') or settings.alpaca_key,
                         os.getenv('ALPACA_PAPER_SECRET') or settings.alpaca_secret)
    return market, broker


def _light(factory, broker, account_id, now):
    lines = _order_lines(resolve_planned(factory, broker, account_id, now), '补交 ')
    lines += reconcile(factory, broker, account_id)
    lines += _order_lines(retry_failed(factory, broker, account_id, now), '重试 ')
    return lines


async def paper_tick(context, now=None, *, services=_services, snapshot_fn=None):
    app = context.application
    if not app.bot_data.get('paper_trading'):
        return
    factory = app.bot_data['session_factory']
    now = now or datetime.now(UTC)
    with transaction(factory) as session:
        account = active_account(session)
        if account is None:
            return
        account_id, chat_id, last_session = account.id, account.chat_id, account.last_session
    messages = []
    try:
        market, broker = await asyncio.to_thread(services)
        lines = await asyncio.to_thread(_light, factory, broker, account_id, now)
        if lines:
            messages.append((chat_id, '🧪 <b>S2 模拟盘订单更新</b>\n' + '\n'.join(lines)))
        tz, hour = app.bot_data.get('strategy_push', ('Asia/Shanghai', 10))
        local = now.astimezone(ZoneInfo(tz))
        if local.hour >= hour and last_session != local.date():
            if snapshot_fn is None:
                from .daily_service import s2_snapshot as snapshot_fn
            lock = app.bot_data.setdefault('daily_strategy_lock', asyncio.Lock())
            async with lock:  # shares the S2 snapshot cache with the daily reports
                try:
                    result = await asyncio.to_thread(premarket, factory, market, broker, account_id,
                                                     now, tz, hour, snapshot_fn)
                except (PaperError, ValueError) as exc:
                    text = _alert_once(factory, account_id, local.date(), type(exc).__name__,
                                       f'🧪 S2 模拟盘今日暂未处理（盘前会重试）：{exc}')
                    result = (chat_id, escape(text)) if text else None
            if result:
                messages.append(result)
    except Exception:
        log.exception('paper trading tick failed')
    for target, text in messages:
        try:
            await app.bot.send_message(target, text, parse_mode=ParseMode.HTML)
        except TelegramError:
            log.warning('paper trading message not delivered', exc_info=True)


# ------------------------------------------------------------------ status and commands

def status_text(session, account, enabled):
    mark = _latest_mark(session, account.id, valued=True)
    orders = _events(session, account.id, 'order')
    lines = ['🧪 <b>S2 模拟盘（仅 Bot Owner）</b>',
             f'开始：{account.started_on} · 每月入金：{_d(account.monthly_aud):,.0f} AUD（每月首个交易日）']
    if not enabled:
        lines.append('⚠️ 服务器未开启 PAPER_TRADING=1：账户已记录，但不会自动入金或下单。')
    if mark is None and account.last_session is None:
        lines.append('等待第一次盘前处理：交易日盘前自动入金，并按 S2 建议向 Alpaca 模拟账户下单。')
    elif mark is None:
        lines.append(f'{account.last_session} 已入金并提交首批订单；收益与回撤从下一个交易日盘前开始统计。')
    else:
        data, deposits = mark.data, _d(account.deposits_usd)
        nav, bench = _d(data['nav']), _d(data['bench_nav'])
        unit, bench_unit = _d(data['unit']), _d(data['bench_unit'])
        lines += [f'估值日：{mark.session_date} 收盘（不含此后入金）',
                  f'S2：${nav:,.2f} · 时间加权 {unit - 1:+.2%} · 最大回撤 {_d(account.max_drawdown):.2%}',
                  f'{BENCHMARK} 定投：${bench:,.2f} · 时间加权 {bench_unit - 1:+.2%} · '
                  f'最大回撤 {_d(account.benchmark_max_drawdown):.2%}']
        days = (mark.session_date - account.started_on).days
        if days >= 180:
            years = days / 365.25
            lines.append(f'年化：S2 {float(unit) ** (1 / years) - 1:+.1%} / {BENCHMARK} {float(bench_unit) ** (1 / years) - 1:+.1%}')
        lines.append(f'累计入金 ${deposits:,.2f}；S2 盈亏约 ${nav - deposits:+,.2f}，{BENCHMARK} 约 ${bench - deposits:+,.2f}')
    holdings = holdings_of(account)
    open_orders = sum(o.data.get('status') in OPEN_ORDER for o in orders)
    holds = [h.data['text'] for h in _events(session, account.id, 'hold') if not h.data.get('resolved')]
    lines += ['持仓：' + ('、'.join(f'{escape(t)} {_text(q)} 股' for t, q in holdings.items()) or '无'),
              f'现金：${_d(account.cash_usd):,.2f} · {BENCHMARK} 对照 {_text(account.benchmark_shares)} 股 + '
              f'${_d(account.benchmark_cash_usd):,.2f}',
              f'入金：{_d(account.deposits_aud):,.2f} AUD 等 → ${_d(account.deposits_usd):,.2f}（已扣换汇费）',
              f'手续费估算 ${_d(account.fees_usd):,.2f} · 股息（扣 15% 预扣税后）${_d(account.dividends_usd):,.2f}',
              f'成交 {sum(o.data.get("status") == "done" and _d(o.data.get("filled", 0)) > 0 for o in orders)} 笔'
              + (f'，{open_orders} 笔待确认' if open_orders else '')]
    lines += [f'⚠️ {escape(text)}' for text in holds]
    lines += ['', '成交来自 Alpaca 模拟撮合（开盘市价单）；手续费按 IBKR Tiered 估算，每次换汇按 $2，'
              f'股息按 15% 预扣税补记，不含澳洲所得税。{BENCHMARK} 对照与 S2 同时同额入金，在同一交易日开盘整股买入。']
    return '\n'.join(lines)


_MONEY = re.compile(r'(?:A\$|AU\$|US\$|\$)?\s*\d[\d,]*(?:\.\d+)?\s*(?:AUD|USD|澳元|美元)?', re.IGNORECASE)


def parse_amounts(args):
    from .services import parse_money_text
    text = ' '.join(args)
    tokens = [t.strip() for t in _MONEY.findall(text) if t.strip()]
    if not tokens or ''.join(tokens).replace(' ', '') != text.replace(' ', ''):
        raise ValueError('没看懂金额，请写成 900AUD 或 600USD。')
    out = []
    for token in tokens:
        if not re.search(r'AUD|USD|澳元|美元|A\$|AU\$|US\$', token, re.IGNORECASE):
            raise ValueError('请写明币种，例如 900AUD 400AUD。')
        out.append(parse_money_text(token))
    return out


def _is_owner(update, context):
    return context.application.bot_data['accounts'].is_owner(update.effective_user.id)


def _owner_only(handler):
    async def guarded(update, context):
        if not _is_owner(update, context):
            await update.message.reply_text('仅 Bot Owner 可使用模拟盘。')
            return
        await handler(update, context)
    guarded.__name__ = handler.__name__
    return guarded


@_owner_only
async def paper_command(update, context):
    with transaction(context.application.bot_data['session_factory']) as session:
        account = active_account(session)
        text = (status_text(session, account, bool(context.application.bot_data.get('paper_trading'))) if account else
                '🧪 模拟盘未开始。\n用法：<code>/paper_start 900AUD 400AUD</code>\n'
                '第一个金额为初始资金（AUD 或 USD），第二个为每月首个交易日自动入金（AUD，可省略）。')
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@_owner_only
async def paper_start_command(update, context):
    try:
        amounts = parse_amounts(context.args)
        if len(amounts) > 2:
            raise ValueError('最多两个金额：初始资金和每月入金。')
        monthly, monthly_currency = amounts[1] if len(amounts) == 2 else (Decimal(0), 'AUD')
        if monthly_currency != 'AUD':
            raise ValueError('每月入金请用 AUD，例如 400AUD。')
        with transaction(context.application.bot_data['session_factory']) as session:
            start_account(session, update.effective_chat.id, amounts[0][0], amounts[0][1], monthly, ny_today())
    except ValueError as exc:
        await update.message.reply_text(f'{exc}\n用法：/paper_start 900AUD 400AUD')
        return
    enabled = context.application.bot_data.get('paper_trading')
    await update.message.reply_text(
        '🧪 模拟盘已创建。下一个交易日盘前（与每日报告同一时间）自动入金换汇，并按 S2 建议向 Alpaca 模拟账户'
        f'提交开盘市价单，成交后通知你；同时记录同额 {BENCHMARK} 整股定投作对照。'
        + ('' if enabled else '\n⚠️ 服务器尚未开启 PAPER_TRADING=1，开启并重启后才会执行。'))


@_owner_only
async def paper_deposit_command(update, context):
    try:
        amounts = parse_amounts(context.args)
        if len(amounts) != 1:
            raise ValueError('一次只能入金一个金额。')
        with transaction(context.application.bot_data['session_factory']) as session:
            account = active_account(session)
            if account is None:
                raise ValueError('模拟盘未开始，请先 /paper_start。')
            queue_deposit(session, account, amounts[0][0], amounts[0][1], ny_today())
    except ValueError as exc:
        await update.message.reply_text(f'{exc}\n用法：/paper_deposit 300AUD')
        return
    await update.message.reply_text(f'已登记，下一个交易日盘前入账（AUD 按当时汇率换汇并扣 ${FX_FEE_USD}），'
                                    f'S2 与 {BENCHMARK} 对照同时入金。')


@_owner_only
async def paper_monthly_command(update, context):
    try:
        amounts = parse_amounts(context.args)
        if len(amounts) != 1 or amounts[0][1] != 'AUD':
            raise ValueError('请输入一个 AUD 金额；0AUD 表示停止每月入金。')
        _check_monthly(amounts[0][0])
        with transaction(context.application.bot_data['session_factory']) as session:
            account = active_account(session)
            if account is None:
                raise ValueError('模拟盘未开始，请先 /paper_start。')
            account.monthly_aud = amounts[0][0]
    except ValueError as exc:
        await update.message.reply_text(f'{exc}\n用法：/paper_monthly 400AUD')
        return
    await update.message.reply_text(f'每月首个交易日入金改为 {amounts[0][0]:,.2f} AUD。')


@_owner_only
async def paper_resume_command(update, context):
    with transaction(context.application.bot_data['session_factory']) as session:
        account = active_account(session)
        holds = [h for h in _events(session, account.id, 'hold') if not h.data.get('resolved')] if account else []
        for hold in holds:
            _update(hold, resolved=True, by='owner')
    await update.message.reply_text('已确认价格异常为真实涨跌，下个交易日盘前恢复下单。' if holds else '当前没有暂停中的价格异常。')


@_owner_only
async def paper_stop_command(update, context):
    with transaction(context.application.bot_data['session_factory']) as session:
        account = active_account(session)
        if account is not None:
            account.active = False
    await update.message.reply_text(
        '模拟盘已停止，不再入金或下单；历史记录保留。当日已提交的订单仍可能在 Alpaca 成交，'
        'Alpaca 模拟账户中的持仓不会自动卖出。' if account else '模拟盘未在运行。')


HANDLERS = {'paper': paper_command, 'paper_start': paper_start_command, 'paper_deposit': paper_deposit_command,
            'paper_monthly': paper_monthly_command, 'paper_resume': paper_resume_command,
            'paper_stop': paper_stop_command}
