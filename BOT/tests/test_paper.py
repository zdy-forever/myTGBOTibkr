"""Owner-only S2 paper trading: ledger, Alpaca paper orders and safety rails."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from telegram.ext import ApplicationHandlerStop

from quantbot import bot, fx, paper
from quantbot.db import transaction
from quantbot.models import PaperAccount
from tests.test_admin_management import configured_accounts
from tests.test_premium_access import Message, context, make_update

D = Decimal
MON, TUE, WED = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)
SESSIONS = [date(2026, 10, 29), date(2026, 10, 30), MON, TUE, WED, date(2026, 11, 5), date(2026, 11, 6),
            date(2026, 11, 9), date(2026, 11, 27), date(2026, 11, 30), date(2026, 12, 1)]


def premarket_utc(day):
    return datetime(day.year, day.month, day.day, 2, 30, tzinfo=UTC)  # 10:30 UTC+8 = previous evening ET


class Market:
    def __init__(self):
        self.opens = {}

    def calendar(self, start, end):
        return [{'date': d.isoformat(), 'open': '09:30', 'close': '16:00'} for d in SESSIONS
                if start <= d <= end]

    def daily_bars(self, symbols, start, end=None, adjustment='raw'):
        day = date.fromisoformat(start[:10])
        if day not in self.opens:
            return pd.DataFrame(columns=['symbol', 'date', 'open', 'close'])
        return pd.DataFrame([{'symbol': 'QQQM', 'date': pd.Timestamp(day, tz='UTC'),
                              'open': self.opens[day], 'close': self.opens[day]}])


class Broker:
    def __init__(self):
        self.orders, self.submitted, self.positions = {}, [], {}
        self.actions = {}
        self.account_number, self.open = 'PA123', False
        self.fail_next_submit = None

    def require_paper(self):
        if not self.account_number.startswith('PA'):
            raise paper.PaperError('Alpaca 账户不是可交易的模拟账户，已停止下单。')
        return {}

    def available(self, symbol):
        return self.positions.get(symbol, D(0))

    def order(self, cid):
        return self.orders.get(cid)

    def submit(self, symbol, side, qty, cid):
        if self.fail_next_submit:
            error, self.fail_next_submit = self.fail_next_submit, None
            if not error.definite:
                self.orders[cid] = {'id': 'x-' + cid, 'status': 'accepted', 'qty': str(qty)}  # reached Alpaca
            raise error
        self.submitted.append((symbol, side, D(qty)))
        self.orders[cid] = {'id': 'x-' + cid, 'status': 'accepted', 'qty': str(qty)}
        return self.orders[cid]

    def fill(self, price_by_symbol, *, fraction=1):
        for order in self.orders.values():
            if order['status'] == 'accepted':
                qty = D(order['qty']) * D(fraction)
                cid = order['id'][2:]
                symbol = cid.split('-')[3]
                order.update(status='filled', filled_qty=str(qty), filled_avg_price=str(price_by_symbol[symbol]))

    def clock(self):
        return {'is_open': self.open}

    def corporate_actions(self, symbols, start, end):
        return self.actions


def snapshot_for(prices, weights, as_of):
    def fn(factory, market, today):
        return {'as_of': as_of.isoformat(), 'trend': '强上涨', 'bucket': 'STRONG_UP',
                'weights': weights, 'prices': prices}
    return fn


PRICES = {'QQQ': 700., 'QQQM': 290., 'SPMO': 100., 'SCHD': 35., 'TQQQ': 66., 'SGOV': 100.5}
WEIGHTS = {'TQQQ': .85, 'SCHD': .15}


@pytest.fixture
def setup(factory, monkeypatch):
    monkeypatch.setattr(fx, 'get_quote', lambda session, **kw: fx.Quote(D('0.65'), datetime.now(UTC),
                                                                          datetime.now(UTC), 'truefx'))
    with transaction(factory) as session:
        account = paper.start_account(session, 777, D(900), 'AUD', D(400), MON)
        account_id = account.id
    return SimpleNamespace(factory=factory, market=Market(), broker=Broker(), id=account_id)


def run(s, day, prices=PRICES, weights=WEIGHTS, as_of=None):
    as_of = as_of or max(d for d in SESSIONS if d < day)
    return paper.premarket(s.factory, s.market, s.broker, s.id, premarket_utc(day), 'Asia/Shanghai', 10,
                           snapshot_for(prices, weights, as_of))


def account(s):
    with transaction(s.factory) as session:
        return session.get(PaperAccount, s.id)


def test_first_session_deposits_orders_and_benchmark(setup):
    s = setup
    chat, text = run(s, MON)
    a = account(s)
    usd = D(900) * D('0.65') - 2
    assert chat == 777 and a.deposits_usd == usd and a.units == usd and a.benchmark_units == usd
    assert a.last_session == MON and a.last_valued_on == date(2026, 10, 30)
    # Whole-share S2 buys within cash, TQQQ first (largest gap), SCHD under the $80 minimum is skipped.
    assert s.broker.submitted and all(q == q.to_integral_value() for _, _, q in s.broker.submitted)
    assert s.broker.submitted[0][:2] == ('TQQQ', 'buy')
    assert sum(q * D(PRICES[t]) for t, _, q in s.broker.submitted) <= usd
    assert '已提交' in text and '583.00' in text
    with transaction(s.factory) as session:
        bench = [e for e in paper._events(session, s.id, 'bench')]
    assert bench and not bench[0].data['done']
    assert run(s, MON) is None  # one premarket per session


def test_fills_update_cash_and_benchmark_buys_at_next_open(setup):
    s = setup
    run(s, MON)
    s.broker.fill({'TQQQ': 66.5, 'SCHD': 35.1})
    lines = paper.reconcile(s.factory, s.broker, s.id)
    a = account(s)
    held = paper.holdings_of(a)
    spent = held['TQQQ'] * D('66.5')
    fee = paper._commission(held['TQQQ'], D('66.5'))
    assert any('TQQQ' in line and '均价 $66.50' in line for line in lines)
    assert a.cash_usd == a.deposits_usd - spent - fee and a.fees_usd == fee
    assert paper.reconcile(s.factory, s.broker, s.id) == []  # applied exactly once
    s.market.opens[MON] = 291
    run(s, TUE, as_of=MON)
    a = account(s)
    assert a.benchmark_shares == 2 and a.benchmark_cash_usd == a.deposits_usd - 2 * 291 - D('0.40')
    with transaction(s.factory) as session:
        mark = paper._latest_mark(session, s.id)
    assert mark.session_date == MON and D(mark.data['unit']) < 1  # fees and slippage


def test_sale_never_exceeds_alpaca_position_so_no_short(setup):
    s = setup
    run(s, MON)
    s.broker.fill({'TQQQ': 66, 'SCHD': 35})
    paper.reconcile(s.factory, s.broker, s.id)
    held = paper.holdings_of(account(s))['TQQQ']
    s.broker.positions = {'TQQQ': held - 2}  # e.g. manual trades in the shared paper account
    _, text = run(s, TUE, weights={'SCHD': .2, 'SGOV': .8}, as_of=MON)
    sells = [x for x in s.broker.submitted if x[1] == 'sell']
    assert sells == [('TQQQ', 'sell', held - 2)]
    s.broker.positions = {}
    s.broker.orders.clear()
    with transaction(s.factory) as session:
        a = session.get(PaperAccount, s.id)
        a.last_session = None
        for e in paper._events(session, s.id, 'order'):
            if e.session_date == TUE:
                session.delete(e)
    _, text = run(s, TUE, weights={'SCHD': .2, 'SGOV': .8}, as_of=MON)
    assert '不会做空' in text and len([x for x in s.broker.submitted if x[1] == 'sell']) == 1


def test_unknown_submit_is_resolved_by_client_order_id_without_duplicate(setup):
    s = setup
    s.broker.fail_next_submit = paper.PaperError('timeout', definite=False)
    _, text = run(s, MON)
    assert '待确认' in text
    with transaction(s.factory) as session:
        statuses = [e.data['status'] for e in paper._events(session, s.id, 'order')]
    assert 'planned' in statuses
    before = len(s.broker.submitted)
    paper.resolve_planned(s.factory, s.broker, s.id, premarket_utc(MON) + timedelta(hours=1))
    assert len(s.broker.submitted) == before  # found by client order ID, not re-sent
    with transaction(s.factory) as session:
        assert all(e.data['status'] != 'planned' for e in paper._events(session, s.id, 'order'))


def test_stale_unknown_order_expires_and_blocks_nothing_twice(setup):
    s = setup
    s.broker.fail_next_submit = paper.PaperError('timeout', definite=False)
    run(s, MON)
    s.broker.orders.clear()  # it never reached Alpaca
    paper.resolve_planned(s.factory, s.broker, s.id, premarket_utc(TUE))
    with transaction(s.factory) as session:
        first = paper._events(session, s.id, 'order')[0]
    assert first.data['status'] == 'expired'


def test_definite_rejection_retried_once_during_market_hours(setup):
    s = setup
    s.broker.fail_next_submit = paper.PaperError('HTTP 422: fractional premarket')
    run(s, MON)
    during = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    assert paper.retry_failed(s.factory, s.broker, s.id, during) == []  # market closed
    s.broker.open = True
    results = paper.retry_failed(s.factory, s.broker, s.id, during)
    assert results and results[0][1] == 'submitted'
    assert paper.retry_failed(s.factory, s.broker, s.id, during) == []


def test_dividend_withholding_uses_shares_held_before_ex_date(setup):
    s = setup
    run(s, MON)
    s.broker.fill({'TQQQ': 66, 'SCHD': 35})
    paper.reconcile(s.factory, s.broker, s.id)
    held = paper.holdings_of(account(s))
    s.broker.actions = {'cash_dividends': [
        {'id': 'd1', 'symbol': 'TQQQ', 'rate': 0.2, 'ex_date': TUE.isoformat(), 'payable_date': WED.isoformat(),
         'process_date': WED.isoformat()}]}
    run(s, TUE, as_of=MON)
    assert account(s).dividends_usd == 0  # not payable yet
    run(s, WED, as_of=TUE)
    net = D('0.2') * held['TQQQ'] * D('0.85')
    assert account(s).dividends_usd == net
    run(s, date(2026, 11, 5), as_of=WED)
    assert account(s).dividends_usd == net


def test_late_split_only_scales_shares_held_through_ex_date(setup):
    s = setup
    run(s, MON)
    s.broker.fill({'TQQQ': 66, 'SCHD': 35})
    paper.reconcile(s.factory, s.broker, s.id)
    before = paper.holdings_of(account(s))['TQQQ']
    halved = dict(PRICES, TQQQ=33.)
    s.broker.actions = {}
    run(s, TUE, as_of=MON)
    _, text = run(s, WED, prices=halved, as_of=TUE)  # Alpaca has not reported the 2:1 split yet
    assert '疑似未登记的拆股' in text and '暂停下单' in text
    assert paper.holdings_of(account(s))['TQQQ'] == before
    assert account(s).max_drawdown > D('-0.05')  # the bogus halving is not a drawdown
    with transaction(s.factory) as session:
        a = session.get(PaperAccount, s.id)
        a.last_session = None
    s.broker.actions = {'forward_splits': [{'id': 's1', 'symbol': 'TQQQ', 'old_rate': 1, 'new_rate': 2,
                                            'ex_date': TUE.isoformat(), 'process_date': TUE.isoformat()}]}
    _, text = run(s, date(2026, 11, 5), prices=halved, as_of=WED)
    assert '已按除权日补记' in text
    assert paper.holdings_of(account(s))['TQQQ'] == before * 2
    assert account(s).max_drawdown > D('-0.05')
    with transaction(s.factory) as session:
        assert all(h.data['resolved'] for h in paper._events(session, s.id, 'hold'))
    assert '暂停下单' not in text


def test_monthly_deposit_once_on_first_session_of_month(setup):
    s = setup
    run(s, MON)
    first = account(s).deposits_aud
    run(s, TUE, as_of=MON)
    assert account(s).deposits_aud == first == 900  # November deposit not repeated on later sessions
    SESSIONS_DEC = date(2026, 12, 1)
    run(s, SESSIONS_DEC, as_of=date(2026, 11, 30))
    assert account(s).deposits_aud == 1300
    with transaction(s.factory) as session:
        a = session.get(PaperAccount, s.id)
        a.last_session = None
    run(s, SESSIONS_DEC, as_of=date(2026, 11, 30))
    assert account(s).deposits_aud == 1300


def test_fx_outage_defers_deposit_without_losing_it(setup, monkeypatch):
    s = setup

    def down(session, **kw):
        raise ValueError('AUD/USD行情时间异常或已过期')
    monkeypatch.setattr(fx, 'get_quote', down)
    _, text = run(s, MON)
    assert '入金顺延' in text and account(s).deposits_usd == 0 and not s.broker.submitted
    monkeypatch.setattr(fx, 'get_quote', lambda session, **kw: fx.Quote(D('0.66'), datetime.now(UTC),
                                                                          datetime.now(UTC), 'truefx'))
    run(s, TUE, as_of=MON)
    assert account(s).deposits_usd == D(900) * D('0.66') - 2


def test_live_account_refused_before_any_order(setup):
    s = setup
    s.broker.account_number = 'AB999'
    with pytest.raises(paper.PaperError, match='模拟账户'):
        run(s, MON)
    assert not s.broker.submitted and account(s).last_session is None


def test_broker_url_is_pinned_to_paper(monkeypatch):
    calls = []

    class Session:
        def request(self, method, url, **kwargs):
            calls.append((method, url))
            return SimpleNamespace(status_code=200, json=lambda: {'id': '1'}, text='')
    broker = paper.PaperBroker('k', 's', session=Session())
    broker.submit('TQQQ', 'buy', D('3'), 'cid')
    broker.order('cid')
    assert all(url.startswith('https://paper-api.alpaca.markets/') for _, url in calls)
    assert calls[0][0] == 'POST'


def test_post_is_never_retried_blindly():
    import requests
    attempts = []

    class Session:
        def request(self, method, url, **kwargs):
            attempts.append(method)
            raise requests.ConnectionError()
    broker = paper.PaperBroker('k', 's', session=Session())
    with pytest.raises(paper.PaperError) as error:
        broker.submit('TQQQ', 'buy', D('1'), 'cid')
    assert attempts == ['POST'] and not error.value.definite


@pytest.mark.parametrize('args,expected', [(['900AUD', '400AUD'], [(D(900), 'AUD'), (D(400), 'AUD')]),
                                           (['600', 'USD'], [(D(600), 'USD')]), (['A$900'], [(D(900), 'AUD')])])
def test_amount_parsing(args, expected):
    assert paper.parse_amounts(args) == expected


@pytest.mark.parametrize('args', [['900'], ['abc'], ['900AUD', 'x']])
def test_amount_parsing_rejects(args):
    with pytest.raises(ValueError):
        paper.parse_amounts(args)


async def test_owner_only_commands_and_menu(factory):
    accounts = configured_accounts()
    with transaction(factory) as session:
        accounts.register(session, 999, '+61400000999')
        accounts.register(session, 101, '+61400000001')
        accounts.register(session, 102, '+61400000002')
        accounts.bootstrap_admins(session)
        accounts.admin_add(session, 999, 101)
    ctx = context(factory, accounts)
    ctx.args = ['900AUD', '400AUD']
    user = Message(text='/paper_start 900AUD 400AUD', user_id=102)
    with pytest.raises(ApplicationHandlerStop):  # ordinary users are stopped at the gate
        await bot.authorization_gate(make_update(user, user_id=102), ctx)
    assert '管理员' in user.replies[0][0]
    message = Message(text='/paper_start', user_id=101)  # other admins are stopped by the handler
    await paper.paper_start_command(make_update(message, user_id=101), ctx)
    assert '仅 Bot Owner' in message.replies[0][0]
    owner = Message(text='/paper_start 900AUD 400AUD', user_id=999)
    await paper.paper_start_command(make_update(owner, user_id=999), ctx)
    assert '模拟盘已创建' in owner.replies[0][0] and 'PAPER_TRADING=1' in owner.replies[0][0]
    status = Message(text='/paper', user_id=999)
    await paper.paper_command(make_update(status, user_id=999), ctx)
    assert '等待第一次盘前处理' in status.replies[0][0]
    again = Message(text='/paper_start 900AUD', user_id=999)
    ctx.args = ['900AUD']
    await paper.paper_start_command(make_update(again, user_id=999), ctx)
    assert '已有运行中的模拟盘' in again.replies[0][0]

    class MenuBot:
        def __init__(self):
            self.calls = {}

        async def set_my_commands(self, commands, *, scope):
            self.calls[scope.chat_id] = {c.command for c in commands}
    app = SimpleNamespace(bot=MenuBot(), bot_data={'session_factory': factory, 'accounts': accounts})
    await bot._refresh_user_menu(app, 999)
    await bot._refresh_user_menu(app, 101)
    assert 'paper' in app.bot.calls[999] and 'paper' not in app.bot.calls[101]


async def test_tick_disabled_or_without_account_does_nothing(factory):
    app = SimpleNamespace(bot_data={'session_factory': factory, 'paper_trading': False}, bot=AsyncMock())
    called = []
    await paper.paper_tick(SimpleNamespace(application=app), services=lambda: called.append(1))
    app.bot_data['paper_trading'] = True
    await paper.paper_tick(SimpleNamespace(application=app), services=lambda: called.append(1))
    assert not called and not app.bot.send_message.called


async def test_tick_reports_errors_once_per_day(setup):
    s = setup
    app = SimpleNamespace(bot_data={'session_factory': s.factory, 'paper_trading': True,
                                    'strategy_push': ('Asia/Shanghai', 10)}, bot=AsyncMock())
    s.broker.account_number = 'LIVE1'
    services = lambda: (s.market, s.broker)
    snap = snapshot_for(PRICES, WEIGHTS, date(2026, 10, 30))
    for _ in range(2):
        await paper.paper_tick(SimpleNamespace(application=app), premarket_utc(MON), services=services,
                               snapshot_fn=snap)
    assert app.bot.send_message.await_count == 1
    assert '模拟账户' in app.bot.send_message.await_args.args[1]
    s.broker.account_number = 'PA1'
    await paper.paper_tick(SimpleNamespace(application=app), premarket_utc(MON) + timedelta(minutes=5),
                           services=services, snapshot_fn=snap)
    assert app.bot.send_message.await_count == 2 and s.broker.submitted


def test_paper_migration_round_trip(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from alembic import command

    root = Path(__file__).resolve().parents[1]
    url = 'sqlite:///' + str(tmp_path / 'paper.db')
    monkeypatch.chdir(root)
    monkeypatch.setenv('DATABASE_URL', url)
    cfg = Config()
    cfg.set_main_option('script_location', str(root / 'alembic'))
    cfg.set_main_option('sqlalchemy.url', url)
    command.upgrade(cfg, 'head')
    engine = create_engine(url)
    assert {'paper_accounts', 'paper_events'} <= set(inspect(engine).get_table_names())
    command.downgrade(cfg, '0017_aud_cash')
    assert 'paper_accounts' not in set(inspect(engine).get_table_names())
    engine.dispose()
