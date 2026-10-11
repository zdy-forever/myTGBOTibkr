"""Command line for developers and the server (the host bot uses the Python API instead):

  python -m quant_strategy.cli smoke                      check every connection (no trading, no secrets printed)
  python -m quant_strategy.cli refresh-pool               rebuild the ETF leaders pool from the latest SEC N-PORT filings
  python -m quant_strategy.cli run [--csv FILE] [--deposit USD]   one strategy run, then the signals for the portfolio in FILE (host CSV format)
"""
import argparse
import json
import sys
from datetime import date

from . import config, messages, pool as poolmod
from .alpaca_client import AlpacaReadOnly
from .engine import StrategyEngine
from .portfolio_csv import Portfolio, PortfolioCsvError, parse_portfolio_csv
from .sec_fundamentals import SecClient, metrics
from .signals import signals_for_user


def build(settings):
    problems = settings.validate()
    if problems:
        raise SystemExit('Configuration problems:\n  - ' + '\n  - '.join(problems))
    market = AlpacaReadOnly(settings.alpaca_key, settings.alpaca_secret, settings.alpaca_trading_url, settings.alpaca_data_url, settings.alpaca_feed)
    return market, SecClient(settings.sec_user_agent, config.CACHE_DIR)


def smoke(settings):
    market, sec = build(settings)
    ok = True
    for name, fn in (('Alpaca clock (read)', lambda: f"market open: {market.clock().get('is_open')}"),
                     ('Alpaca daily bars', lambda: f"{len(market.daily_bars(['SPY'], '2026-01-01'))} SPY bars on feed {settings.alpaca_feed}"),
                     ('SEC ticker file', lambda: f'{len(sec.tickers()[0])} tickers'),
                     ('SEC fundamentals (AAPL)', lambda: str(metrics(sec.companyfacts(sec.tickers()[0]['AAPL']), date.today(), 200.0)[1] or 'parsed'))):
        try:
            print(f'[ ok ] {name}: {fn()}')
        except Exception as exc:
            ok = False
            print(f'[FAIL] {name}: {type(exc).__name__}: {str(exc)[:160]}')
    print('ALL GOOD' if ok else 'SOME CHECKS FAILED')
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog='quant_strategy')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('smoke')
    sub.add_parser('refresh-pool')
    run = sub.add_parser('run')
    run.add_argument('--csv')
    run.add_argument('--deposit', type=float)
    run.add_argument('--json', action='store_true', help='print the run and the signals as JSON (what the host stores)')
    args = ap.parse_args(argv)
    settings = config.from_env()
    if args.cmd == 'smoke':
        return smoke(settings)
    market, sec = build(settings)
    if args.cmd == 'refresh-pool':
        _, rows = sec.tickers()
        built = poolmod.build_pool(rows)
        poolmod.save_pool(built)
        print(messages.pool_text(built))
        return 0
    portfolio = Portfolio()
    if args.csv:
        try:
            portfolio = parse_portfolio_csv(open(args.csv, encoding='utf-8').read())
        except PortfolioCsvError as exc:
            print('CSV problems:\n  ' + '\n  '.join(exc.problems))
            return 2
    engine = StrategyEngine(settings, market, sec)
    result = engine.run(extra_symbols=portfolio.holdings)
    signals = signals_for_user(result, portfolio, settings, args.deposit)
    if args.json:
        print(json.dumps(dict(run=result.to_dict(), signals=signals.to_dict()), indent=1, default=str))
    else:
        print(messages.run_text(result), '\n', messages.rank_text(result, {s: h.shares * result.prices.get(s, h.avg_price) for s, h in portfolio.holdings.items()}), '\n', messages.signals_text(signals), sep='\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
