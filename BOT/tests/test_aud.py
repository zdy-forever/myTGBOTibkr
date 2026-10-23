from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text

from quantbot import bot, daily_service, fx, strategy_service
from quantbot.allocations import allocated_cash, allocation_fraction, set_strategy_cash
from quantbot.csv_io import export_portfolio_csv, import_portfolio_csv
from quantbot.models import CashAccount, FxQuote, StrategyCash, User, UserStrategy
from quantbot.services import parse_money_text
from quantbot.strategy_commands import fx_command, parse_cash, save_cash_text
from tests.test_premium_access import Message, context, make_update
from tests.test_strategy_service import SETTINGS, add_user, make_run


@pytest.fixture
def live_fx(monkeypatch):
    monkeypatch.setenv('FX_PROVIDER', 'truefx')
    monkeypatch.setattr(fx, '_failure', None)
    calls = []
    def fetch(now=None):
        stamp = now or datetime.now(UTC)
        calls.append(stamp)
        return fx.Quote(Decimal('.65'), stamp, stamp, 'truefx')
    monkeypatch.setattr(fx, 'fetch_quote', fetch)
    return calls


@pytest.mark.parametrize('value',["200 AUD", "200aud", "AUD200", "A$200", "AU$ 200", "200澳元", "200澳币"])
def test_aud_money_aliases(value):
    assert parse_money_text(value) == (Decimal(200), 'AUD')


@pytest.mark.parametrize('value',["US$200AUD", "A$200USD", "$200AUD"])
def test_ambiguous_currency_symbols_rejected(value):
    with pytest.raises(ValueError):
        parse_money_text(value)
    assert parse_money_text('$200') == (Decimal(200), 'USD')


def test_truefx_parser_uses_aud_sell_bid_not_inverse_or_ask():
    now=datetime(2026,10,23,15,tzinfo=UTC)
    body=f'USD/JPY,1792764562421,151.,003,151.,007\nAUD/USD,{int(now.timestamp()*1000)},0.65,001,0.65,009,0.64,0.66\n'
    quote=fx.parse_truefx(body,now)
    assert quote.rate==Decimal('.65001') and quote.observed_at==now
    with pytest.raises(ValueError):
        fx.parse_truefx(body.replace('0.65,009','0.64,009'),now)


@pytest.mark.parametrize('value',['NaN','Infinity','0','-1','11'])
def test_bad_rates_rejected(value):
    now=datetime(2026,10,23,15,tzinfo=UTC)
    with pytest.raises(ValueError):
        fx.validate_quote(fx.Quote(Decimal(value),now,now,'truefx'),now)


def test_stale_and_future_quotes_cannot_generate_advice():
    now=datetime(2026,10,23,15,tzinfo=UTC)
    for stamp in [now-timedelta(minutes=6),now+timedelta(minutes=2)]:
        with pytest.raises(ValueError):
            fx.validate_quote(fx.Quote(Decimal('.65'),stamp,now,'truefx'),now)
    # Weekend last close is permitted, but not after the market has reopened.
    friday=datetime(2026,10,23,20,59,tzinfo=UTC)
    saturday=friday+timedelta(hours=12)
    fx.validate_quote(fx.Quote(Decimal('.65'),friday,saturday,'truefx'),saturday)
    with pytest.raises(ValueError):
        fx.validate_quote(fx.Quote(Decimal('.65'),friday-timedelta(days=1),saturday,'truefx'),saturday)
    sunday_open=datetime(2026,10,25,22,tzinfo=UTC)
    with pytest.raises(ValueError):
        fx.validate_quote(fx.Quote(Decimal('.65'),friday,sunday_open,'truefx'),sunday_open)


def test_quote_cache_refresh_and_failed_refresh_never_uses_expired_data(factory,live_fx,monkeypatch):
    now=datetime(2026,10,23,15,tzinfo=UTC)
    with factory.begin() as session:
        assert fx.get_quote(session,now=now).rate==Decimal('.65')
    with factory.begin() as session:
        assert fx.get_quote(session,now=now+timedelta(seconds=10)).rate==Decimal('.65')
    assert len(live_fx)==1
    with factory.begin() as session:
        fx.get_quote(session,now=now+timedelta(seconds=31))
    assert len(live_fx)==2
    def fail(now=None):raise ValueError('行情不可用')
    monkeypatch.setattr(fx,'fetch_quote',fail)
    with pytest.raises(ValueError),factory.begin() as session:
        fx.get_quote(session,now=now+timedelta(minutes=10))


def test_mixed_independent_budgets_share_holdings_by_usd_value_and_keep_original(factory,live_fx):
    uid=add_user(factory,1,positions=[('AAA','10')]);other=add_user(factory,2,cash='50')
    with factory.begin() as session:
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))
    with factory.begin() as session:
        user=session.get(User,uid)
        set_strategy_cash(session,user,1,200,'AUD',actor=1)
        set_strategy_cash(session,user,2,130,'USD',actor=1)
        assert allocation_fraction(session,uid)==Decimal('.5')
        assert allocated_cash(session,uid)==(Decimal(200),'AUD')
        portfolio=strategy_service.portfolio_of(session,uid)
        assert portfolio.cash==130 and portfolio.currency=='USD'
        assert portfolio.holdings['AAA'].shares==5
        assert session.get(StrategyCash,(uid,'strategy1')).balance==200
        assert session.get(StrategyCash,(uid,'strategy1')).currency=='AUD'
        assert session.get(CashAccount,uid).balance==260
        assert session.get(CashAccount,other).balance==50


def test_public_aud_cash_is_converted_for_both_engines_without_mutation(factory,live_fx):
    uid=add_user(factory,1)
    with factory.begin() as session:
        user=session.get(User,uid)
        set_strategy_cash(session,user,0,1000,'AUD',actor=1)
        data=strategy_service.signals_for(session,uid,make_run(),SETTINGS)
        assert data['summary']['cash']==650
        assert '1000' in data['cash_conversion'].replace(',','') and 'TrueFX' in data['cash_conversion']
        session.delete(session.get(UserStrategy,(uid,'strategy1')))
        session.add(UserStrategy(user_id=uid,strategy_id='strategy2',weight_percent=Decimal(100)))
        session.flush()
        from sgs.live import TICKERS
        snapshot=dict(as_of='2026-10-22',trend='上涨',weights={'SCHD':1.},prices={ticker:30. for ticker in TICKERS})
        body=daily_service.s2_text(session,uid,snapshot,date(2026,10,23))
        assert '$650.00' in body and '1,000.00 AUD' in body and '报价' in body
        cash=session.get(CashAccount,uid)
        assert cash.balance==1000 and cash.currency=='AUD'


def test_csv_keeps_mixed_currency_round_trip(factory,live_fx):
    uid=add_user(factory,1);other=add_user(factory,2)
    with factory.begin() as session:
        import_portfolio_csv(session,session.get(User,uid),'Company,Shares Held,Average Price\nCASH,1,200 AUD\nCASH,2,130 USD\n',actor=1)
        exported=export_portfolio_csv(session,session.get(User,uid))
        assert 'CASH,1,200.00 AUD' in exported
        assert 'CASH,2,130.00 USD' in exported
        import_portfolio_csv(session,session.get(User,other),exported,actor=2)
        assert session.get(StrategyCash,(other,'strategy1')).currency=='AUD'
        assert session.get(CashAccount,other).balance==260


def test_unavailable_fx_blocks_aud_but_usd_keeps_working(factory,monkeypatch):
    uid=add_user(factory,1);other=add_user(factory,2,cash='200')
    monkeypatch.setattr(fx,'_failure',None)
    monkeypatch.setattr(fx,'fetch_quote',lambda now=None: (_ for _ in ()).throw(ValueError('行情不可用')))
    with factory.begin() as session:
        set_strategy_cash(session,session.get(User,uid),0,1000,'AUD',actor=1)
    with factory.begin() as session:
        with pytest.raises(ValueError):strategy_service.portfolio_of(session,uid)
        assert strategy_service.portfolio_of(session,other).cash==200


def test_automatic_rate_change_revalues_budget_fraction_without_changing_aud(factory,live_fx,monkeypatch):
    uid=add_user(factory,1,positions=[('AAA','10')])
    with factory.begin() as session:session.add(UserStrategy(user_id=uid,strategy_id='strategy2'))
    with factory.begin() as session:
        user=session.get(User,uid)
        set_strategy_cash(session,user,1,200,'AUD',actor=1)
        set_strategy_cash(session,user,2,130,'USD',actor=1)
    monkeypatch.setattr(fx,'fetch_quote',lambda now=None: fx.Quote(Decimal('.70'),now or datetime.now(UTC),now or datetime.now(UTC),'truefx'))
    with factory.begin() as session:fx.get_quote(session,force=True)
    with factory.begin() as session:
        assert allocation_fraction(session,uid)==Decimal(140)/Decimal(270)
        assert strategy_service.portfolio_of(session,uid).cash==140
        assert session.get(StrategyCash,(uid,'strategy1')).balance==200
        pages=bot._portfolio_pages(session,uid,session.get(CashAccount,uid),[])
        assert '270.00 USD' in pages[0]


async def test_s1_pending_delivery_preserves_original_fx_timestamp(factory,live_fx):
    from quantbot.models import UserSignal
    uid=add_user(factory,1,cash='1000')
    with factory.begin() as session:
        set_strategy_cash(session,session.get(User,uid),0,1000,'AUD',actor=1)
        run=make_run();row,_=strategy_service.store_run(session,run)
        data=strategy_service.signals_for(session,uid,run,SETTINGS)
        strategy_service.store_user_signals(session,row,run,uid,data)
    from sqlalchemy import select
    with factory.begin() as session:
        rows=list(session.scalars(select(UserSignal).where(UserSignal.user_id==uid)))
        assert rows and all(r.fx_note==data['cash_conversion'] for r in rows)
    app=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),bot_data={'session_factory':factory})
    sent,_=await strategy_service.deliver_pending(app)
    assert sent==1
    assert data['cash_conversion'] in app.bot.send_message.call_args.args[1]


async def test_fx_command_is_automatic_and_does_not_change_cash(factory,accounts,live_fx):
    uid=add_user(factory,999,cash='100')
    ctx=context(factory,accounts);ctx.args=[]
    message=Message(text='/fx')
    await fx_command(make_update(message,user_id=999),ctx)
    assert '自动汇率' in message.replies[-1][0] and 'TrueFX' in message.replies[-1][0]
    assert '无需手动' in message.replies[-1][0]
    with factory.begin() as session:assert session.get(CashAccount,uid).balance==100
    ctx.args=['.65'];message=Message(text='/fx .65')
    await fx_command(make_update(message,user_id=999),ctx)
    assert '无需手动' in message.replies[-1][0]


@pytest.mark.parametrize('status',['unregistered','subscription_required','strategy_required','paid','admin'])
def test_fx_menu_scoped_to_premium(status):
    assert ('fx' in {r.command for r in bot._menu_for(status,{'strategy2'} if status in {'paid','admin'} else set())})==(status in {'strategy_required','paid','admin'})


def test_provider_never_leaks_api_key(monkeypatch):
    monkeypatch.setenv('FX_PROVIDER','twelvedata');monkeypatch.setenv('TWELVE_DATA_API_KEY','secret-test-value')
    def fail(*args,**kwargs):raise OSError('url?apikey=secret-test-value')
    monkeypatch.setattr(fx,'urlopen',fail)
    with pytest.raises(ValueError) as e:fx.fetch_quote(datetime.now(UTC))
    assert 'secret-test-value' not in str(e.value)


def test_aud_migration_preserves_existing_usd_budget(tmp_path,monkeypatch):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    url='sqlite:///'+str(tmp_path/'migration.db')
    monkeypatch.chdir(root);monkeypatch.setenv('DATABASE_URL',url)
    cfg=Config()
    cfg.set_main_option('script_location',str(root/'alembic'))
    cfg.set_main_option('sqlalchemy.url',url)
    command.upgrade(cfg,'0016_daily_report_version')
    engine=create_engine(url)
    with engine.begin() as con:
        con.execute(text("INSERT INTO users (id,telegram_id,phone_fingerprint,role,is_active,created_at,updated_at) VALUES ('u',1,'hash','USER',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        con.execute(text("INSERT INTO strategy_cash (user_id,strategy_id,balance) VALUES ('u','strategy1',200)"))
    command.upgrade(cfg,'head')
    with engine.begin() as con:
        assert con.execute(text('SELECT balance,currency FROM strategy_cash')).one()==(200,'USD')
        con.execute(text("INSERT INTO fx_quotes (pair,aud_usd,observed_at,fetched_at,source) VALUES ('AUDUSD',.65,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'truefx')"))
    command.downgrade(cfg,'0016_daily_report_version')
    with engine.begin() as con:assert con.execute(text('SELECT balance FROM strategy_cash')).scalar()==200
    engine.dispose()
