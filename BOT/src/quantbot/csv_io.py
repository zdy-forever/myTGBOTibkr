from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from .allocations import cash_budgets, set_strategy_cash
from .display import plain
from .models import User
from .services import (
    PortfolioService,
    decimal_value,
    normalize_currency,
    normalize_symbol,
)

MONEY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]{3})\s*$")


@dataclass(frozen=True)
class ImportResult:
    positions_created: int
    cash_updated: bool


def _money(value: str) -> tuple[Decimal, str]:
    match = MONEY_RE.match(value)
    if not match:
        raise ValueError(f"invalid money value: {value}")
    return decimal_value(match.group(1), max_places=8), normalize_currency(match.group(2))


def import_portfolio_csv(session: Session, user: User, content: str, *, actor: int,
                         replace: bool = False) -> ImportResult:
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    required = {"Company", "Shares Held", "Average Price"}
    if len(reader.fieldnames or ()) != 3 or set(reader.fieldnames or ()) != required:
        raise ValueError("CSV header must be Company,Shares Held,Average Price")
    service = PortfolioService()
    parsed_positions: list[tuple[str, Decimal, Decimal, str]] = []
    cash_rows = {}
    seen: set[str] = set()
    for line_no, row in enumerate(reader, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"line {line_no}: 每行必须恰有三列")
        company = (row["Company"] or "").strip()
        stock_cash = company.upper() == "STOCK:CASH"
        if stock_cash:
            company = "CASH"
        if not stock_cash and company.lower() in {"current_cash", "cash"}:
            number = 0
            if company.lower() == "cash":
                value = (row["Shares Held"] or "").strip()
                if not re.fullmatch(r"[0-9]{1,4}", value):
                    raise ValueError(f"line {line_no}: CASH 第二列须为策略编号，0 表示公共现金")
                number = int(value)
            if number in cash_rows:
                raise ValueError("CSV contains multiple cash rows for the same strategy")
            amount, currency = _money(row["Average Price"])
            if number and currency != "USD":
                raise ValueError("独立策略现金只接受 USD")
            cash_rows[number] = (amount, currency)
            continue
        symbol = normalize_symbol(company)
        if symbol in seen:
            raise ValueError(f"line {line_no}: duplicate symbol {symbol}")
        seen.add(symbol)
        shares = decimal_value(row["Shares Held"], positive=True)
        cost, currency = _money(row["Average Price"])
        parsed_positions.append((symbol, shares, cost, currency))

    if 0 in cash_rows and len(cash_rows) != 1:
        raise ValueError("CASH 0 必须是唯一现金行，不能与 CASH 1、2、3 混用")
    existing = {p.symbol: p for p in service.list_positions(session, user.id)}
    if not replace and any(symbol in existing for symbol, *_ in parsed_positions):
        raise ValueError("CSV contains positions that already exist; use replace mode")
    if replace:
        for position in existing.values():
            service.remove_position(session, user, position.symbol, position.exchange, actor=actor)
    for symbol, shares, cost, currency in parsed_positions:
        service.add_position(session, user, symbol, shares, cost, currency, actor=actor)
    for number, (amount, currency) in cash_rows.items():
        set_strategy_cash(session, user, number, amount, currency, actor=actor)
    return ImportResult(len(parsed_positions), bool(cash_rows))


def export_portfolio_csv(session: Session, user: User) -> str:
    service = PortfolioService()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["Company", "Shares Held", "Average Price"])
    cash = service.cash(session, user.id)
    budgets = cash_budgets(session, user.id)
    if budgets:
        for row in budgets:
            writer.writerow(["CASH", row.strategy_id.removeprefix("strategy"),
                             f"{plain(row.balance, 2, 8)} USD"])
    else:
        writer.writerow(["CASH", 0, f"{plain(cash.balance, 2, 8)} {cash.currency}"])
    for position in service.list_positions(session, user.id):
        company = "STOCK:CASH" if position.symbol == "CASH" else position.symbol
        writer.writerow([company, plain(position.shares, 0, 10),
                         f"{plain(position.average_cost, 2, 8)} {position.currency}"])
    return output.getvalue()
