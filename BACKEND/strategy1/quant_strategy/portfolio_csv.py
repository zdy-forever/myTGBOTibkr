"""Portfolio input compatible with the host bot's CSV:

    Company,Shares Held,Average Price
    current_cash,0,1000 USD          <- Shares Held = cash mode (0 manual, 1 monthly, 2 weekly, 3 quarterly, 4 yearly); Average Price = balance + currency
    ISRG,0.55,374.21 USD

Optional extra columns: Exchange, Date (acquisition date, YYYYMMDD or YYYY-MM-DD; used only to estimate holding age for the trim friction test).
The parser validates everything before returning anything (the host imports inside one transaction) and reports the CSV line of every problem."""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

CASH_MODES = {0: 'manual', 1: 'monthly', 2: 'weekly', 3: 'quarterly', 4: 'yearly'}
SYMBOL = re.compile(r'[A-Z][A-Z0-9.\-]{0,9}')


class PortfolioCsvError(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__('; '.join(self.problems))


@dataclass
class Holding:
    symbol: str
    shares: float
    avg_price: float
    currency: str = 'USD'
    exchange: str = 'US'
    acquired_on: date = None


@dataclass
class Portfolio:
    holdings: dict = field(default_factory=dict)
    cash: float = 0.0
    currency: str = 'USD'
    cash_mode: int = 0
    warnings: list = field(default_factory=list)


def parse_money(text):
    """'1,000.50 USD' -> (1000.5, 'USD'); a bare number is USD."""
    parts = str(text).strip().replace(',', '').split()
    if not parts or len(parts) > 2:
        raise ValueError(f'cannot read amount {text!r}')
    value = float(parts[0])
    currency = parts[1].upper() if len(parts) == 2 else 'USD'
    if not re.fullmatch(r'[A-Z]{3}', currency) or value != value or value in (float('inf'), float('-inf')):
        raise ValueError(f'cannot read amount {text!r}')
    return value, currency


def parse_date(text):
    text = str(text).strip()
    for fmt in ('%Y%m%d', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f'cannot read date {text!r}')


def parse_portfolio_csv(text) -> Portfolio:
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿')))
    names = {(n or '').strip().lower(): n for n in (reader.fieldnames or [])}
    missing = [c for c in ('company', 'shares held', 'average price') if c not in names]
    if missing:
        raise PortfolioCsvError([f'missing column(s): {", ".join(missing)} (expected: Company,Shares Held,Average Price)'])
    problems, portfolio, seen_cash = [], Portfolio(), False
    for line, row in enumerate(reader, start=2):
        get = lambda key: (row.get(names[key]) or '').strip() if key in names else ''
        symbol = get('company').upper()
        if not symbol:
            continue
        try:
            if symbol == 'CURRENT_CASH':
                if seen_cash:
                    raise ValueError('current_cash appears twice')
                seen_cash = True
                portfolio.cash, portfolio.currency = parse_money(get('average price'))      # the second column of this row is ignored
                if portfolio.cash < 0:
                    raise ValueError('cash cannot be negative')
                continue
            if not SYMBOL.fullmatch(symbol):
                raise ValueError(f'invalid symbol {symbol!r}')
            if symbol in portfolio.holdings:
                raise ValueError(f'{symbol} appears twice')
            shares = float(get('shares held'))
            price, currency = parse_money(get('average price'))
            if not shares > 0 or not price > 0:
                raise ValueError('shares and average price must be positive')
            when = parse_date(get('date')) if get('date') else None
            portfolio.holdings[symbol] = Holding(symbol, shares, price, currency, get('exchange').upper() or 'US', when)
        except ValueError as exc:
            problems.append(f'line {line} ({symbol}): {exc}')
    if problems:
        raise PortfolioCsvError(problems)
    non_usd = sorted({h.currency for h in portfolio.holdings.values() if h.currency != 'USD'} | ({portfolio.currency} if portfolio.currency != 'USD' else set()))
    if non_usd:
        portfolio.warnings.append(f'amounts in {", ".join(non_usd)} are not converted: only USD amounts take part in the analysis')
    return portfolio
