"""Chinese rendering of one user's strategy results, in Telegram HTML (bold headings, aligned <pre> tables). Every dynamic piece goes through html.escape.
`signal_line` is plain text and is what user_signals.recommendation stores.
Manual S1 checks show actual totals and native component points as requested;
decision thresholds and scoring formulas stay private."""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from html import escape as esc
from zoneinfo import ZoneInfo

DISCLAIMER = "仅供参考，不构成投资建议；系统不会下单，买卖由你自己决定。"
DISCLAIMER_HTML = f"⚠️ <i>{DISCLAIMER}</i>"

KIND_TITLE = {
    "K1A_REPLACE": "换仓",
    "K5X_TRIM": "减仓换股",
    "MONTHLY_BUY": "本月买入",
}


def _amount_text(signal: dict) -> str:
    shares = signal.get("est_shares")
    share_text = f"（约 {shares:.2f} 股）" if shares and math.isfinite(shares) else ""
    return f"约 ${signal['amount_usd']:,.0f}{share_text}"


def signal_line(signal: dict, *, split: bool = False) -> str:
    """One suggestion as plain text (stored in user_signals.recommendation and used in the push)."""
    icon, verb = ("🔴", "卖出") if signal["action"] == "SELL" else ("🟢", "买入")
    return f"{icon} {verb} {signal['symbol']} {_amount_text(signal)}"


def signal_html(signal: dict) -> str:
    icon, verb = ("🔴", "卖出") if signal["action"] == "SELL" else ("🟢", "买入")
    return f"{icon} <b>{verb} {esc(signal['symbol'])}</b>  {esc(_amount_text(signal))}"


def public_recommendation(text: str) -> str:
    """Also hide explanatory lines on pending messages saved by older versions."""
    first = text.splitlines()[0] if text else ""
    for title in KIND_TITLE.values():
        first = first.replace(f"【{title}】", "")
    return first.strip()


def render_user_signals(data: dict) -> str:
    """`data` is UserSignals.to_dict()."""
    status = data["status"]
    if status == "run_failed":
        return "本月策略运行失败，暂时没有建议。管理员会处理。"
    if status == "stale_data":
        return "行情数据已过期，暂时没有建议。"
    summary = data["summary"]
    lines = [f"💼 <b>你的组合</b>  约 ${summary['equity']:,.0f}（现金 ${summary['cash']:,.0f}）", ""]
    if data.get('cash_conversion'):
        lines += [esc(data['cash_conversion']), '']
    signals = data["signals"]
    if signals:
        lines.append("💡 <b>操作建议</b>")
        lines += [signal_html(s) for s in signals]
    else:
        lines.append("💡 <b>操作建议</b>：本月没有。")
    notes = [f"ℹ️ {esc(_note(n))}" for n in data.get("notes", [])[:4]]
    if notes:
        lines += [""] + notes
    lines += ["", DISCLAIMER_HTML]
    return "\n".join(lines)


def _score_details(sc: dict) -> str:
    """Native component scales, matching the backend's reversed-P ranking view."""
    from quant_strategy.logic import G_POINTS, NATIVE

    def points(key, *, reverse=False):
        value = sc.get(key)
        if value is None or not math.isfinite(value):
            return '—'
        if reverse:
            value = NATIVE['P'] - value
        return f'{value:.1f}'

    return (f"    P {points('p_native', reverse=True)}/30  V {points('v_native')}/25  F {points('f_native')}/15\n"
            f"    G {G_POINTS:.1f}/15  D {points('d_native')}/10  Q {points('q_native')}/5")


def render_check(run, scores: dict, held_values: dict, data: dict, min_buy_score: float, top: int = 10) -> str:
    """/check for one user. `scores` = that user's scores for every scored stock (their own D included), `data` = UserSignals.to_dict()."""
    total_held = sum(held_values.values())
    summary = data.get("summary") or {}
    stock_value = total_held or 1.0
    buyable = set(summary["buyable_symbols"]) if "buyable_symbols" in summary else None

    ranked = sorted(((s, sc) for s, sc in scores.items() if s in run.symbols), key=lambda kv: (-kv[1]["total"], kv[0]))
    rank = {s: i for i, (s, _) in enumerate(ranked, 1)}

    def row(symbol, sc):
        ok = symbol in buyable if buyable is not None else sc["gate_ok"] and sc["total"] >= min_buy_score
        flag = "📌" if symbol in held_values else ("✅" if ok else "🔒")
        held_note = f"  持仓{held_values[symbol] / stock_value:.0%}" if symbol in held_values else ""
        return f"{rank[symbol]:>2}  {flag} {esc(symbol):<6}  {sc['total']:5.1f} 分{held_note}\n{_score_details(sc)}"

    updated = datetime.fromisoformat(run.finished_at or run.started_at)
    updated = (updated if updated.tzinfo else updated.replace(tzinfo=UTC)).astimezone(ZoneInfo("Asia/Shanghai"))
    lines = ["📊 <b>策略检查</b>", f"📅 行情日期 {esc(run.as_of_bar or '无')}\n更新于 {updated:%Y-%m-%d %H:%M}（北京时间）", "",
             f"🏆 <b>排名前 {min(top, len(ranked))}</b>（共 {len(ranked)} 只参与排名）",
             "<i>评分为策略总分（0–100），按你当前的组合计算；分数高不代表一定买入。</i>",
             "<i>分项满分：P /30、V /25、F /15、G /15、D /10、Q /5。P显示反向处理后的分值；总分经权重换算，分项不直接相加。</i>",
             "<pre>" + "\n".join(row(s, sc) for s, sc in ranked[:top]) + "</pre>"]
    mine = sorted(((s, scores[s]) for s in held_values if s in scores), key=lambda kv: (-kv[1]["total"], kv[0]))
    if mine:
        rows = [f"{i:>2}  📌 {esc(s):<6}  {sc['total']:5.1f} 分  持仓{held_values[s] / stock_value:.0%}\n{_score_details(sc)}" for i, (s, sc) in enumerate(mine, 1)]
        lines += ["💼 <b>你的持仓排名</b>", "<i>占比按股票市值计算，不含现金</i>", "<pre>" + "\n".join(rows) + "</pre>"]
    unscored = [s for s in held_values if s not in scores]
    if unscored:
        lines.append("⚪ 暂无法评分的持仓（必要数据不足）：" + esc("、".join(sorted(unscored))))
    lines += ["<i>✅符合买入条件　🔒暂不符合买入条件（不等于冷却期）　📌你已持有</i>", "", "━━━━━━━━━━━━━━", "", render_user_signals(data)]
    return "\n".join(lines)


def check_pages(text: str, limit: int = 3800) -> list[str]:
    """Split long checks on complete lines/table rows, retaining valid HTML."""
    if len(text) <= limit:
        return [text]
    blocks = []
    for part in re.split(r'(<pre>.*?</pre>)', text, flags=re.S):
        if part.startswith('<pre>'):
            records = []
            for row in part[5:-6].splitlines():
                if row.startswith('    ') and records:
                    records[-1] += '\n' + row
                else:
                    records.append(row)
            rows = []
            for row in records:
                if rows and len('\n'.join(rows + [row])) + 11 > limit:
                    blocks.append('<pre>' + '\n'.join(rows) + '</pre>')
                    rows = []
                rows.append(row)
            if rows:
                blocks.append('<pre>' + '\n'.join(rows) + '</pre>')
        else:
            for line in part.splitlines():
                if len(line) > limit and '、' in line:
                    group = ''
                    for item in line.split('、'):
                        if group and len(group) + len(item) + 1 > limit:
                            blocks.append(group)
                            group = ''
                        group += ('、' if group else '') + item
                    blocks.append(group)
                else:
                    blocks.append(line)
    pages, current = [], ''
    for block in blocks:
        candidate = current + ('\n' if current else '') + block
        if len(candidate) > limit and current:
            pages.append(current)
            current = block
        else:
            current = candidate
    if current:
        pages.append(current)
    return pages


def render_cooldowns(blocked: dict, protected: dict, now) -> str:
    """blocked / protected: {symbol: datetime until which the rule applies}; only entries still in force are shown."""
    live_blocked = sorted((until, s) for s, until in blocked.items() if until > now)
    live_protected = sorted((until, s) for s, until in protected.items() if until > now)
    if not live_blocked and not live_protected:
        return "🧊 目前没有冷却期的股票。"
    lines = ["🧊 <b>冷却期内的股票</b>", "<i>根据你记录的实际卖出及冷却期选择</i>"]
    if live_blocked:
        lines += ["", "⛔ <b>暂不会再建议买入</b>", "<pre>" + "\n".join(esc(s) for _, s in live_blocked) + "</pre>"]
    if live_protected:
        lines += ["", "🛡 <b>暂不会建议卖出</b>", "<pre>" + "\n".join(esc(s) for _, s in live_protected) + "</pre>"]
    lines += ["", "<i>这只影响系统给你的建议；你自己想买卖什么不受限制。</i>"]
    return "\n".join(lines)


def _note(note: str) -> str:
    if note.startswith("no amount to invest"):
        return "你的现金为 0，无法给出买入金额；用 /cash 填写可投入的现金后再查看。"
    if note.startswith("no buyable candidate"):
        return "目前没有符合条件的可买入标的。"
    if ": no market price" in note:
        return note.split(": no market price", 1)[0] + "：暂无行情价格，暂按你填写的平均成本估值。"
    if ": amount in " in note:
        return note.split(": amount in ", 1)[0] + "：非美元持仓未纳入分析。"
    if note.startswith("cash in "):
        return "非美元现金未纳入分析。"
    return "部分必要数据暂不可用。"
