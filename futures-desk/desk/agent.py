"""Answers trading questions from the desk's own scan, backtests and symbol detail.

There is no outside model. Every sentence is built from numbers the desk already computed.
"""
from __future__ import annotations

import re
from typing import Any

from . import service

_STOP = {
    "now", "net", "be", "arm", "all", "can", "for", "the", "and", "top", "day", "orb",
    "rsi", "ema", "macd", "buy", "sell", "long", "short", "open", "high", "low", "risk",
    "news", "what", "when", "best", "today", "this", "that", "with", "from", "your",
    "stop", "hold", "flat", "edge", "rule", "test", "data", "live", "chart", "how",
    "why", "any", "are", "its", "not", "but", "has", "was", "will", "should", "would",
}


def answer(market: str, question: str, risk: float | None = None) -> dict[str, Any]:
    q = (question or "").strip()
    if not q:
        return _out("Ask about today's buys or sells, a symbol, a strategy, or the opening range.")
    scan = service.scan(market, risk_budget=risk, with_news=True)
    if not scan.get("ready"):
        return _out("The desk is still loading data and running the backtests. Ask again in a minute.")
    symbols = _symbols(market, q)
    intent = _intent(q)
    if symbols and (intent["symbol"] or not (intent["book"] or intent["status"]) or intent["news"] or intent["size"]):
        if intent["book"] and not intent["symbol"]:
            return _books(market, scan)
        return _about_symbols(market, scan, symbols, risk, intent)
    if intent["book"]:
        return _books(market, scan)
    if intent["orb"]:
        return _orb(market)
    if intent["status"]:
        return _status(market, scan)
    if intent["buy"] or intent["sell"] or intent["today"]:
        return _lists(market, scan, buys=intent["buy"] or intent["today"], sells=intent["sell"] or intent["today"])
    if intent["size"]:
        return _out(
            f"Set Risk / trade in the header (now {_ccy(market)}{_num(risk or scan.get('risk_budget') or 0, 0)}). "
            "Each pick then shows how many lots or units fit that loss at the stop. "
            "Ask about a symbol, for example “how many lots of TCS”, for the size on that name."
        )
    return _fallback(market, scan)


def _out(text: str, chips: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"text": text.strip(), "chips": chips or []}


def _ccy(market: str) -> str:
    return "₹" if market == "IN" else "$"


def _num(v: Any, d: int = 2) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.{d}f}"


def _px(market: str, v: Any) -> str:
    return _ccy(market) + _num(v)


def _intent(q: str) -> dict[str, bool]:
    ql = q.lower()

    def has(*words: str) -> bool:
        return any(re.search(rf"\b{re.escape(w)}\b", ql) for w in words)

    buy = has("buy", "long", "longs", "bullish", "calls")
    sell = has("sell", "short", "shorts", "bearish", "puts")
    today = has("today", "recommend", "recommendation", "recommendations", "pick", "picks", "setup", "setups", "trade", "trades", "watchlist")
    return {
        "buy": buy,
        "sell": sell,
        "today": today and not (buy or sell),
        "book": has("backtest", "backtested", "strategy", "strategies", "playbook", "validated", "edge", "expectancy", "which rule", "what works", "win rate"),
        "orb": has("orb", "opening range", "opening-range", "first candle", "5 minute", "5-minute", "five minute"),
        "status": has("market open", "market closed", "session", "what time", "when does", "is the market", "hours"),
        "news": has("news", "headline", "headlines", "earnings"),
        "size": has("size", "lots", "lot", "quantity", "qty", "position", "risk", "how much", "how many"),
        "symbol": has("about", "why", "should", "tell me", "setup", "chart", "technical", "technicals", "news", "levels"),
    }


def _symbols(market: str, question: str) -> list[str]:
    insts = service.instruments(market)
    found: list[str] = []
    ql = question.lower()
    for inst in sorted(insts, key=lambda i: -len((i.get("name") or ""))):
        name = (inst.get("name") or "").strip()
        sid = inst["id"]
        if len(name) >= 4 and name.lower() != sid.lower() and re.search(rf"\b{re.escape(name.lower())}\b", ql):
            found.append(sid)
    ids = {i["id"].upper(): i["id"] for i in insts}
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9&]*", question):
        up = tok.upper().replace("&", "")
        # M&M style ids keep the ampersand
        key = tok.upper() if tok.upper() in ids else up
        if key not in ids:
            continue
        short = len(key) <= 3
        if tok.isupper() or (not short and tok.lower() not in _STOP) or (market == "IN" and len(key) >= 3 and tok.lower() not in _STOP):
            if market == "US" and short and not tok.isupper():
                continue
            found.append(ids[key])
    out: list[str] = []
    for sid in found:
        if sid not in out:
            out.append(sid)
    return out[:4]


def _tradable(scan: dict, side: str | None = None) -> list[dict]:
    rows = [p for p in scan.get("picks") or [] if p.get("grade") != "SKIP"]
    if side:
        rows = [p for p in rows if p["side"] == side]
    return rows


def _pick_line(market: str, p: dict) -> str:
    o = (p.get("playbook") or {}).get("oos") or {}
    hold = "day trade, flat by the close" if p.get("kind") == "day" else f"swing, review after {p.get('hold')} sessions"
    ev = ""
    if o.get("n"):
        ev = f" The rule's out-of-sample result is {o.get('win')}% wins, {float(o.get('exp') or 0):+.2f}R a trade, profit factor {o.get('pf')} over {o.get('n')} trades ({p.get('verdict')})."
    late = " The session is already open, so take it only if price is still near the entry." if p.get("late") else ""
    note = ""
    if p.get("grade") == "SKIP":
        note = " Skipped: " + "; ".join(p.get("notes") or []) + "."
    return (
        f"{p['action']} {p['symbol']} ({p.get('grade')}) via {p.get('rule_name')}, {hold}. "
        f"Entry {_px(market, p.get('entry'))}, stop {_px(market, p.get('stop'))}, target {_px(market, p.get('t1'))}."
        f"{ev}{late}{note}"
    )


def _chip(p: dict) -> dict[str, str]:
    return {"symbol": p["symbol"], "label": f"{p['action']} {p['symbol']}"}


def _honesty(scan: dict) -> str:
    val = [p for p in scan.get("playbooks") or [] if p.get("verdict") == "VALIDATED"]
    if not val:
        return "No rule has passed the out-of-sample test on this market. The names below are the highest-scoring ideas, and the desk would rather you trade them small or stay flat."
    names = ", ".join(f"{p['name']} ({p['side']})" for p in val)
    return f"The only rule that passed the out-of-sample test here is {names}. Prefer those setups over the rest."


def _lists(market: str, scan: dict, buys: bool, sells: bool) -> dict[str, Any]:
    parts = [_honesty(scan), f"Data is through {scan.get('as_of')}."]
    chips: list[dict[str, str]] = []
    if buys:
        rows = _tradable(scan, "long")[:5]
        parts.append("Buys:" if rows else "Buys: none that clear the filter today.")
        parts.extend(_pick_line(market, p) for p in rows)
        chips.extend(_chip(p) for p in rows)
    if sells:
        rows = _tradable(scan, "short")[:5]
        parts.append("Sells:" if rows else "Sells: none that clear the filter today.")
        parts.extend(_pick_line(market, p) for p in rows)
        chips.extend(_chip(p) for p in rows)
    hidden = scan.get("hidden_setups") or 0
    if hidden:
        parts.append(f"{hidden} other setups were hidden because their rule lost money out of sample.")
    return _out("\n\n".join(parts), chips)


def _books(market: str, scan: dict) -> dict[str, Any]:
    books = scan.get("playbooks") or []
    lines = [f"Backtest on the {scan.get('label')} universe, costs included ({(scan.get('cost') or 0) * 100:.2f}% round trip). The last 40% of dates were held out."]
    ranked = sorted(books, key=lambda p: ({"VALIDATED": 0, "MARGINAL": 1, "REJECTED": 2, "TOO_FEW": 3}.get(p.get("verdict"), 9), -((p.get("oos") or {}).get("exp") or -9)))
    shown = [p for p in ranked if p.get("verdict") in ("VALIDATED", "MARGINAL")] or ranked[:6]
    for p in shown:
        o = p.get("oos") or {}
        lines.append(
            f"{p.get('name')} ({p.get('side')}) is {p.get('verdict')}: {o.get('win')}% wins, {float(o.get('exp') or 0):+.2f}R a trade, "
            f"profit factor {o.get('pf')}, {o.get('n')} out-of-sample trades. {p.get('why') or ''}".strip()
        )
    if not any(p.get("verdict") == "VALIDATED" for p in books):
        lines.append("Nothing on this market cleared the bar for a validated edge.")
    return _out("\n\n".join(lines))


def _status(market: str, scan: dict) -> dict[str, Any]:
    st = scan.get("market_status") or {}
    state = st.get("state")
    label = scan.get("label") or market
    if state == "open":
        text = f"{label} is open ({st.get('now')}). Intraday charts refresh about every 20 seconds. Daily picks were built from the prior close."
    elif state == "pre":
        text = f"{label} is before the open ({st.get('now')}). Next open {st.get('next_open_ist')}."
    else:
        text = f"{label} is closed ({st.get('now')}). Next open {st.get('next_open_ist')}. The daily chart and today's list are from the last close ({scan.get('as_of')})."
    return _out(text)


def _orb(market: str) -> dict[str, Any]:
    board = service.orb_board(market)
    rows = board.get("rows") or []
    if not rows:
        return _out("No opening-range names yet. The board fills once the session has 5-minute bars.")
    fired = [r for r in rows if r.get("state") == "triggered"]
    waiting = [r for r in rows if r.get("state") in ("waiting", "forming")]
    lines = [
        "Opening-range breakout (Zarattini–Barbon–Aziz): trade the first 5-minute candle's direction on the highest relative-volume names, stop at the other end, flat at the close. This desk has not proved it yet — it is being forward-tested."
    ]
    if fired:
        lines.append("Triggered:")
        lines.extend(
            f"{r['symbol']} {r.get('direction') or ''} at {_px(market, r.get('entry'))}, stop {_px(market, r.get('stop'))}, now {r.get('r_now')}R, relative volume {r.get('rvol')}x."
            for r in fired[:5]
        )
    if waiting:
        lines.append("Still waiting on a break: " + ", ".join(r["symbol"] for r in waiting[:8]) + ".")
    stopped = [r for r in rows if r.get("state") in ("stopped", "failed")]
    if stopped:
        lines.append(f"{len(stopped)} of the top names have already stopped or failed.")
    chips = [{"symbol": r["symbol"], "label": r["symbol"]} for r in (fired or rows)[:5]]
    return _out("\n\n".join(lines), chips)


def _about_symbols(market: str, scan: dict, symbols: list[str], risk: float | None, intent: dict[str, bool]) -> dict[str, Any]:
    parts: list[str] = []
    chips: list[dict[str, str]] = []
    table = {r["symbol"]: r for r in scan.get("table") or []}
    picks = {p["symbol"]: p for p in scan.get("picks") or []}
    for sid in symbols:
        row = table.get(sid)
        if row is None and sid not in picks:
            parts.append(f"{sid} is not in this market's futures list.")
            continue
        detail = None
        err = ""
        try:
            detail = service.detail(market, sid, risk_budget=risk)
        except Exception as exc:  # noqa: BLE001
            err = f" Full news and levels could not be loaded ({exc})."
        parts.append(_symbol_text(market, sid, scan, row, picks.get(sid), detail, intent) + err)
        pick = picks.get(sid)
        if pick:
            chips.append(_chip(pick))
        else:
            chips.append({"symbol": sid, "label": sid})
    return _out("\n\n".join(p for p in parts if p), chips)


def _symbol_text(market: str, sid: str, scan: dict, row: dict | None, pick: dict | None, detail: dict | None, intent: dict[str, bool]) -> str:
    name = (detail or {}).get("name") or (row or {}).get("name") or sid
    title = name if name == sid else f"{sid} ({name})"
    tech = (detail or {}).get("tech") or {}
    close = tech.get("close") if tech else (row or {}).get("close")
    bits = [f"{title}, last close {_px(market, close)} as of {(detail or scan).get('as_of')}."]
    if row and row.get("chg") is not None:
        bits.append(f"Session change {float(row['chg']):+.2f}%, relative volume {row.get('rvol') or '—'}x, trend {row.get('trend') or 'n/a'}, RSI(14) {row.get('rsi14') if row.get('rsi14') is not None else '—'}.")
    if tech:
        rsi2 = "—" if tech.get("rsi2") is None else _num(tech.get("rsi2"), 1)
        if tech.get("ema20") and close is not None:
            where = "above" if close >= tech["ema20"] else "below"
            bits.append(f"RSI(2) {rsi2}, and price is {where} the 20-day average ({_px(market, tech.get('ema20'))}).")
        else:
            bits.append(f"RSI(2) {rsi2}.")
        # The ternary above is messy if the first branch doesn't include RSI. Rewrite below if needed.
    if tech.get("rating_label"):
        bits.append(f"TradingView rating: {tech['rating_label']}.")
    extra = (detail or {}).get("extra") or {}
    if extra.get("oi_read"):
        bits.append(f"Open interest: {extra['oi_read']} ({extra.get('oi_chg_pct'):+.1f}% OI)." if extra.get("oi_chg_pct") is not None else f"Open interest: {extra['oi_read']}.")
    nsum = (detail or {}).get("news_summary") or {}
    if nsum.get("count"):
        bits.append(f"News tape is {nsum.get('label')} across {nsum.get('count')} headlines.")
        head = ((detail or {}).get("news") or [{}])[0].get("title")
        if head:
            bits.append(f"Latest headline: {head}.")
    fund = (detail or {}).get("fundamentals") or {}
    if fund.get("available") and fund.get("quality"):
        bits.append(f"Fundamentals look {fund['quality']}.")
    if pick:
        bits.append(_pick_line(market, pick))
        sz = pick.get("sizing") or {}
        if intent["size"] and sz:
            if market == "IN":
                bits.append(
                    f"At your risk budget that is {sz.get('lots')} lot(s), {sz.get('qty')} quantity. "
                    f"One lot risks about {_px(market, sz.get('risk_per_lot'))}."
                    + ("" if sz.get("fits") else " One lot already exceeds the budget, so the desk sizes it at zero.")
                )
            else:
                bits.append(f"At your risk budget that is about {sz.get('qty')} units, notional {_px(market, sz.get('notional'))}.")
    else:
        bits.append("No setup on this name made today's list. Either no rule fired, or the rule that fired loses money out of sample and was hidden.")
    if pick and pick.get("verdict") not in ("VALIDATED", "MARGINAL"):
        bits.append("This idea did not come from a rule that passed the out-of-sample test.")
    # clean the awkward rsi sentence if the ternary built a fragment
    return " ".join(bits)


def _fallback(market: str, scan: dict) -> dict[str, Any]:
    buys = _tradable(scan, "long")[:3]
    sells = _tradable(scan, "short")[:3]
    lines = [
        "I answer from this desk only: today's buys and sells, a symbol's levels and news, which rules passed the backtest, and the opening-range board. I don't forecast a price.",
        _honesty(scan),
    ]
    if buys:
        lines.append("Top buys right now: " + ", ".join(p["symbol"] for p in buys) + ".")
    if sells:
        lines.append("Top sells right now: " + ", ".join(p["symbol"] for p in sells) + ".")
    chips = [_chip(p) for p in buys + sells]
    return _out("\n\n".join(lines), chips)
