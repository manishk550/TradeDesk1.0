"""Rule library + honest backtester.

Rules are fixed a priori (no parameter fitting on this data). Every rule is run on every
instrument of a market, trades are pooled, and a playbook (rule + side) is only
"VALIDATED" when it is profitable in-sample AND out-of-sample after costs, with a day-clustered
t-stat so a single crash day with 50 simultaneous signals cannot fake confidence.
"""
from __future__ import annotations

import math
from typing import Any, Callable

from . import indicators

VERSION = "4"  # bump whenever rules, costs or verdict thresholds change (invalidates saved backtests)
WARMUP = 105
COST = {"IN": 0.0008, "US": 0.0010}  # round-trip, fraction of price
IS_FRACTION = 0.60


def make_ctx(bars: list[dict]) -> dict[str, Any]:
    ind = indicators.compute(bars)
    S = ind["series"]
    S["sma100"] = indicators.sma([b["close"] for b in bars], 100)
    return {"bars": bars, "S": S, "dir": ind["dir"], "n": len(bars)}


def g(ctx: dict, key: str, i: int) -> float | None:
    if i < 0 or i >= ctx["n"]:
        return None
    return ctx["S"][key][i]


# --------------------------------------------------------------------------- rules
RULES: dict[str, dict[str, Any]] = {}


def rule(key: str, name: str, kind: str, desc: str, markets=("IN", "US"), needs_open=False, evidence=""):
    def deco(fn: Callable) -> Callable:
        RULES[key] = {
            "key": key,
            "name": name,
            "kind": kind,  # "day" = flat by close of entry day, "swing" = 2-5 sessions
            "desc": desc,
            "markets": markets,
            "needs_open": needs_open,
            "evidence": evidence,
            "fn": fn,
        }
        return fn

    return deco


def _rvol(ctx: dict, i: int) -> float | None:
    v20 = g(ctx, "vol20", i)
    if not v20:
        return None
    return ctx["bars"][i]["volume"] / v20


@rule(
    "gap_go",
    "Gap & Go (in play)",
    "day",
    "Stock gaps >=0.5 ATR at the open on above-average prior-day volume: trade WITH the gap, exit at the close. "
    "Daily-bar proxy of the 'Stocks in Play' idea.",
    needs_open=True,
    evidence="Zarattini-Barbon-Aziz 2024 (SSRN 4729284): high relative-volume names carry intraday momentum.",
)
def gap_go(ctx, i, live_open=None):
    j = i + 1
    bars = ctx["bars"]
    op = live_open if live_open else (bars[j]["open"] if j < ctx["n"] else None)
    atr, c = g(ctx, "atr", i), bars[i]["close"]
    rv = _rvol(ctx, i)
    if op is None or not atr or rv is None:
        return None
    gap = op - c
    if abs(gap) < 0.5 * atr or rv < 1.2:
        return None
    side = "long" if gap > 0 else "short"
    return {"side": side, "mode": "open", "risk_atr": 0.75, "hold": 1, "cond": f"gap {gap / c * 100:+.2f}% on {rv:.1f}x volume"}


@rule(
    "gap_fade",
    "Gap Fade",
    "day",
    "Opens >=1 ATR away from yesterday's close: fade it back toward the prior close (gap fill), exit by the close.",
    needs_open=True,
)
def gap_fade(ctx, i, live_open=None):
    j = i + 1
    bars = ctx["bars"]
    op = live_open if live_open else (bars[j]["open"] if j < ctx["n"] else None)
    atr, c = g(ctx, "atr", i), bars[i]["close"]
    if op is None or not atr:
        return None
    gap = op - c
    if abs(gap) < 1.0 * atr:
        return None
    side = "short" if gap > 0 else "long"
    return {
        "side": side,
        "mode": "open",
        "risk_atr": 1.0,
        "target_abs": c,
        "hold": 1,
        "cond": f"gap {gap / c * 100:+.2f}% = {abs(gap) / atr:.1f} ATR",
    }


@rule(
    "rsi2_pullback",
    "RSI(2) Trend Pullback",
    "swing",
    "In a 100-day uptrend buy 2-day RSI <=10 (mirror for shorts at >=90). Exit on a close back through the 5-day average, "
    "stop 2.5 ATR, max 5 sessions.",
    evidence="Connors/Alvarez short-term mean reversion; mixed on index futures, tested here on single names.",
)
def rsi2_pullback(ctx, i, live_open=None):
    c, s100, r2 = ctx["bars"][i]["close"], g(ctx, "sma100", i), g(ctx, "rsi2", i)
    if s100 is None or r2 is None:
        return None
    if c > s100 and r2 <= 10:
        side = "long"
    elif c < s100 and r2 >= 90:
        side = "short"
    else:
        return None
    return {"side": side, "mode": "open", "risk_atr": 2.5, "hold": 5, "exit_sma5": True, "cond": f"RSI(2) {r2:.0f} in {'up' if side == 'long' else 'down'}trend"}


@rule(
    "rsi2_deep",
    "RSI(2) Deep Washout",
    "swing",
    "Stricter RSI(2) pullback: 2-day RSI <=5 above the 100-day (>=95 below it for shorts), >=2 down/up days in a row. "
    "Same exits: close back through the 5-day average, stop 2.5 ATR, max 5 sessions.",
    evidence="Connors/Alvarez: the more extreme the washout, the better the bounce - checked here, not assumed.",
)
def rsi2_deep(ctx, i, live_open=None):
    bars = ctx["bars"]
    c, s100, r2 = bars[i]["close"], g(ctx, "sma100", i), g(ctx, "rsi2", i)
    if s100 is None or r2 is None or i < 3:
        return None
    if c > s100 and r2 <= 5 and bars[i]["close"] < bars[i - 1]["close"] < bars[i - 2]["close"]:
        return {"side": "long", "mode": "open", "risk_atr": 2.5, "hold": 5, "exit_sma5": True, "cond": f"RSI(2) {r2:.0f} after 2 straight down closes"}
    if c < s100 and r2 >= 95 and bars[i]["close"] > bars[i - 1]["close"] > bars[i - 2]["close"]:
        return {"side": "short", "mode": "open", "risk_atr": 2.5, "hold": 5, "exit_sma5": True, "cond": f"RSI(2) {r2:.0f} after 2 straight up closes"}
    return None


@rule(
    "ibs_reversal",
    "IBS Close Reversal",
    "day",
    "Internal Bar Strength = where the close sits in the day's range. Close in the bottom 15% in an uptrend: buy next open, "
    "exit at that day's close (mirror for tops in a downtrend). Stop 1.5 ATR.",
    evidence="Documented equity mean-reversion effect; third-party NQ futures tests found no proven edge - tested on single names here.",
)
def ibs_reversal(ctx, i, live_open=None):
    b = ctx["bars"][i]
    rng = b["high"] - b["low"]
    s100, atr = g(ctx, "sma100", i), g(ctx, "atr", i)
    if rng <= 0 or s100 is None or not atr or rng < 0.5 * atr:
        return None
    ibs = (b["close"] - b["low"]) / rng
    if ibs <= 0.15 and b["close"] > s100:
        return {"side": "long", "mode": "open", "risk_atr": 1.5, "hold": 1, "cond": f"closed in the bottom {ibs * 100:.0f}% of the range"}
    if ibs >= 0.85 and b["close"] < s100:
        return {"side": "short", "mode": "open", "risk_atr": 1.5, "hold": 1, "cond": f"closed in the top {ibs * 100:.0f}% of the range"}
    return None


@rule(
    "breakout_vol",
    "20-Day Breakout + Volume",
    "swing",
    "Close beyond the prior 20-day high/low on >=1.5x volume with the 50-day trend: enter next open, stop 1.5 ATR, "
    "target 2R, max 3 sessions.",
    evidence="Turtle/Donchian breakout with a volume filter; no proven edge on index futures in third-party tests.",
)
def breakout_vol(ctx, i, live_open=None):
    c = ctx["bars"][i]["close"]
    hh, ll, s50, rv = g(ctx, "hh20", i), g(ctx, "ll20", i), g(ctx, "sma50", i), _rvol(ctx, i)
    if None in (hh, ll, s50, rv) or rv < 1.5:
        return None
    if c > hh and c > s50:
        return {"side": "long", "mode": "open", "risk_atr": 1.5, "target_r": 2.0, "hold": 3, "cond": f"closed above 20d high on {rv:.1f}x volume"}
    if c < ll and c < s50:
        return {"side": "short", "mode": "open", "risk_atr": 1.5, "target_r": 2.0, "hold": 3, "cond": f"closed below 20d low on {rv:.1f}x volume"}
    return None


@rule(
    "nr7_expand",
    "NR7 Range Expansion",
    "day",
    "Yesterday had the narrowest range of 7 days. Next session buy-stop above its high (uptrend) or sell-stop below its low "
    "(downtrend); stop at the other end, target 1.5R, flat by the close.",
    evidence="Crabel narrow-range expansion; classic day-trade volatility breakout.",
)
def nr7_expand(ctx, i, live_open=None):
    if i < 8:
        return None
    bars = ctx["bars"]
    rng = [b["high"] - b["low"] for b in bars[i - 6 : i + 1]]
    if rng[-1] > min(rng) + 1e-12:
        return None
    atr, c, s50 = g(ctx, "atr", i), bars[i]["close"], g(ctx, "sma50", i)
    if not atr or s50 is None or rng[-1] <= 0 or rng[-1] > 1.2 * atr:
        return None
    hi, lo = bars[i]["high"], bars[i]["low"]
    if c > s50:
        return {"side": "long", "mode": "stop", "level": hi, "stop_abs": lo, "target_r": 1.5, "hold": 1, "cond": "NR7 inside an uptrend"}
    return {"side": "short", "mode": "stop", "level": lo, "stop_abs": hi, "target_r": 1.5, "hold": 1, "cond": "NR7 inside a downtrend"}


@rule(
    "oi_buildup",
    "Open-Interest Build-Up",
    "day",
    "India: price moves >=1% (or 0.6 ATR) with open interest +3% and volume >=1.3x average = fresh positions. "
    "Trade the continuation next session, stop 1.2 ATR, exit at the close.",
    markets=("IN",),
    evidence="Standard NSE long/short build-up read; single-stock futures data from NSE bhavcopy.",
)
def oi_buildup(ctx, i, live_open=None):
    bars = ctx["bars"]
    b = bars[i]
    if i < 1 or "oi" not in b:
        return None
    prev = bars[i - 1]["close"]
    atr, rv = g(ctx, "atr", i), _rvol(ctx, i)
    base = b["oi"] - b["doi"]
    if not atr or rv is None or base <= 0:
        return None
    chg = b["close"] / prev - 1
    oi_chg = b["doi"] / base
    thresh = max(0.01, 0.6 * atr / b["close"])
    if rv < 1.3 or oi_chg < 0.03:
        return None
    if chg >= thresh:
        return {"side": "long", "mode": "open", "risk_atr": 1.2, "hold": 1, "cond": f"long build-up: price {chg * 100:+.1f}%, OI {oi_chg * 100:+.1f}%, vol {rv:.1f}x"}
    if chg <= -thresh:
        return {"side": "short", "mode": "open", "risk_atr": 1.2, "hold": 1, "cond": f"short build-up: price {chg * 100:+.1f}%, OI {oi_chg * 100:+.1f}%, vol {rv:.1f}x"}
    return None


@rule(
    "trend_stack",
    "Indicator Stack (EMA+MACD+Supertrend+RSI)",
    "swing",
    "The retail favourite: price > EMA20 > SMA50 > SMA100, MACD histogram > 0, Supertrend up, RSI 55-75 (mirror for shorts). "
    "Included as a control so you can see whether piling indicators together actually works.",
    evidence="Third-party futures tests: MACD/EMA/Supertrend stacks show 'no proven edge'.",
)
def trend_stack(ctx, i, live_open=None):
    bars = ctx["bars"]
    c = bars[i]["close"]
    e20, s50, s100 = g(ctx, "ema20", i), g(ctx, "sma50", i), g(ctx, "sma100", i)
    mh, r14, d = g(ctx, "macd_hist", i), g(ctx, "rsi14", i), ctx["dir"][i]
    if None in (e20, s50, s100, mh, r14, d):
        return None
    if c > e20 > s50 > s100 and mh > 0 and d == 1 and 55 <= r14 <= 75:
        return {"side": "long", "mode": "open", "risk_atr": 1.5, "target_r": 1.5, "hold": 3, "cond": "all indicators bullish"}
    if c < e20 < s50 < s100 and mh < 0 and d == -1 and 25 <= r14 <= 45:
        return {"side": "short", "mode": "open", "risk_atr": 1.5, "target_r": 1.5, "hold": 3, "cond": "all indicators bearish"}
    return None


# --------------------------------------------------------------------------- simulation
def simulate(ctx: dict, i: int, st: dict, cost: float, entry_override: float | None = None) -> dict | None:
    bars, n = ctx["bars"], ctx["n"]
    j = i + 1
    if j >= n:
        return None
    atr = g(ctx, "atr", i)
    if not atr:
        return None
    sgn = 1 if st["side"] == "long" else -1
    b = bars[j]
    if st["mode"] == "open":
        entry = entry_override or b["open"]
    else:
        lvl = st["level"]
        if sgn > 0:
            if b["high"] < lvl:
                return None
            entry = max(lvl, b["open"])
        else:
            if b["low"] > lvl:
                return None
            entry = min(lvl, b["open"])
    if "stop_abs" in st:
        risk = abs(entry - st["stop_abs"])
        if risk < 0.1 * atr:
            return None
        stop = st["stop_abs"]
    else:
        risk = st["risk_atr"] * atr
        stop = entry - sgn * risk
    target = None
    if "target_abs" in st:
        target = st["target_abs"]
        if (target - entry) * sgn <= 0:
            return None
    elif "target_r" in st:
        target = entry + sgn * st["target_r"] * risk
    last = min(n - 1, j + st["hold"] - 1)
    exit_px, why, exit_k = None, "time", last
    for k in range(j, last + 1):
        bk = bars[k]
        if k > j:  # gap through the stop on a later day exits at the open
            if (sgn > 0 and bk["open"] <= stop) or (sgn < 0 and bk["open"] >= stop):
                exit_px, why, exit_k = bk["open"], "stop", k
                break
        hit_stop = bk["low"] <= stop if sgn > 0 else bk["high"] >= stop
        hit_tgt = target is not None and (bk["high"] >= target if sgn > 0 else bk["low"] <= target)
        if hit_stop:  # pessimistic: stop wins when both are inside one bar
            exit_px, why, exit_k = stop, "stop", k
            break
        if hit_tgt:
            exit_px, why, exit_k = target, "target", k
            break
        if st.get("exit_sma5") and k > j:
            s5 = g(ctx, "sma5", k)
            if s5 is not None and ((sgn > 0 and bk["close"] >= s5) or (sgn < 0 and bk["close"] <= s5)):
                exit_px, why, exit_k = bk["close"], "target", k
                break
        if k == last:
            exit_px, why, exit_k = bk["close"], "time", k
    move = (exit_px - entry) * sgn
    r = (move - cost * entry) / risk
    return {
        "side": st["side"],
        "i": i,
        "j": j,
        "k": exit_k,
        "date": bars[j].get("date"),
        "entry": entry,
        "exit": exit_px,
        "stop": stop,
        "r": max(r, -3.0),
        "ret": move / entry - cost,
        "why": why,
        "bars": exit_k - j + 1,
    }


def run_rule(ctx: dict, key: str, market: str) -> list[dict]:
    fn = RULES[key]["fn"]
    cost = COST[market]
    trades: list[dict] = []
    i = WARMUP
    n = ctx["n"]
    while i < n - 1:
        st = fn(ctx, i)
        if not st:
            i += 1
            continue
        t = simulate(ctx, i, st, cost)
        if not t:
            i += 1
            continue
        trades.append(t)
        i = t["k"]  # next signal evaluated after the trade is closed
    return trades


# --------------------------------------------------------------------------- statistics
def stats(rs: list[float], dates: list[str] | None = None) -> dict[str, Any]:
    n = len(rs)
    if n == 0:
        return {"n": 0, "win": 0.0, "exp": 0.0, "pf": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "t": 0.0, "days": 0}
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gw, gl = sum(wins), -sum(losses)
    pf = (gw / gl) if gl > 0 else (9.99 if gw > 0 else 0.0)
    exp = sum(rs) / n
    # day-clustered t-stat
    days = 0
    t = 0.0
    if dates:
        by: dict[str, list[float]] = {}
        for r, d in zip(rs, dates):
            by.setdefault(d, []).append(r)
        dm = [sum(v) / len(v) for v in by.values()]
        days = len(dm)
        if days > 2:
            m = sum(dm) / days
            var = sum((x - m) ** 2 for x in dm) / (days - 1)
            sd = math.sqrt(var)
            t = m / (sd / math.sqrt(days)) if sd > 0 else 0.0
    return {
        "n": n,
        "win": round(100 * len(wins) / n, 1),
        "exp": round(exp, 3),
        "pf": round(min(pf, 9.99), 2),
        "avg_win": round(gw / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "t": round(t, 2),
        "days": days,
    }


def verdict(is_s: dict, oos: dict) -> tuple[str, str]:
    if oos["n"] < 30 or oos["days"] < 20:
        return "TOO_FEW", f"only {oos['n']} out-of-sample trades"
    if is_s["exp"] <= 0 or oos["exp"] <= 0:
        return "REJECTED", "loses money after costs in-sample or out-of-sample"
    if oos["exp"] >= 0.05 and oos["pf"] >= 1.2 and oos["t"] >= 1.5:
        return "VALIDATED", "profitable in both halves; out-of-sample PF>=1.2 and day-clustered t>=1.5"
    if oos["exp"] >= 0.03 and oos["pf"] >= 1.1 and oos["t"] >= 0.5:
        return "MARGINAL", "positive in both halves but the edge is thin / statistically weak"
    return "REJECTED", "no meaningful edge: expectancy is about zero or not distinguishable from luck"


def evaluate_market(contexts: dict[str, dict], market: str) -> dict[str, Any]:
    """Run all rules over a market, return playbook stats keyed 'rule:side'."""
    out: dict[str, Any] = {}
    all_dates = sorted({b["date"] for c in contexts.values() for b in c["bars"][WARMUP:]})
    if not all_dates:
        return {"playbooks": {}, "split": None, "universe": 0}
    split = all_dates[int(len(all_dates) * IS_FRACTION)]
    for key, meta in RULES.items():
        if market not in meta["markets"]:
            continue
        per_side: dict[str, list[tuple[dict, str]]] = {"long": [], "short": []}
        for sym, ctx in contexts.items():
            for t in run_rule(ctx, key, market):
                per_side[t["side"]].append((t, sym))
        for side, items in per_side.items():
            items.sort(key=lambda x: x[0]["date"])
            ins = [t for t, _ in items if t["date"] < split]
            oos = [t for t, _ in items if t["date"] >= split]
            s_all = stats([t["r"] for t, _ in items], [t["date"] for t, _ in items])
            s_is = stats([t["r"] for t in ins], [t["date"] for t in ins])
            s_oos = stats([t["r"] for t in oos], [t["date"] for t in oos])
            v, why = verdict(s_is, s_oos)
            # equity curve in R (mean R per day, summed) for sparkline
            by: dict[str, list[float]] = {}
            for t, _ in items:
                by.setdefault(t["date"], []).append(t["r"])
            cum, curve = 0.0, []
            for d in sorted(by):
                cum += sum(by[d]) / len(by[d])
                curve.append([d, round(cum, 2)])
            per_sym: dict[str, list[float]] = {}
            for t, sym in items:
                per_sym.setdefault(sym, []).append(t["r"])
            sym_stats = {
                s: {"n": len(v), "exp": round(sum(v) / len(v), 2), "win": round(100 * sum(1 for x in v if x > 0) / len(v))}
                for s, v in per_sym.items()
                if len(v) >= 5
            }
            out[f"{key}:{side}"] = {
                "rule": key,
                "side": side,
                "name": meta["name"],
                "kind": meta["kind"],
                "all": s_all,
                "is": s_is,
                "oos": s_oos,
                "verdict": v,
                "why": why,
                "curve": curve[-250:],
                "by_symbol": sym_stats,
                "avg_bars": round(sum(t["bars"] for t, _ in items) / len(items), 1) if items else 0,
            }
    return {"playbooks": out, "split": split, "universe": len(contexts), "dates": [all_dates[0], all_dates[-1]]}
