"""Timestamped automatic AUD/USD reference quotes; never performs broker FX."""
import asyncio
import csv
import io
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .models import FxQuote

CACHE_TTL = timedelta(seconds=30)
_lock = threading.RLock()
_failure = None


@dataclass(frozen=True)
class Quote:
    rate: Decimal
    observed_at: datetime
    fetched_at: datetime
    source: str


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def market_closed(now):
    ny = now.astimezone(ZoneInfo('America/New_York'))
    return ny.weekday() == 5 or ny.weekday() == 4 and ny.hour >= 17 or ny.weekday() == 6 and ny.hour < 17


def validate_quote(quote, now):
    if not quote.rate.is_finite() or not 0 < quote.rate <= 10:
        raise ValueError('AUD/USD行情数值无效，已暂停AUD资金建议。')
    stamp = _aware(quote.observed_at)
    max_age = timedelta(hours=72) if market_closed(now) else timedelta(minutes=20 if quote.source == 'yahoo_delayed' else 5)
    if stamp > now + timedelta(seconds=60) or now - stamp > max_age:
        raise ValueError('AUD/USD行情时间异常或已过期，已暂停AUD资金建议；请稍后用 /fx 重试。')
    if market_closed(now):
        ny = now.astimezone(ZoneInfo('America/New_York'))
        friday = ny.date() - timedelta(days=(ny.weekday() - 4) % 7)
        closing = datetime.combine(friday, time(17), tzinfo=ny.tzinfo)
        tolerance = timedelta(minutes=20 if quote.source == 'yahoo_delayed' else 5)
        if stamp < closing - tolerance:
            raise ValueError('休市汇率不是最近周五收盘附近的报价，已暂停AUD资金建议。')
    if _aware(quote.fetched_at) > now + timedelta(seconds=60):
        raise ValueError('汇率缓存时间异常，请稍后用 /fx 重试。')
    return quote


def _json(url):
    try:
        with urlopen(Request(url, headers={'User-Agent': 'Quantbot/1.0', 'Accept': 'application/json'}), timeout=5) as response:
            return json.loads(response.read(200_000))
    except Exception:
        # Do not propagate URL-bearing errors: the URL can contain an API key.
        raise ValueError('AUD/USD行情服务暂不可用，已暂停AUD资金建议；请稍后用 /fx 重试。') from None


def fetch_quote(now=None):
    now = now or datetime.now(UTC)
    provider = os.getenv('FX_PROVIDER', 'truefx').lower()
    try:
        if provider == 'truefx':
            try:
                request = Request('https://webrates.truefx.com/rates/connect.html?f=csv&c=AUD/USD', headers={'User-Agent': 'Quantbot/1.0'})
                with urlopen(request, timeout=5) as response:
                    body = response.read(200_000).decode('utf-8')
            except Exception:
                raise ValueError('AUD/USD行情服务暂不可用，已暂停AUD资金建议；请稍后用 /fx 重试。') from None
            quote = parse_truefx(body, now)
            return validate_quote(quote, now)
        elif provider == 'twelvedata':
            key = os.getenv('TWELVE_DATA_API_KEY', '').strip()
            if not key:
                raise ValueError('实时AUD/USD行情源尚未配置，请管理员设置TWELVE_DATA_API_KEY；USD资金不受影响。')
            data = _json('https://api.twelvedata.com/exchange_rate?' + urlencode({'symbol': 'AUD/USD', 'apikey': key}))
            if data.get('symbol') != 'AUD/USD' or data.get('status') == 'error':
                raise ValueError('实时AUD/USD行情请求未成功，请管理员检查行情权限或额度。')
            value, stamp, source = data['rate'], data['timestamp'], 'twelvedata'
        elif provider == 'yahoo':
            data = _json('https://query1.finance.yahoo.com/v8/finance/chart/AUDUSD%3DX?interval=1m&range=1d')
            meta = data['chart']['result'][0]['meta']
            if meta.get('symbol') != 'AUDUSD=X' or meta.get('currency') != 'USD':
                raise ValueError('AUD/USD行情币种不匹配，已暂停建议。')
            value, stamp, source = meta['regularMarketPrice'], meta['regularMarketTime'], 'yahoo_delayed'
        else:
            raise ValueError('FX_PROVIDER配置无效，支持truefx、twelvedata或yahoo。')
        quote = Quote(Decimal(str(value)), datetime.fromtimestamp(float(stamp), UTC), now, source)
    except (KeyError, TypeError, IndexError, InvalidOperation, OverflowError):
        raise ValueError('AUD/USD行情响应不完整，已暂停AUD资金建议。') from None
    return validate_quote(quote, now)


def parse_truefx(body, now):
    for row in csv.reader(io.StringIO(body)):
        if len(row) < 6 or row[0].strip() != 'AUD/USD':
            continue
        try:
            if not re.fullmatch(r'\d{13}', row[1].strip()):
                raise ValueError
            prices = []
            for big, pips in [(row[2].strip(), row[3].strip()), (row[4].strip(), row[5].strip())]:
                if not re.fullmatch(r'\d+\.\d{2}', big) or not re.fullmatch(r'\d{1,3}', pips):
                    raise ValueError
                prices.append(Decimal(big + pips.zfill(3)))
            bid, ask = prices
            if not 0 < bid <= ask <= 10:
                raise ValueError
            stamp = datetime.fromtimestamp(int(row[1]) / 1000, UTC)
            return Quote(bid, stamp, now, 'truefx')
        except (ValueError, InvalidOperation, OverflowError):
            break
    raise ValueError('AUD/USD报价不完整或买卖价异常，已暂停AUD资金建议。')


def get_quote(session, *, force=False, now=None):
    global _failure
    now = now or datetime.now(UTC)
    provider = os.getenv('FX_PROVIDER', 'truefx').lower()
    expected_source = {'truefx': 'truefx', 'yahoo': 'yahoo_delayed', 'twelvedata': 'twelvedata'}.get(provider)
    pinned = session.info.get('aud_usd_quote')
    if pinned and not force and pinned.source == expected_source:
        return validate_quote(pinned, now)
    with _lock:
        row = session.get(FxQuote, 'AUDUSD')
        if row and not force and row.source == expected_source and now - _aware(row.fetched_at) < CACHE_TTL:
            try:
                quote = validate_quote(Quote(row.aud_usd, _aware(row.observed_at), _aware(row.fetched_at), row.source), now)
            except ValueError:
                quote = None
            if quote:
                session.info['aud_usd_quote'] = quote
                return quote
        if _failure and not force and _failure[1] == provider and now - _failure[0] < CACHE_TTL:
            raise ValueError(_failure[2])
        try:
            quote = fetch_quote(now)
        except ValueError as exc:
            _failure = (now, provider, str(exc))
            raise
        _failure = None
        values = dict(pair='AUDUSD', aud_usd=quote.rate, observed_at=quote.observed_at,
                      fetched_at=quote.fetched_at, source=quote.source)
        dialect = session.get_bind().dialect.name
        if dialect == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert
            statement = insert(FxQuote).values(**values)
            session.execute(statement.on_conflict_do_update(index_elements=['pair'], set_=values))
        elif row:
            for key, value in values.items():
                setattr(row, key, value)
        else:
            session.add(FxQuote(**values))
        session.flush()
        session.info['aud_usd_quote'] = quote
        return quote


def rate_of(session, user_id=None, now=None):
    return get_quote(session, now=now).rate


def to_usd(session, user_id, amount, currency):
    amount = Decimal(amount)
    if currency == 'USD':
        return amount
    if currency != 'AUD':
        raise ValueError('现有美股策略只支持USD或AUD现金。')
    return amount * rate_of(session, user_id) if amount else Decimal(0)


def conversion_note(session, user_id, amount, currency):
    if currency != 'AUD':
        return ''
    if not amount:
        return '原币现金：0.00 AUD；按0.00 USD计算。'
    q = get_quote(session)
    label = {'truefx': 'TrueFX市场参考买价', 'yahoo_delayed': 'Yahoo延迟参考行情', 'twelvedata': 'Twelve Data实时参考行情'}[q.source]
    state = '；外汇周末休市，使用最近有效报价' if market_closed(datetime.now(UTC)) else ''
    return (f'原币现金：{amount:,.2f} AUD ≈ {amount * q.rate:,.2f} USD；'
            f'1 AUD = {q.rate:g} USD（{label}，报价{q.observed_at:%Y-%m-%d %H:%M:%S} UTC{state}）。'
            '仅为估值，未实际换汇；券商成交汇率与费用可能不同。')


def refresh_market_quote(factory, *, force=False):
    from .db import transaction
    with transaction(factory) as session:
        return get_quote(session, force=force)


async def prepare_user_fx(context, telegram_id):
    """Pre-fetch outside the event loop when this user has AUD funds."""
    from sqlalchemy import select
    from .models import CashAccount, StrategyCash, User
    from .db import transaction
    factory = context.application.bot_data['session_factory']
    with transaction(factory) as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return
        cash = session.get(CashAccount, user.id)
        needed = (cash is not None and cash.currency == 'AUD' and cash.balance > 0) or session.scalar(select(StrategyCash.user_id).where(
            StrategyCash.user_id == user.id, StrategyCash.currency == 'AUD', StrategyCash.balance > 0).limit(1)) is not None
    if needed:
        await asyncio.to_thread(refresh_market_quote, factory)
