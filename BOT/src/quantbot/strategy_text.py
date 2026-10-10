"""Chinese rendering of one user's strategy results, in Telegram HTML (bold headings, aligned <pre> tables). Every dynamic piece goes through html.escape.
`signal_line` is plain text and is what user_signals.recommendation stores. No rule details and no component values are shown anywhere."""
from __future__ import annotations

import math
from html import escape as esc

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


def _detail_text(signal: dict) -> str:
    parts = []
    if signal.get("rank"):
        parts.append(f"排名第 {signal['rank']}")
    parts.append(KIND_TITLE.get(signal["kind"], signal["kind"]))
    return " · ".join(parts)


def signal_line(signal: dict, *, split: bool = False) -> str:
    """One suggestion as plain text (stored in user_signals.recommendation and used in the push)."""
    icon, verb = ("🔴", "卖出") if signal["action"] == "SELL" else ("🟢", "买入")
    return f"{icon} {verb} {signal['symbol']} {_amount_text(signal)}\n    {_detail_text(signal)}"


def signal_html(signal: dict) -> str:
    icon, verb = ("🔴", "卖出") if signal["action"] == "SELL" else ("🟢", "买入")
    return f"{icon} <b>{verb} {esc(signal['symbol'])}</b>  {esc(_amount_text(signal))}\n　<i>{esc(_detail_text(signal))}</i>"


def render_user_signals(data: dict) -> str:
    """`data` is UserSignals.to_dict()."""
    status = data["status"]
    if status == "run_failed":
        return "本月策略运行失败，暂时没有建议。管理员会处理。"
    if status == "stale_data":
        return "行情数据已过期，暂时没有建议。"
    summary = data["summary"]
    lines = [f"💼 <b>你的组合</b>  约 ${summary['equity']:,.0f}（现金 ${summary['cash']:,.0f}）", ""]
    signals = data["signals"]
    if signals:
        lines.append("💡 <b>操作建议</b>")
        lines += [signal_html(s) for s in signals]
    else:
        lines.append("💡 <b>操作建议</b>：本月没有。")
    top = summary.get("top_candidates") or []
    if top:
        lines += ["", "📈 <b>排名靠前的可买入</b>", esc(" › ".join(sym for sym, _ in top[:6]))]
    notes = [f"ℹ️ {esc(_note(n))}" for n in data.get("notes", [])[:4]]
    if notes:
        lines += [""] + notes
    lines += ["", DISCLAIMER_HTML]
    return "\n".join(lines)


def render_check(run, scores: dict, held_values: dict, data: dict, min_buy_score: float, top: int = 10) -> str:
    """/check for one user. `scores` = that user's scores for every scored stock (their own D included), `data` = UserSignals.to_dict()."""
    total_held = sum(held_values.values())
    summary = data.get("summary") or {}
    equity = summary.get("equity") or total_held or 1.0

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    rank = {s: i for i, (s, _) in enumerate(ranked, 1)}

    def row(symbol, sc):
        ok = sc["gate_ok"] and sc["total"] >= min_buy_score
        flag = "📌" if symbol in held_values else ("✅" if ok else "🔒")
        held_note = f"  持仓{held_values[symbol] / equity:.0%}" if symbol in held_values else ""
        return f"{rank[symbol]:>2}  {flag} {esc(symbol):<6}{held_note}"

    lines = ["📊 <b>策略检查</b>", f"📅 行情日期 {esc(run.as_of_bar or '无')}　更新于 {esc(run.started_at[5:16].replace('T', ' '))} UTC", "",
             f"🏆 <b>排名前 {min(top, len(ranked))}</b>（共 {len(ranked)} 只参与排名）", "<pre>" + "\n".join(row(s, sc) for s, sc in ranked[:top]) + "</pre>"]
    mine = sorted(((s, scores[s]) for s in held_values if s in scores), key=lambda kv: (-kv[1]["total"], kv[0]))
    if mine:
        rows = [f"{i:>2}  📌 {esc(s):<6} 持仓{held_values[s] / equity:.0%}" for i, (s, _) in enumerate(mine, 1)]       # ranked among the holdings themselves: 1, 2, 3, ...
        lines += ["💼 <b>你的持仓排名</b>", "<pre>" + "\n".join(rows) + "</pre>"]
    unscored = [s for s in held_values if s not in scores]
    if unscored:
        lines.append("⚪ 没有评分的持仓（不在股票池，或缺少数据）：" + esc("、".join(sorted(unscored))))
    lines += ["<i>✅可买入　🔒暂不可买　📌你已持有</i>", "", "━━━━━━━━━━━━━━", "", render_user_signals(data)]
    return "\n".join(lines)


def render_cooldowns(blocked: dict, protected: dict, now) -> str:
    """blocked / protected: {symbol: datetime until which the rule applies}; only entries still in force are shown."""
    live_blocked = sorted((until, s) for s, until in blocked.items() if until > now)
    live_protected = sorted((until, s) for s, until in protected.items() if until > now)
    if not live_blocked and not live_protected:
        return "🧊 目前没有冷却期的股票。"
    lines = ["🧊 <b>冷却期内的股票</b>", "<i>根据你以前收到的换仓/减仓建议</i>"]
    if live_blocked:
        lines += ["", "⛔ <b>暂不会再建议买入</b>", "<pre>" + "\n".join(f"{esc(s):<6} 到 {until:%Y-%m-%d}" for until, s in live_blocked) + "</pre>"]
    if live_protected:
        lines += ["", "🛡 <b>暂不会建议卖出</b>", "<pre>" + "\n".join(f"{esc(s):<6} 到 {until:%Y-%m-%d}" for until, s in live_protected) + "</pre>"]
    lines += ["", "<i>这只影响系统给你的建议；你自己想买卖什么不受限制。</i>"]
    return "\n".join(lines)


def _note(note: str) -> str:
    if note.startswith("no amount to invest"):
        return "你的现金为 0，无法给出买入金额；用 /cash 填写可投入的现金后再查看。"
    if note.startswith("no buyable candidate"):
        return "目前没有符合条件的可买入标的。"
    return note
