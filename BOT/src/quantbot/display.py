"""Human-readable numbers. The database keeps money and quantities as fixed-point decimals with 8-10 places, and Python prints those as `0E-8` or `0.5500000000`;
nothing the user reads or exports should look like that."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def plain(value, min_places: int = 0, max_places: int = 10) -> str:
    """Fixed-point text without an exponent: at most `max_places` decimals, trailing zeros trimmed down to `min_places`."""
    source = Decimal(str(value))
    if not source.is_finite():
        return "数值异常"
    try:
        number = source.quantize(Decimal(1).scaleb(-max_places), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return "数值异常"
    text = format(number, "f")
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0").ljust(min_places, "0")
    if whole == "-0" and not frac.strip("0"):
        whole = "0"
    return f"{whole}.{frac}" if frac else whole


def money(value) -> str:
    """1,000.00 (for messages)."""
    source = Decimal(str(value))
    if not source.is_finite():
        return "数值异常"
    try:
        number = source.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return "数值异常"
    return format(number, ",f")


def qty(value) -> str:
    """0.55, 3 (share counts)."""
    return plain(value, 0, 10)


def price(value) -> str:
    """374.21, 120.00, 0.0123 (prices and average costs: at least 2 decimals, up to 8)."""
    return plain(value, 2, 8)


def render_portfolio(cash, positions) -> str:
    """/portfolio in Telegram HTML: cash line plus an aligned table of positions. `cash` has balance/currency, `positions` symbol/exchange/shares/average_cost/currency."""
    from html import escape as esc

    lines = ["💼 <b>我的持仓</b>", f"💵 现金　<b>{money(cash.balance)} {esc(cash.currency)}</b>"]
    if not cash.balance:
        lines.append("<i>现金为 0：用 /cash 填写可以投入的现金，才会有买入建议。</i>")
    lines.append("")
    if not positions:
        lines.append("还没有持仓：用 /add 逐只添加，或 /import 导入 CSV。")
        return "\n".join(lines)
    rows = [(f"{p.symbol}@{p.exchange}" if p.exchange != "US" else p.symbol, qty(p.shares), f"{price(p.average_cost)} {p.currency}") for p in positions]
    w0, w1 = max(len(r[0]) for r in rows + [("代码", "", "")]), max(len(r[1]) for r in rows + [("", "股数", "")])
    table = [f"{'代码':<{w0 - 1}}  {'股数':>{w1}}  均价"] + [f"{a:<{w0}}  {b:>{w1}}  {c}" for a, b, c in rows]
    lines += [f"📋 <b>{len(positions)} 只</b>", "<pre>" + "\n".join(esc(t) for t in table) + "</pre>"]
    return "\n".join(lines)


def render_portfolio_pages(cash, positions) -> list[str]:
    """Return complete HTML pages, each comfortably below Telegram's text limit."""
    complete = render_portfolio(cash, positions)
    if len(complete) <= 3800:
        return [complete]
    from html import escape as esc

    rows = [(f"{p.symbol}@{p.exchange}" if p.exchange != "US" else p.symbol,
             qty(p.shares), f"{price(p.average_cost)} {p.currency}") for p in positions]
    name_width = max(len(row[0]) for row in rows)
    share_width = max(len(row[1]) for row in rows)
    heading = f"{'代码':<{name_width - 1}}  {'股数':>{share_width}}  均价"
    formatted = [esc(f"{name:<{name_width}}  {shares:>{share_width}}  {cost}")
                 for name, shares, cost in rows]
    chunks: list[list[str]] = []
    chunk: list[str] = []
    length = 0
    for line in formatted:
        if chunk and length + len(line) + 1 > 3400:
            chunks.append(chunk)
            chunk, length = [], 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        chunks.append(chunk)
    pages = []
    for index, lines in enumerate(chunks, 1):
        page = (f"💼 <b>我的持仓</b>　第 {index}/{len(chunks)} 页\n"
                f"💵 现金　<b>{money(cash.balance)} {esc(cash.currency)}</b>\n\n"
                f"📋 <b>共 {len(positions)} 只</b>\n<pre>{esc(heading)}\n"
                + "\n".join(lines) + "</pre>")
        pages.append(page)
    return pages
