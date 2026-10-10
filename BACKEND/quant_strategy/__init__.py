"""Strategy module for the multi-user portfolio bot (Phase 2): shared market run + per-user signals. No Telegram, no database, no orders.

    from quant_strategy import StrategyEngine, parse_portfolio_csv, signals_for_user, config
"""
from . import config
from .alpaca_client import AlpacaReadOnly
from .engine import RunResult, StrategyEngine
from .portfolio_csv import Holding, Portfolio, PortfolioCsvError, parse_portfolio_csv
from .sec_fundamentals import SecClient
from .signals import Signal, UserSignals, signals_for_user
from .versioning import strategy_info, strategy_version

__all__ = ['config', 'AlpacaReadOnly', 'RunResult', 'StrategyEngine', 'Holding', 'Portfolio', 'PortfolioCsvError', 'parse_portfolio_csv', 'SecClient', 'Signal', 'UserSignals',
           'signals_for_user', 'strategy_info', 'strategy_version']
