"""Settings of the strategy module. The chosen strategy mirrors the QuantConnect version (R5_K1A_K5x, ETF pool, 5 leaders per theme, extra tech ETFs,
overlap removed). The module has NO Telegram or database code: the host bot passes users' portfolios in and stores the results.
Secrets are passed explicitly (Settings(...)) or read from environment variables; nothing secret is ever logged."""
import os
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('QUANT_STRATEGY_HOME', HERE.parent / 'var'))      # pool.json and the SEC cache live here (writable, git-ignored)
CACHE_DIR = DATA_DIR / 'cache'

STRATEGY_NAME = 'R5_K1A_K5x_ETF5_TECH'
STRATEGY = dict(leaders_per_group=5, extra_tech_etfs=True, dedupe_overlap=True, target_weight_floor=0.037,
                decision_frequency='monthly', min_buy_score=0)      # same as OnlineTest CONFIG; this module only supports a monthly run, min_buy_score 0 = no total-score floor
FACTOR_ETFS = ('SPY', 'IWM', 'QQQ')
BAR_HISTORY_DAYS = 420          # calendar days of daily bars (>= 252 trading days for the beta regression)
POOL_MAX_AGE_DAYS = 400
STALE_BAR_DAYS = 5              # latest price bar older than this: no signals are produced


def load_env(path=HERE.parent / '.env'):
    """Optional minimal .env reader (KEY=VALUE per line). Existing environment variables win."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    alpaca_key: str = ''
    alpaca_secret: str = ''
    alpaca_data_url: str = 'https://data.alpaca.markets'
    alpaca_trading_url: str = 'https://paper-api.alpaca.markets'      # only used for the calendar / clock (read only)
    alpaca_feed: str = 'iex'                                          # 'iex' is free; 'sip' needs a paid data plan
    dollar_volume_multiplier: float = 1.0                             # IEX volume is a small share of the market: raise (e.g. 30) to approximate consolidated volume
    sec_user_agent: str = ''
    default_deposit_usd: float = 200.0                                # monthly amount used when a user has no plan (research: A$300 ~ US$200)
    min_sale_usd: float = 330.0                                       # ~A$500, smallest replacement / trim sale considered

    def validate(self):
        problems = []
        if not self.alpaca_key or not self.alpaca_secret:
            problems.append('ALPACA_API_KEY / ALPACA_API_SECRET are missing')
        if not self.sec_user_agent or '@' not in self.sec_user_agent:
            problems.append("SEC_USER_AGENT is missing: SEC requires a contact in the User-Agent, e.g. 'Your Name you@example.com'")
        return problems


def from_env() -> Settings:
    load_env()
    env = os.environ.get
    first = lambda *names: next((os.environ[n] for n in names if os.environ.get(n)), '')      # also accepts the Alpaca-dashboard style names (ALPACA_PAPER1_API_KEY_ID ...)
    return Settings(alpaca_key=first('ALPACA_API_KEY', 'ALPACA_API_KEY_ID', 'ALPACA_PAPER1_API_KEY_ID'), alpaca_secret=first('ALPACA_API_SECRET', 'ALPACA_API_SECRET_KEY', 'ALPACA_PAPER1_API_SECRET_KEY'), alpaca_data_url=env('ALPACA_DATA_URL', 'https://data.alpaca.markets').rstrip('/'),
                    alpaca_trading_url=env('ALPACA_TRADING_URL', 'https://paper-api.alpaca.markets').rstrip('/'), alpaca_feed=env('ALPACA_FEED', 'iex').lower(),
                    dollar_volume_multiplier=float(env('DOLLAR_VOLUME_MULTIPLIER', '1')), sec_user_agent=env('SEC_USER_AGENT', ''),
                    default_deposit_usd=float(env('DEFAULT_DEPOSIT_USD', '200')), min_sale_usd=float(env('MIN_SALE_USD', '330')))
