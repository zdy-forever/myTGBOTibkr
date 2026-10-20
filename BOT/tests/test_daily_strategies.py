from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from telegram.ext import ApplicationHandlerStop
import sgs
from sgs.live import TICKERS, cfg, commission, recommendations, snapshot
from quantbot import bot, daily_service as daily, strategy_service as s1
from quantbot.allocations import allocated_cash, allocation_fraction, cash_budgets, save_weights, set_strategy_cash
from quantbot.csv_io import export_portfolio_csv, import_portfolio_csv
from quantbot.models import CashAccount, DailyReport, NotificationPreference, StrategyPool, User, UserStrategy
from quantbot.strategy_commands import parse_cash, pool_command, strategy_two_off
from tests.test_premium_access import Message, context, make_update
from tests.test_strategy_service import SETTINGS, add_user, make_run


def test_fixed_cash_exclusive_exact_reserved(factory):
    uid = add_user(factory, 1, cash='999', positions=[('AAA', '10')])
    with factory.begin() as session:
        session.add(UserStrategy(user_id=uid, strategy_id='strategy2'))
    with factory.begin() as session:
        user = session.get(User, uid)
        for n, amount in [(1,200),(2,300),(3,500)]:
            set_strategy_cash(session,user,n,amount,'USD',actor=1)
        assert session.get(CashAccount,uid).balance == 1000
        assert allocated_cash(session,uid) == (Decimal(200),'USD')
        assert allocation_fraction(session,uid) == Decimal('.2')
        assert s1.portfolio_of(session,uid).holdings['AAA'].shares == 2
        with pytest.raises(ValueError,match='独立金额'):
            save_weights(session,uid,{'strategy1':Decimal(60),'strategy2':Decimal(40)})
        session.delete(session.get(UserStrategy,(uid,'strategy2')))
        session.flush()
        assert allocated_cash(session,uid)[0] == 200
        set_strategy_cash(session,user,0,800,'USD',actor=1)
        save_weights(session,uid,{'strategy1':Decimal(100)})
        assert not cash_budgets(session,uid)
        assert allocated_cash(session,uid)[0] == 800


@pytest.mark.parametrize('text,number,amount',[('CASH 1 200USD',1,200),('/cash 2 300 USD',2,300),('0 500USD',0,500),('200usd',None,200),('CASH 3 0USD',3,0)])
def test_cash_grammar(text,number,amount):
    assert parse_cash(text)==(number,Decimal(amount),'USD')


@pytest.mark.parametrize('rows',['CASH,0,100USD\nCASH,1,20USD\n','CASH,1,100USD\nCASH,1,20USD\n','CASH,1,20AUD\n','CASH,-1,20USD\n'])
def test_csv_invalid_cash_atomic(factory,rows):
    uid=add_user(factory,1,cash='50')
    with pytest.raises(ValueError),factory.begin() as session:
        import_portfolio_csv(session,session.get(User,uid),'Company,Shares Held,Average Price\n'+rows,actor=1)
    with factory.begin() as session:
        assert session.get(CashAccount,uid).balance==50
        assert not cash_budgets(session,uid)


def test_csv_round_trip_and_partial_update(factory):
    uid,other=add_user(factory,1),add_user(factory,2)
    header='Company,Shares Held,Average Price\n'
    with factory.begin() as session:
        import_portfolio_csv(session,session.get(User,uid),header+'CASH,1,200USD\nCASH,2,300USD\n',actor=1)
        exported=export_portfolio_csv(session,session.get(User,uid))
        assert 'CASH,0' not in exported
        import_portfolio_csv(session,session.get(User,other),exported,actor=2)
        assert {r.strategy_id:r.balance for r in cash_budgets(session,other)}=={'strategy1':200,'strategy2':300}
        import_portfolio_csv(session,session.get(User,uid),header+'CASH,1,400USD\n',actor=1)
        assert session.get(CashAccount,uid).balance==700


async def test_private_pool_add_remove_and_legacy_reset(factory,accounts,monkeypatch):
    from quantbot import strategy_commands
    monkeypatch.setattr(strategy_commands,'default_pool_symbols',lambda:['AAA','BBB'])
    uid,other=add_user(factory,999),add_user(factory,2)
    ctx=context(factory,accounts)
    ctx.args=['+','CCC']
    await pool_command(make_update(Message(text='/strategy1_pool + CCC'),user_id=999),ctx)
    run=make_run()
    with factory.begin() as session:
        assert set(s1.personal_run(session,uid,run).symbols)=={'AAA','BBB','CCC'}
        assert s1.personal_run(session,other,run) is run
        assert session.get(StrategyPool,uid).symbols==['AAA','BBB','CCC']
    ctx.args=['-','AAA']
    await pool_command(make_update(Message(text='/strategy1_pool - AAA'),user_id=999),ctx)
    with factory.begin() as session:
        assert set(s1.personal_run(session,uid,run).symbols)=={'BBB','CCC'}
    ctx.args=['reset']
    await pool_command(make_update(Message(text='/strategy1_pool reset'),user_id=999),ctx)
    with factory.begin() as session:
        assert s1.personal_run(session,uid,run) is run


@pytest.mark.parametrize('enabled',[set(),{'strategy1'},{'strategy2'},{'strategy1','strategy2'}])
async def test_command_and_menu_matrix(factory,accounts,enabled):
    with factory.begin() as session:
        user=accounts.register(session,999,'+61400000999').user
        for key in enabled:
            session.add(UserStrategy(user_id=user.id,strategy_id=key))
    ctx=context(factory,accounts)
    for command,required in [('/check1','strategy1'),('/strategy1_pool','strategy1'),('/check2','strategy2')]:
        update=make_update(Message(text=command),user_id=999)
        if required in enabled:
            await bot.authorization_gate(update,ctx)
        else:
            with pytest.raises(ApplicationHandlerStop):
                await bot.authorization_gate(update,ctx)
    menu={c.command for c in bot._menu_for('admin',enabled)}
    assert ('strategy1_off' in menu)==('strategy1' in enabled)
    assert ('strategy2_off' in menu)==('strategy2' in enabled)
    assert 'check2' in menu
    assert ('check' in menu)==bool(enabled)
    assert 'check1' in menu
    descriptions={c.command:c.description for c in bot._menu_for('admin',enabled)}
    for number in (1,2):
        assert ('需先启用' in descriptions[f'check{number}'])==(f'strategy{number}' not in enabled)
    update=make_update(Message(text='/check'),user_id=999)
    if enabled:
        await bot.authorization_gate(update,ctx)
    else:
        with pytest.raises(ApplicationHandlerStop):
            await bot.authorization_gate(update,ctx)


@pytest.mark.parametrize('enabled',[
    set(), {'strategy1'}, {'strategy2'}, {'strategy1','strategy2'},
    {'strategy2','<unknown>'},
])
async def test_check_dispatches_only_enabled_strategies(factory,accounts,monkeypatch,enabled):
    with factory.begin() as session:
        user=accounts.register(session,999,'+61400000999').user
        for key in enabled:
            session.add(UserStrategy(user_id=user.id,strategy_id=key))
    calls=[]
    async def one(update,ctx):
        calls.append('strategy1')
    async def two(update,ctx,*,show_progress=True):
        assert show_progress is False
        calls.append('strategy2')
    monkeypatch.setattr(bot,'check_one',one)
    monkeypatch.setattr(bot,'check_two',two)
    message=Message(text='/check')
    await bot.check(make_update(message,user_id=999),context(factory,accounts))
    assert calls==sorted(enabled & {'strategy1','strategy2'})
    text='\n'.join(t for t,_ in message.replies)
    if not enabled:
        assert '尚未启用' in text and '/strategy' in text
    if '<unknown>' in enabled:
        assert '暂不支持' in text and '<unknown>' not in text


async def test_check_continues_after_strategy_failure(factory,accounts,monkeypatch):
    uid=add_user(factory,999)
    with factory.begin() as session:
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))
    one=AsyncMock(side_effect=RuntimeError('unavailable'))
    two=AsyncMock()
    monkeypatch.setattr(bot,'check_one',one)
    monkeypatch.setattr(bot,'check_two',two)
    message=Message(text='/check')
    update=make_update(message,user_id=999)
    ctx=context(factory,accounts)
    await bot.check(update,ctx)
    one.assert_awaited_once_with(update,ctx)
    two.assert_awaited_once_with(update,ctx,show_progress=False)
    assert any('策略 1 检查失败' in text and '/check1' in text for text,_ in message.replies)


@pytest.mark.parametrize('registered',[False,True])
async def test_check_requires_verified_premium(factory,accounts,monkeypatch,registered):
    if registered:
        add_user(factory,101)
    one=AsyncMock()
    two=AsyncMock()
    monkeypatch.setattr(bot,'check_one',one)
    monkeypatch.setattr(bot,'check_two',two)
    ctx=context(factory,accounts)
    ctx.application.bot_data['enforce_premium']=True
    message=Message(text='/check')
    await bot.check(make_update(message),ctx)
    one.assert_not_awaited()
    two.assert_not_awaited()
    assert 'Premium' in message.replies[0][0]


async def test_check_missing_s1_budget_still_checks_s2(factory,accounts,monkeypatch):
    uid=add_user(factory,999)
    with factory.begin() as session:
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))
        session.flush()
        set_strategy_cash(session,session.get(User,uid),2,300,'USD',actor=999)
    two=AsyncMock()
    monkeypatch.setattr(bot,'check_two',two)
    message=Message(text='/check')
    ctx=context(factory,accounts)
    await bot.check(make_update(message,user_id=999),ctx)
    two.assert_awaited_once()
    assert any(text.startswith('策略 1 · ') and '独立金额模式' in text and 'CASH' in text for text,_ in message.replies)


@pytest.mark.parametrize('feed,hour,minute,expected',[
    ('sip',19,59,date(2026,10,20)),
    ('sip',20,5,date(2026,10,20)),
    ('sip',20,15,date(2026,10,20)),
    ('sip',20,16,date(2026,10,21)),
    ('sip',21,35,date(2026,10,21)),
    ('iex',20,5,date(2026,10,21)),
])
async def test_s2_manual_check_waits_for_closed_bar_availability(
        factory,accounts,monkeypatch,feed,hour,minute,expected):
    from quantbot import strategy_commands
    uid=add_user(factory,999)
    with factory.begin() as session:
        session.delete(session.get(UserStrategy,(uid,'strategy1')))
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))

    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime(2026,10,20,hour,minute,tzinfo=UTC)

    market=SimpleNamespace(feed=feed,calendar=lambda a,b:[
        {'date':'2026-10-20','close':'16:00'},
        {'date':'2026-10-21','close':'16:00'},
    ])
    captured=[]
    def snapshot(factory,market,day):
        captured.append(day)
        return {'test':True}
    monkeypatch.setattr(strategy_commands,'datetime',Clock)
    monkeypatch.setattr(s1,'strategy_settings',lambda:SETTINGS)
    monkeypatch.setattr(s1,'build_engine',lambda _:SimpleNamespace(market=market))
    monkeypatch.setattr(daily,'s2_snapshot',snapshot)
    monkeypatch.setattr(daily,'s2_text',lambda *args:'策略 2 · 每日盘前报告')
    message=Message(text='/check2')
    await strategy_commands.check_two(make_update(message,user_id=999),context(factory,accounts))
    assert captured==[expected]
    assert message.replies[-1][0]=='策略 2 · 手动检查（参考最近收盘数据）'


def test_calendar_time_holidays_open():
    market=SimpleNamespace(calendar=lambda a,b:[{'date':'2026-10-19','open':'09:30'},{'date':'2026-10-20','open':'09:30'}])
    assert daily.calendar_window(market,datetime(2026,10,20,1,59,tzinfo=UTC),'Asia/Shanghai',10) is None
    window=daily.calendar_window(market,datetime(2026,10,20,2,tzinfo=UTC),'Asia/Shanghai',10)
    assert window[:2]==(date(2026,10,20),date(2026,10,19))
    assert daily.calendar_window(market,datetime(2026,10,20,13,30,tzinfo=UTC),'Asia/Shanghai',10) is None
    assert daily.calendar_window(market,datetime(2026,10,21,2,tzinfo=UTC),'Asia/Shanghai',10) is None


def test_s1_monthly_buy_only_on_selected_day(factory):
    uid=add_user(factory,1)
    today=date(2026,10,20)
    days=[date(2026,10,19),today]
    app=SimpleNamespace(bot_data={})
    with factory.begin() as session:
        user=session.get(User,uid)
        prefs=session.get(NotificationPreference,uid)
        prefs.auto_notice_day=1
        text=daily.s1_text(session,user,make_run(),SETTINGS,today,days,app)
        assert '今日不是你的月度买入提醒日' in text and '🟢' not in text
        assert prefs.last_notice_month is None
        prefs.auto_notice_day=2
        text=daily.s1_text(session,user,make_run(),SETTINGS,today,days,app)
        assert '🟢' in text and prefs.last_notice_month=='2026-10'
        assert '🟢' not in daily.s1_text(session,user,make_run(),SETTINGS,today,days,app)


def test_frozen_cfg_unchanged():
    frozen=json.loads((Path(sgs.__file__).parents[1]/'FROZEN_RULES.json').read_text())['default_cfg_snapshot']
    actual=cfg()
    for key,value in frozen.items():
        if key not in {'PERIODS','START','END','RUN_MODE'}:
            assert json.loads(json.dumps(actual[key]))==value,key


class Market:
    def __init__(self):
        dates=pd.bdate_range('2014-01-01','2026-10-19')
        self.rows=pd.DataFrame([{'date':day,'symbol':t,'close':100*np.exp(i*.0002)} for i,day in enumerate(dates) for t in TICKERS])
    def daily_bars(self,symbols,start,end=None,adjustment='all'):
        return self.rows[(self.rows.date>=pd.Timestamp(start))&(self.rows.date<pd.Timestamp(end).tz_localize(None))]
    def calendar(self,start,end):
        return [{'date':d.date().isoformat()} for d in pd.bdate_range(start,end)]


def test_g2_replay_and_missing_data():
    market=Market()
    today=date(2026,10,20)
    full=snapshot(market,today)
    resumed=snapshot(market,today,snapshot(market,date(2026,10,19)))
    assert resumed==full
    assert full['as_of']=='2026-10-19'
    assert full['weights']['GPIQ']>=.2
    assert abs(sum(full['weights'].values())-1)<1e-9
    market.rows=market.rows[~((market.rows.symbol=='GPIQ')&(market.rows.date==pd.Timestamp('2026-10-19')))]
    with pytest.raises(ValueError,match='缺失'):
        snapshot(market,today)


def test_g2_orders_bounded_by_cash_including_costs():
    data={'weights':{'GPIQ':.2,'SGOV':.8},'prices':{t:100 for t in TICKERS}}
    actions=recommendations(data,{'TQQQ':10},0)
    assert actions[0]['action']=='SELL' and actions[0]['symbol']=='TQQQ'
    cash=0
    for action in actions:
        q=action['shares']*(1 if action['action']=='BUY' else -1)
        price=100*(1+cfg()['SLIPPAGE_PER_SIDE'] if q>0 else 1-cfg()['SLIPPAGE_PER_SIDE'])
        cash-=q*price+commission(q,price)
        assert cash>=0
    assert cash>=cfg()['V5_CASH_RESERVE_USD']
    assert recommendations(data,{},10)==[]


async def test_report_delivery_idempotent_and_old_allocation_invalidated(factory):
    uid=add_user(factory,1)
    today=datetime.now(UTC).date()
    app=SimpleNamespace(bot_data={'session_factory':factory},bot=SimpleNamespace(send_message=AsyncMock()))
    with factory.begin() as session:
        session.add(DailyReport(user_id=uid,strategy_id='strategy1',session_date=today,body='今日无需操作'))
    opening=datetime.now(UTC)+timedelta(hours=1)
    await daily.deliver_reports(app,today,opening)
    await daily.deliver_reports(app,today,opening)
    assert app.bot.send_message.await_count==1
    with factory.begin() as session:
        row=session.scalar(select(DailyReport))
        row.notified_at=None
        set_strategy_cash(session,session.get(User,uid),1,200,'USD',actor=1)
    await daily.deliver_reports(app,today,opening)
    assert app.bot.send_message.await_count==1


async def test_s2_off_keeps_s1(factory,accounts):
    uid=add_user(factory,999)
    with factory.begin() as session:
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))
    await strategy_two_off(make_update(Message(text='/strategy2_off'),user_id=999),context(factory,accounts))
    with factory.begin() as session:
        assert session.get(UserStrategy,(uid,'strategy2')) is None
        assert session.get(UserStrategy,(uid,'strategy1')) is not None


async def test_daily_orchestration_generates_both_and_reuses_reports(factory, monkeypatch):
    uid=add_user(factory,1,cash='500')
    with factory.begin() as session:
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))
    with factory.begin() as session:
        set_strategy_cash(session,session.get(User,uid),1,200,'USD',actor=1)
        set_strategy_cash(session,session.get(User,uid),2,300,'USD',actor=1)
        row,_=s1.store_run(session,make_run(as_of='2026-10-19'))
    today=date(2026,10,20)
    now=datetime(2026,10,20,2,tzinfo=UTC)
    monkeypatch.setattr(s1,'strategy_settings',lambda:SETTINGS)
    monkeypatch.setattr(s1,'build_engine',lambda settings:SimpleNamespace(market=object()))
    monkeypatch.setattr(daily,'calendar_window',lambda *args:(today,date(2026,10,19),[today],now+timedelta(hours=11)))
    calls=[]
    def market_data(*args):
        calls.append(1)
        return {'as_of':'2026-10-19','trend':'震荡','weights':{'GPIQ':1},'prices':{t:100 for t in TICKERS}}
    monkeypatch.setattr(daily,'s2_snapshot',market_data)
    delivery=AsyncMock()
    monkeypatch.setattr(daily,'deliver_reports',delivery)
    app=SimpleNamespace(bot_data={'session_factory':factory},bot=SimpleNamespace(send_message=AsyncMock()))
    await daily._daily_run(app,now)
    await daily._daily_run(app,now)
    with factory.begin() as session:
        reports=list(session.scalars(select(DailyReport)))
        assert {r.strategy_id for r in reports}=={'strategy1','strategy2'}
        assert all(r.data_ready and len(r.body)<4096 for r in reports)
        assert '当前趋势' in next(r.body for r in reports if r.strategy_id=='strategy2')
    assert len(calls)==1


async def test_data_failure_recovers_without_duplicate_report_rows(factory, monkeypatch):
    uid=add_user(factory,1)
    with factory.begin() as session:
        session.delete(session.get(UserStrategy,(uid,'strategy1')))
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))
    today=date(2026,10,20)
    now=datetime.now(UTC)
    monkeypatch.setattr(s1,'strategy_settings',lambda:SETTINGS)
    monkeypatch.setattr(s1,'build_engine',lambda settings:SimpleNamespace(market=object()))
    monkeypatch.setattr(daily,'calendar_window',lambda *args:(today,date(2026,10,19),[today],now+timedelta(hours=1)))
    def failed(*args):
        raise ValueError('missing quote')
    monkeypatch.setattr(daily,'s2_snapshot',failed)
    monkeypatch.setattr(daily,'deliver_reports',AsyncMock())
    app=SimpleNamespace(bot_data={'session_factory':factory})
    await daily._daily_run(app,now)
    with factory.begin() as session:
        row=session.scalar(select(DailyReport))
        assert not row.data_ready and '暂不提供买卖建议' in row.body
        row_id=row.id
        row.created_at=now-timedelta(hours=1)
    monkeypatch.setattr(daily,'s2_snapshot',lambda *args:{'as_of':'2026-10-19','trend':'下跌',
                        'weights':{'GPIQ':.2,'SGOV':.8},'prices':{t:100 for t in TICKERS}})
    await daily._daily_run(app,now)
    with factory.begin() as session:
        rows=list(session.scalars(select(DailyReport)))
        assert len(rows)==1 and rows[0].id==row_id and rows[0].data_ready


async def test_daily_delivery_checks_membership_and_premium(factory):
    uid=add_user(factory,1)
    now=datetime.now(UTC)
    with factory.begin() as session:
        session.add(DailyReport(user_id=uid,strategy_id='strategy1',session_date=now.date(),body='今日无需操作'))
    app=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),bot_data={
        'session_factory':factory,'enforce_premium':True,
        'accounts':SimpleNamespace(has_premium_access=lambda s,t:False)})
    await daily.deliver_reports(app,now.date(),now+timedelta(hours=1))
    app.bot.send_message.assert_not_awaited()
    app.bot_data['enforce_premium']=False
    with factory.begin() as session:
        session.delete(session.get(UserStrategy,(uid,'strategy1')))
    await daily.deliver_reports(app,now.date(),now+timedelta(hours=1))
    app.bot.send_message.assert_not_awaited()


def test_cash_named_stock_round_trips_without_changing_budget(factory):
    from quantbot.models import Position
    uid=add_user(factory,1,cash='200',positions=[('CASH','3')])
    other=add_user(factory,2,cash='0')
    with factory.begin() as session:
        exported=export_portfolio_csv(session,session.get(User,uid))
        assert 'STOCK:CASH,3,' in exported
        import_portfolio_csv(session,session.get(User,other),exported,actor=2)
    with factory.begin() as session:
        position=session.scalar(select(Position).where(Position.user_id==other))
        assert position.symbol=='CASH' and position.shares==3
        assert session.get(CashAccount,other).balance==200
        assert not cash_budgets(session,other)


@pytest.mark.parametrize('args,expected',[
    (['+','aapl','MSFT','aapl'],('add',['AAPL','MSFT'])),
    (['+aapl','+MSFT'],('add',['AAPL','MSFT'])),
    (['-','aapl,MSFT'],('remove',['AAPL','MSFT'])),
    (['添加','BRK-B'],('add',['BRK-B'])),
    (['remove','AAPL'],('remove',['AAPL'])),
])
def test_incremental_pool_parser(args,expected):
    from quantbot.strategy_commands import parse_pool_change
    assert parse_pool_change(args)==expected


@pytest.mark.parametrize('args',[['AAPL','MSFT'],['+'],['+AAPL','-MSFT'],['-','<script>'],['+',*['AAA']*61]])
def test_invalid_pool_changes_leave_pending_input(args):
    with pytest.raises(ValueError):
        bot._validate_command_args('strategy1_pool',args)


async def test_pool_defaults_remove_noop_and_empty(factory,accounts,monkeypatch):
    from quantbot import strategy_commands
    defaults=['AAA','BBB']
    monkeypatch.setattr(strategy_commands,'default_pool_symbols',lambda:defaults.copy())
    uid=add_user(factory,999)
    ctx=context(factory,accounts)
    message=Message(text='/strategy1_pool')
    await pool_command(make_update(message,user_id=999),ctx)
    assert any('AAA、BBB' in text for text,_ in message.replies)
    ctx.args=['+','AAA']
    await pool_command(make_update(Message(text='/strategy1_pool + AAA'),user_id=999),ctx)
    with factory.begin() as session:
        assert session.get(StrategyPool,uid) is None
        enabled=session.get(UserStrategy,(uid,'strategy1')).enabled_at
    ctx.args=['-','ZZZ']
    await pool_command(make_update(Message(text='/strategy1_pool - ZZZ'),user_id=999),ctx)
    with factory.begin() as session:
        assert session.get(StrategyPool,uid) is None
        assert session.get(UserStrategy,(uid,'strategy1')).enabled_at==enabled
    ctx.args=['-','AAA','BBB']
    await pool_command(make_update(Message(text='/strategy1_pool - AAA BBB'),user_id=999),ctx)
    with factory.begin() as session:
        assert session.get(StrategyPool,uid).symbols==[]
        assert not s1.personal_run(session,uid,make_run()).symbols
    ctx.args=['+','CCC']
    await pool_command(make_update(Message(text='/strategy1_pool + CCC'),user_id=999),ctx)
    with factory.begin() as session:
        assert session.get(StrategyPool,uid).symbols==['CCC']
    assert defaults==['AAA','BBB']


async def test_pool_supports_staged_add_remove_and_cancel(factory,accounts,monkeypatch):
    from quantbot import strategy_commands
    monkeypatch.setattr(strategy_commands,'default_pool_symbols',lambda:['AAA'])
    uid=add_user(factory,999)
    ctx=context(factory,accounts)
    ctx.user_data={}
    await bot._staged_command_entry(make_update(Message(text='/strategy1_pool'),user_id=999),ctx)
    assert ctx.user_data['pending_command_args'][0]=='strategy1_pool'
    await bot._staged_command_data(make_update(Message(text='BBB'),user_id=999),ctx)
    assert ctx.user_data['pending_command_args'][0]=='strategy1_pool'
    await bot._staged_command_data(make_update(Message(text='+ BBB'),user_id=999),ctx)
    with factory.begin() as session:
        assert session.get(StrategyPool,uid).symbols==['AAA','BBB']
    ctx.args=[]
    await bot._staged_command_entry(make_update(Message(text='/strategy1_pool'),user_id=999),ctx)
    await bot.cancel(make_update(Message(text='/cancel'),user_id=999),ctx)
    assert not ctx.user_data


async def test_default_pool_above_old_sixty_limit_can_be_edited(factory,accounts,monkeypatch):
    from quantbot import strategy_commands
    defaults=[f'X{i}' for i in range(76)]
    monkeypatch.setattr(strategy_commands,'default_pool_symbols',lambda:defaults.copy())
    uid=add_user(factory,999)
    ctx=context(factory,accounts)
    ctx.args=['+','AAPL']
    await pool_command(make_update(Message(text='/strategy1_pool + AAPL'),user_id=999),ctx)
    with factory.begin() as session:
        assert session.get(StrategyPool,uid).symbols==defaults+['AAPL']


def test_s2_fractional_buy_with_less_than_one_share_cash():
    data={'weights':{'GPIQ':1},'prices':{t:100 for t in TICKERS}}
    orders=recommendations(data,{},50)
    assert len(orders)==1
    order=orders[0]
    assert order['symbol']=='GPIQ' and order['action']=='BUY'
    assert 0<order['shares']<1
    q=Decimal(str(order['shares']))
    price=Decimal(100)*(1+Decimal(str(cfg()['SLIPPAGE_PER_SIDE'])))
    fee=min(max(q*Decimal('.005'),Decimal(1)),q*price*Decimal('.01'))
    assert Decimal(50)-q*price-fee>=Decimal(str(cfg()['V5_CASH_RESERVE_USD']))


def test_s2_fractional_exit_preserves_sub_precision_dust_and_sells_first():
    data={'weights':{'GPIQ':1},'prices':{t:100 for t in TICKERS}}
    orders=recommendations(data,{'TQQQ':Decimal('.123456789')},100)
    assert orders[0]['symbol']=='TQQQ' and orders[0]['action']=='SELL'
    assert orders[0]['shares']==.123456
    assert Decimal(str(orders[0]['shares']))<=Decimal('.123456789')
    assert any(o['action']=='BUY' and not o['shares'].is_integer() for o in orders)


def test_s2_fractional_cash_and_holdings_invariants():
    import random
    rng=random.Random(42)
    for _ in range(200):
        prices={t:round(rng.uniform(5,1500),4) for t in TICKERS}
        held={t:Decimal(str(round(rng.uniform(0,5),8))) for t in TICKERS}
        cash=Decimal(str(round(rng.uniform(0,500),4)))
        orders=recommendations({'weights':{'GPIQ':.2,'SGOV':.8},'prices':prices},held,cash)
        buy_seen=False
        for order in orders:
            q=Decimal(str(order['shares']))
            assert q>0 and q==q.quantize(Decimal('.000001'))
            buying=order['action']=='BUY'
            if buying: buy_seen=True
            else:
                assert not buy_seen
                assert q<=held[order['symbol']]
            px=Decimal(str(prices[order['symbol']]))*(1+Decimal(str(cfg()['SLIPPAGE_PER_SIDE']))*(1 if buying else -1))
            fee=min(max(q*Decimal('.005'),Decimal(1)),q*px*Decimal('.01'))
            cash=cash-q*px-fee if buying else cash+q*px-fee
            assert cash>=0
            if buying: assert cash>=Decimal(str(cfg()['V5_CASH_RESERVE_USD']))


def test_s2_fractional_report_readable_and_no_whole_share_excuse(factory):
    uid=add_user(factory,1,cash='50')
    with factory.begin() as session:
        session.delete(session.get(UserStrategy,(uid,'strategy1')))
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))
    with factory.begin() as session:
        text=daily.s2_text(session,uid,{'as_of':'2026-10-19','trend':'震荡',
            'weights':{'GPIQ':1},'prices':{t:100 for t in TICKERS}},date(2026,10,20))
        assert '买入 GPIQ 0.' in text and '支持碎股' in text and '约 $' in text
        assert '整股' not in text and 'e-' not in text
