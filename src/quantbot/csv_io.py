from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from .models import User
from .services import (
    FREQUENCY_TO_MODE,
    MODE_TO_FREQUENCY,
    PortfolioService,
    decimal_value,
    normalize_currency,
    normalize_symbol,
)

MONEY_RE = re.compile(r"^\s*([^\s]+)\s+([A-Za-z]{3})\s*$")


@dataclass(frozen=True)
class ImportResult:
    positions_created: int
    cash_updated: bool


def _money(value: str) -> tuple[Decimal, str]:
    match = MONEY_RE.match(value)
    if not match:
        raise ValueError(f"invalid money value: {value}")
    return decimal_value(match.group(1)), normalize_currency(match.group(2))


def import_portfolio_csv(session: Session, user: User, content: str, *, actor: int,
                         replace: bool = False) -> ImportResult:
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    required = {"Company", "Shares Held", "Average Price"}
    if set(reader.fieldnames or ()) != required:
        raise ValueError("CSV header must be Company,Shares Held,Average Price")
    service = PortfolioService()
    parsed_positions: list[tuple[str, Decimal, Decimal, str]] = []
    cash_row: tuple[int, Decimal, str] | None = None
    seen: set[str] = set()
    for line_no, row in enumerate(reader, start=2):
        company = (row["Company"] or "").strip()
        if company.lower() == "current_cash":
            if cash_row is not None:
                raise ValueError("CSV contains multiple current_cash rows")
            try:
                mode = int(row["Shares Held"])
            except ValueError as exc:
                raise ValueError(f"line {line_no}: invalid cash management mode") from exc
            if mode not in MODE_TO_FREQUENCY:
                raise ValueError(f"line {line_no}: cash mode must be 0 through 4")
            amount, currency = _money(row["Average Price"])
            cash_row = (mode, amount, currency)
            continue
        symbol = normalize_symbol(company)
        if symbol in seen:
            raise ValueError(f"line {line_no}: duplicate symbol {symbol}")
        seen.add(symbol)
        shares = decimal_value(row["Shares Held"], positive=True)
        cost, currency = _money(row["Average Price"])
        parsed_positions.append((symbol, shares, cost, currency))

    existing = {p.symbol: p for p in service.list_positions(session, user.id)}
    if not replace and any(symbol in existing for symbol, *_ in parsed_positions):
        raise ValueError("CSV contains positions that already exist; use replace mode")
    if replace:
        for position in existing.values():
            service.remove_position(session, user, position.symbol, position.exchange, actor=actor)
    for symbol, shares, cost, currency in parsed_positions:
        service.add_position(session, user, symbol, shares, cost, currency, actor=actor)
    if cash_row:
        mode, amount, currency = cash_row
        cash = service.set_cash(session, user, amount, currency, actor=actor)
        cash.management_mode = MODE_TO_FREQUENCY[mode]
    return ImportResult(len(parsed_positions), cash_row is not None)


def export_portfolio_csv(session: Session, user: User) -> str:
    service = PortfolioService()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["Company", "Shares Held", "Average Price"])
    cash = service.cash(session, user.id)
    writer.writerow(["current_cash", FREQUENCY_TO_MODE[cash.management_mode],
                     f"{cash.balance} {cash.currency}"])
    for position in service.list_positions(session, user.id):
        writer.writerow([position.symbol, position.shares,
                         f"{position.average_cost} {position.currency}"])
    return output.getvalue()
