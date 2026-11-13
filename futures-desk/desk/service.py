from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import archive, india, indicators, news, strategies, tv, us
from .common import DATA, cache, http_json

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
MARKETS = {
    "IN": {"label": "India · NSE futures", "ccy": "₹", "tz": IST, "open": dtime(9, 15), "close": dtime(15, 30)},
    "US": {"label": "US · CoinDCX stock futures", "ccy": "$", "tz": NY, "open": dtime(9, 30), "close": dtime(16, 0)},
}

_STATE: dict[str, Any] = {"ctx": {}, "eval": {}, "evaluating": set(), "boot": False}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- market clock
def market_status(market: str) -> dict[str, Any]:
    m = MARKETS[market]
    now = datetime.now(m["tz"])
    wd = now.weekday()
    t = now.time()
    if wd < 5 and m["open"] <= t < m["close"]:
        state = "open"
    elif wd < 5 and t < m["open"]:
        state = "pre"
    else:
        state = "closed"
    d = now.date()
    if state == "closed" or (wd >= 5):
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    nxt = datetime.combine(d, m["open"], tzinfo=m["tz"]) if state != "open" else None
    return {
        "state": state,
        "now": now.strftime("%a %H:%M"),
        "next_open_ist": nxt.astimezone(IST).strftime("%a %d %b %H:%M IST") if nxt else None,
        "session_date": now.date().isoformat(),
    }


# --------------------------------------------------------------------------- data access
def instruments(market: str) -> list[dict[str, Any]]:
    if market == "IN":
        return india.universe()
    return us.universe()


def bars_for(market: str, inst: dict[str, Any]) -> list[dict[str, Any]]:
    if market == "IN":
        s = india.build_series().get(inst["id"])
        bars = list(s["bars"]) if s else []
    else:
        try:
            bars = us.daily(inst, max_age_h=10_000)
        except Exception:  # noqa: BLE001
            bars = []
    # while the cash session is in progress a same-day row (if any) is partial: ignore it
    st = market_status(market)
    if bars and st["state"] == "open" and bars[-1].get("date") == st["session_date"]:
        bars = bars[:-1]
    return bars


def contexts(market: str) -> dict[str, dict[str, Any]]:
    insts = {i["id"]: i for i in instruments(market)}
    data_key = None
    out: dict[str, dict[str, Any]] = {}
    for sid, inst in insts.items():
        bars = bars_for(market, inst)
        if len(bars) < strategies.WARMUP + 20:
            continue
        ck = f"ctx:{market}:{sid}:{bars[-1]['date']}:{len(bars)}"
        c = cache.get(ck)
        if c is None:
            c = cache.set(ck, strategies.make_ctx(bars), 6 * 3600)
        c["inst"] = inst
        out[sid] = c
    return out


def data_key(market: str, ctxs: dict[str, dict[str, Any]]) -> str:
    last = max((c["bars"][-1]["date"] for c in ctxs.values()), default="none")
    return f"{market}:{last}:{len(ctxs)}:v{strategies.VERSION}"


# --------------------------------------------------------------------------- backtest cache
def _eval_path(market: str):
    return DATA / f"eval_{market}.json"


def ensure_eval(market: str, force: bool = False) -> dict[str, Any] | None:
    ctxs = contexts(market)
    if not ctxs:
        return None
    key = data_key(market, ctxs)
    cur = _STATE["eval"].get(market)
    if cur and cur["key"] == key and not force:
        return cur
    p = _eval_path(market)
    if p.exists() and not force:
        try:
            saved = json.loads(p.read_text())
            if saved.get("key") == key:
                _STATE["eval"][market] = saved
                return saved
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        if market in _STATE["evaluating"]:
            return cur
        _STATE["evaluating"].add(market)
    try:
        t0 = time.time()
        res = strategies.evaluate_market(ctxs, market)
        res["key"] = key
        res["built_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        res["seconds"] = round(time.time() - t0, 1)
        res["cost"] = strategies.COST[market]
        _STATE["eval"][market] = res
        p.write_text(json.dumps(res))
        return res
    finally:
        with _LOCK:
            _STATE["evaluating"].discard(market)


def evaluating(market: str) -> bool:
    return market in _STATE["evaluating"]


def boot() -> None:
    """Start background sync + backtest workers once."""
    if _STATE["boot"]:
        return
    _STATE["boot"] = True

    def loop() -> None:
        while True:
            try:
                india.sync()
                us.sync_all(max_age_h=3.0)
                for m in ("IN", "US"):
                    ensure_eval(m)
                    print("archive", m, archive.record_if_due(m, __import__("sys").modules[__name__]))
            except Exception as exc:  # noqa: BLE001
                print("worker error:", exc)
            time.sleep(20 * 60)

    # make whatever is on disk usable immediately, then keep it fresh
    def quick() -> None:
        for m in ("IN", "US"):
            try:
                ensure_eval(m)
            except Exception as exc:  # noqa: BLE001
                print("eval error:", m, exc)

    threading.Thread(target=quick, daemon=True, name="quick-eval").start()
    threading.Thread(target=loop, daemon=True, name="refresh").start()


# --------------------------------------------------------------------------- picks
def _verdict_rank(v: str) -> int:
    return {"VALIDATED": 3, "MARGINAL": 2, "TOO_FEW": 1, "REJECTED": 0}.get(v, 0)


def _align(side: str, x: float | None, pos: float = 0.1, neg: float = -0.1) -> int:
    if x is None:
        return 0
    s = 1 if side == "long" else -1
    if x * s >= pos:
        return 1
    if x * s <= neg:
        return -1
    return 0


def _live_open(market: str, sid: str, ctx: dict, snap_row: dict) -> float | None:
    st = market_status(market)
    if st["state"] != "open" or not snap_row:
        return None
    last = ctx["bars"][-1]
    if last["date"] >= st["session_date"]:
        return None
    op = snap_row.get("open")
    if not op:
        return None
    if market == "IN":
        s = india.build_series().get(sid)
        if s and s["underlying"]:
            return op * s["last_close"] / s["underlying"]
    return op


def build_pick(market: str, ctx: dict, rule_key: str, st: dict, entry_ref: float | None, snap_row: dict, risk_budget: float, ev: dict) -> dict[str, Any]:
    inst = ctx["inst"]
    bars = ctx["bars"]
    i = ctx["n"] - 1
    atr = strategies.g(ctx, "atr", i) or 0.0
    side = st["side"]
    sgn = 1 if side == "long" else -1
    meta = strategies.RULES[rule_key]
    last_close = bars[i]["close"]
    entry = st.get("level") if st["mode"] == "stop" else (entry_ref or last_close)
    if "stop_abs" in st:
        stop = st["stop_abs"]
        risk = abs(entry - stop)
    else:
        risk = st["risk_atr"] * atr
        stop = entry - sgn * risk
    target = None
    if "target_abs" in st:
        target = st["target_abs"]
    elif "target_r" in st:
        target = entry + sgn * st["target_r"] * risk
    t1 = target if target is not None else entry + sgn * 1.0 * risk
    pb = ev["playbooks"].get(f"{rule_key}:{side}") if ev else None
    verdict = pb["verdict"] if pb else "TOO_FEW"
    lot = None
    if market == "IN":
        s = india.build_series().get(inst["id"])
        lot = s["lot"] if s else None
    sizing = None
    if risk > 0:
        qty = risk_budget / risk
        if lot:
            lots = int(qty // lot)
            sizing = {"lots": lots, "qty": lots * lot, "lot": lot, "risk_per_lot": round(risk * lot, 0), "lot_value": round(entry * lot, 0), "fits": lots >= 1}
        else:
            sizing = {"qty": round(qty, 2), "notional": round(qty * entry, 2), "fits": True}
    steps = plan_steps(meta, st, entry, stop, target, atr, market)
    return {
        "symbol": inst["id"],
        "name": inst.get("name") or inst["id"],
        "rule": rule_key,
        "rule_name": meta["name"],
        "kind": meta["kind"],
        "side": side,
        "action": "BUY" if side == "long" else "SELL",
        "mode": st["mode"],
        "cond": st.get("cond"),
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2) if target is not None else None,
        "t1": round(t1, 2),
        "risk": round(risk, 2),
        "risk_pct": round(100 * risk / entry, 2) if entry else None,
        "atr": round(atr, 2),
        "hold": st["hold"],
        "verdict": verdict,
        "playbook": {k: pb[k] for k in ("is", "oos", "all", "why", "avg_bars")} if pb else None,
        "sizing": sizing,
        "steps": steps,
        "last_close": round(last_close, 2),
    }


def plan_steps(meta: dict, st: dict, entry: float, stop: float, target: float | None, atr: float, market: str) -> list[str]:
    side = st["side"]
    verb = "Buy" if side == "long" else "Sell short"
    close = "sell" if side == "long" else "cover"
    out: list[str] = []
    if st["mode"] == "stop":
        out.append(f"Place a {'buy-stop' if side == 'long' else 'sell-stop'} at {entry:.2f}. Cancel it if it has not triggered by mid-afternoon.")
    elif meta["kind"] == "day" and st.get("hold") == 1:
        out.append(f"{verb} near the open around {entry:.2f}. Skip it if the open is already beyond the stop or has run more than 0.4 ATR away.")
    else:
        out.append(f"{verb} at the next open around {entry:.2f}.")
    out.append(f"Hard stop {stop:.2f} ({abs(entry - stop):.2f} away). Place it with the order, never widen it.")
    if target is not None:
        out.append(f"Take profit at {target:.2f}.")
    if st.get("exit_sma5"):
        out.append(f"Or {close} on the first close back through the 5-day average.")
    if st["hold"] == 1:
        out.append("Flat by the close. Do not carry it overnight.")
    else:
        out.append(f"Time exit: close it after {st['hold']} sessions if neither level is hit.")
    return out


def grade(pick: dict, news_sum: dict, rating: float | None, fund: dict, earnings_days: int | None) -> tuple[str, list[str], bool]:
    """Quality grade built from the validated playbook first, confirmations second. Returns (grade, notes, veto)."""
    side = pick["side"]
    notes: list[str] = []
    pts = {"VALIDATED": 3, "MARGINAL": 1}.get(pick["verdict"], 0)
    veto = False
    if pick["verdict"] == "VALIDATED":
        notes.append("Playbook passed the out-of-sample test")
    elif pick["verdict"] == "MARGINAL":
        notes.append("Playbook edge is thin - trade small")
    else:
        notes.append("Playbook is NOT validated on this universe - idea only")
    label = news_sum.get("label")
    if label == ("bearish" if side == "long" else "bullish"):
        veto = True
        notes.append(f"News tape is {label} - against this trade (skip)")
    elif label == ("bullish" if side == "long" else "bearish"):
        pts += 1
        notes.append(f"News tape is {label} - supports it")
    a = _align(side, rating)
    if a > 0:
        pts += 1
        notes.append(f"Technical rating agrees ({tv.rating_label(rating)})")
    elif a < 0:
        pts -= 1
        notes.append(f"Technical rating disagrees ({tv.rating_label(rating)})")
    q = fund.get("quality") if fund.get("available") else None
    if q == "strong" and side == "long":
        pts += 1
        notes.append("Fundamentals strong")
    elif q == "weak" and side == "long":
        pts -= 1
        notes.append("Fundamentals weak")
    elif q == "weak" and side == "short":
        pts += 1
        notes.append("Weak fundamentals support the short")
    if earnings_days is not None and 0 <= earnings_days <= 2:
        veto = True
        notes.append(f"Earnings in {earnings_days} day(s) - skip (gap risk)")
    if pick["verdict"] not in ("VALIDATED", "MARGINAL"):
        g = "C"
    else:
        g = "A" if pts >= 5 else ("B" if pts >= 3 else "C")
    if veto:
        g = "SKIP"
    return g, notes, veto


def mark_late(p: dict, mstat: dict) -> None:
    """A setup built from the last close is meant for the next open; flag it if the session is already running."""
    meta = strategies.RULES[p["rule"]]
    p["late"] = False
    if mstat["state"] == "open" and p["mode"] == "open" and not meta["needs_open"]:
        p["late"] = True
        p["steps"].insert(0, f"Session is already live: this setup was built for today's open. Only take it if price is still within 0.4 ATR ({p['atr'] * 0.4:.2f}) of {p['entry']:.2f}; otherwise wait for the next session.")


def days_to(ts: Any) -> int | None:
    if not ts:
        return None
    try:
        d = datetime.utcfromtimestamp(int(ts)).date()
    except (ValueError, OSError, TypeError):
        return None
    return (d - date.today()).days


def scan(market: str, risk_budget: float | None = None, with_news: bool = True) -> dict[str, Any]:
    risk_budget = risk_budget or (10_000 if market == "IN" else 100)
    ck = f"scan:{market}:{risk_budget}:{with_news}"
    hit = cache.get(ck)
    if hit is not None:
        return hit
    res = _scan(market, risk_budget, with_news)
    return cache.set(ck, res, 45 if res.get("ready") else 3)


def _scan(market: str, risk_budget: float, with_news: bool) -> dict[str, Any]:
    t0 = time.time()
    ctxs = contexts(market)
    status = {
        "india_sync": dict(india.STATUS) if market == "IN" else None,
        "us_sync": {k: us.STATUS[k] for k in ("running", "done", "total")} if market == "US" else None,
        "evaluating": evaluating(market),
    }
    mstat = market_status(market)
    if not ctxs:
        return {"market": market, "ready": False, "status": status, "market_status": mstat, "picks": [], "table": []}
    ev = ensure_eval(market)
    snap = tv.snapshot(market, list(ctxs.keys()))
    picks: list[dict[str, Any]] = []
    cond: list[dict[str, Any]] = []
    table: list[dict[str, Any]] = []
    pending: list[tuple] = []
    for sid, ctx in ctxs.items():
        i = ctx["n"] - 1
        bars = ctx["bars"]
        row = tv.lookup(snap, sid)
        S = ctx["S"]
        c = bars[i]["close"]
        chg = (c / bars[i - 1]["close"] - 1) * 100 if i else 0
        rv = strategies._rvol(ctx, i)
        s100 = S["sma100"][i]
        trend = "up" if (s100 and c > s100) else ("down" if s100 else "n/a")
        entry_row = {
            "symbol": sid,
            "name": ctx["inst"].get("name") or sid,
            "kind": ctx["inst"].get("kind"),
            "close": round(c, 2),
            "chg": round(chg, 2),
            "rvol": round(rv, 2) if rv else None,
            "rsi2": round(S["rsi2"][i], 0) if S["rsi2"][i] is not None else None,
            "rsi14": round(S["rsi14"][i], 0) if S["rsi14"][i] is not None else None,
            "trend": trend,
            "atr_pct": round(100 * S["atr"][i] / c, 2) if S["atr"][i] else None,
            "rating": row.get("Recommend.All"),
            "rating_label": tv.rating_label(row.get("Recommend.All")),
            "sector": row.get("sector"),
            "live_change": row.get("change"),
            "date": bars[i]["date"],
        }
        if market == "IN":
            b = bars[i]
            base = b["oi"] - b["doi"]
            entry_row["oi_chg"] = round(100 * b["doi"] / base, 1) if base > 0 else None
            entry_row["oi"] = b["oi"]
        table.append(entry_row)
        live_open = _live_open(market, sid, ctx, row)
        found: list[tuple[str, dict]] = []
        for key, meta in strategies.RULES.items():
            if market not in meta["markets"]:
                continue
            st = meta["fn"](ctx, i, live_open) if meta["needs_open"] else meta["fn"](ctx, i)
            if st and not (meta["needs_open"] and live_open is None):
                found.append((key, st))
            elif meta["needs_open"] and live_open is None and key == "gap_go":
                rvv = strategies._rvol(ctx, i)
                atr = S["atr"][i]
                if rvv and rvv >= 1.2 and atr:
                    pbl = ev["playbooks"].get(f"gap_go:long") if ev else None
                    pbs = ev["playbooks"].get(f"gap_go:short") if ev else None
                    cond.append(
                        {
                            "symbol": sid,
                            "name": entry_row["name"],
                            "buy_above": round(c + 0.5 * atr, 2),
                            "sell_below": round(c - 0.5 * atr, 2),
                            "rvol": round(rvv, 2),
                            "verdict_long": pbl["verdict"] if pbl else None,
                            "verdict_short": pbs["verdict"] if pbs else None,
                            "atr": round(atr, 2),
                        }
                    )
        if found:
            pending.append((sid, ctx, row, found, live_open, entry_row))
    # headlines only for names that actually have a setup, fetched in parallel
    inst_news: dict[str, list] = {}

    def _worth_news(found: list) -> bool:
        return any((ev or {}).get("playbooks", {}).get(f"{k}:{s['side']}", {}).get("verdict") in ("VALIDATED", "MARGINAL") for k, s in found)

    news_names = [x for x in pending if _worth_news(x[3])]
    if len(news_names) < 12:  # nothing tradable: still read the news for the best few ideas
        extra = [x for x in pending if x not in news_names]
        extra.sort(key=lambda x: -max(((ev or {}).get("playbooks", {}).get(f"{k}:{s['side']}", {}).get("oos", {}).get("exp") or -9) for k, s in x[3]))
        news_names += extra[: 12 - len(news_names)]
    if with_news and news_names:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {sid: pool.submit(news.fetch, news.query_for(ctx["inst"], market), market) for sid, ctx, *_ in news_names}
            for sid, f in futs.items():
                try:
                    inst_news[sid] = f.result(timeout=45)
                except Exception:  # noqa: BLE001
                    inst_news[sid] = []
    for sid, ctx, row, found, live_open, entry_row in pending:
        inst = ctx["inst"]
        items = inst_news.get(sid, [])
        nsum = news.summarize(items)
        fund = tv.fundamentals(row)
        edays = days_to(row.get("earnings_release_next_date")) if inst.get("kind") != "index" else None
        for key, st in found:
            p = build_pick(market, ctx, key, st, live_open if strategies.RULES[key]["needs_open"] else None, row, risk_budget, ev or {"playbooks": {}})
            g, notes, veto = grade(p, nsum, row.get("Recommend.All"), fund, edays)
            p.update(
                {
                    "grade": g,
                    "notes": notes,
                    "news": nsum,
                    "rating": row.get("Recommend.All"),
                    "rating_label": tv.rating_label(row.get("Recommend.All")),
                    "fund_quality": fund.get("quality"),
                    "sector": row.get("sector"),
                    "rvol": entry_row["rvol"],
                    "chg": entry_row["chg"],
                    "headline": items[0]["title"] if items else None,
                }
            )
            mark_late(p, mstat)
            oos = (p["playbook"] or {}).get("oos") or {}
            p["score"] = round(_verdict_rank(p["verdict"]) * 10 + (oos.get("exp") or 0) * 10 + {"A": 3, "B": 2, "C": 1, "SKIP": -5}[g], 2)
            picks.append(p)
    hidden = 0
    kept = []
    for p in picks:
        oos_exp = ((p.get("playbook") or {}).get("oos") or {}).get("exp")
        if p["verdict"] not in ("VALIDATED", "MARGINAL") and (oos_exp is None or oos_exp <= 0):
            hidden += 1  # a rule that lost money out-of-sample is not an "idea", it is noise
            continue
        kept.append(p)
    picks = kept
    picks.sort(key=lambda p: -p["score"])
    # one headline pick per symbol; the rest are "also fires"
    seen: dict[str, dict] = {}
    for p in picks:
        if p["symbol"] not in seen:
            seen[p["symbol"]] = p
            p["also"] = []
        else:
            seen[p["symbol"]]["also"].append(f"{p['action']} · {p['rule_name']}")
    headline = [p for p in picks if p["symbol"] in seen and seen[p["symbol"]] is p]
    cond.sort(key=lambda x: -x["rvol"])
    table.sort(key=lambda r: -(abs(r["chg"]) * (r["rvol"] or 1)))
    pbs = []
    if ev:
        for k, v in ev["playbooks"].items():
            pbs.append({"key": k, **{x: v[x] for x in ("rule", "side", "name", "kind", "all", "is", "oos", "verdict", "why", "curve", "avg_bars")}})
        pbs.sort(key=lambda r: (-_verdict_rank(r["verdict"]), -r["oos"]["exp"]))
    return {
        "market": market,
        "label": MARKETS[market]["label"],
        "ccy": MARKETS[market]["ccy"],
        "ready": True,
        "market_status": mstat,
        "status": status,
        "as_of": max(r["date"] for r in table),
        "universe": len(ctxs),
        "risk_budget": risk_budget,
        "picks": headline,
        "hidden_setups": hidden,
        "conditional": cond[:40],
        "table": table,
        "playbooks": pbs,
        "orb_forward": archive.orb_forward(market, ctxs),
        "split": ev.get("split") if ev else None,
        "history": ev.get("dates") if ev else None,
        "cost": strategies.COST[market],
        "seconds": round(time.time() - t0, 1),
    }


# --------------------------------------------------------------------------- detail
def pivots(b: dict) -> dict[str, float]:
    p = (b["high"] + b["low"] + b["close"]) / 3
    return {
        "R2": p + (b["high"] - b["low"]),
        "R1": 2 * p - b["low"],
        "P": p,
        "S1": 2 * p - b["high"],
        "S2": p - (b["high"] - b["low"]),
        "prevH": b["high"],
        "prevL": b["low"],
    }


def detail(market: str, symbol: str, risk_budget: float | None = None) -> dict[str, Any]:
    risk_budget = risk_budget or (10_000 if market == "IN" else 100)
    insts = {i["id"]: i for i in instruments(market)}
    inst = insts.get(symbol.upper()) or insts.get(symbol)
    if not inst:
        raise KeyError(symbol)
    bars = bars_for(market, inst)
    if len(bars) < 60:
        raise RuntimeError("not enough history yet")
    ctx = strategies.make_ctx(bars)
    ctx["inst"] = inst
    S = ctx["S"]
    n = ctx["n"]
    i = n - 1
    ev = ensure_eval(market) or {"playbooks": {}}
    snap = tv.snapshot(market, [inst["id"]])
    row = tv.lookup(snap, inst["id"])
    items = news.fetch(news.query_for(inst, market), market, 10)
    nsum = news.summarize(items)
    fund = tv.fundamentals(row)
    live_open = _live_open(market, inst["id"], ctx, row)
    picks = []
    edays = days_to(row.get("earnings_release_next_date")) if inst.get("kind") != "index" else None
    for key, meta in strategies.RULES.items():
        if market not in meta["markets"]:
            continue
        st = meta["fn"](ctx, i, live_open) if meta["needs_open"] else meta["fn"](ctx, i)
        if st and not (meta["needs_open"] and live_open is None):
            p = build_pick(market, ctx, key, st, live_open if meta["needs_open"] else None, row, risk_budget, ev)
            g, notes, veto = grade(p, nsum, row.get("Recommend.All"), fund, edays)
            p.update({"grade": g, "notes": notes})
            mark_late(p, market_status(market))
            picks.append(p)
    picks.sort(key=lambda p: -(_verdict_rank(p["verdict"]) * 10))
    # historical markers (validated playbooks only get highlighted in the UI)
    markers = []
    for key, meta in strategies.RULES.items():
        if market not in meta["markets"]:
            continue
        for t in strategies.run_rule(ctx, key, market)[-40:]:
            pb = ev["playbooks"].get(f"{key}:{t['side']}")
            markers.append(
                {
                    "rule": key,
                    "side": t["side"],
                    "j": t["j"],
                    "k": t["k"],
                    "entry": round(t["entry"], 2),
                    "exit": round(t["exit"], 2),
                    "r": round(t["r"], 2),
                    "why": t["why"],
                    "verdict": pb["verdict"] if pb else "TOO_FEW",
                }
            )
    lo = max(0, n - 260)
    def cut(key: str) -> list:
        return [None if v is None else round(v, 2) for v in S[key][lo:]]

    series = {k: cut(k) for k in ("ema20", "sma50", "sma100", "bb_up", "bb_lo", "supertrend")}
    series["rsi14"] = cut("rsi14")
    series["rsi2"] = cut("rsi2")
    series["macd_hist"] = [None if v is None else round(v, 3) for v in S["macd_hist"][lo:]]
    chart_bars = []
    for b in bars[lo:]:
        cb = {k: round(b[k], 2) for k in ("open", "high", "low", "close")}
        cb.update({"time": b["time"], "date": b["date"], "volume": b["volume"]})
        if "oi" in b:
            cb["oi"] = b["oi"]
            cb["doi"] = b["doi"]
        chart_bars.append(cb)
    markers = [{**m, "j": m["j"] - lo, "k": m["k"] - lo} for m in markers if m["j"] >= lo]
    # per-symbol playbook evidence
    per_sym = {}
    for k, v in ev["playbooks"].items():
        s = (v.get("by_symbol") or {}).get(inst["id"])
        if s:
            per_sym[k] = s
    last = bars[i]
    atr = S["atr"][i]
    tech = {
        "close": round(last["close"], 2),
        "ema20": round(S["ema20"][i], 2) if S["ema20"][i] else None,
        "sma50": round(S["sma50"][i], 2) if S["sma50"][i] else None,
        "sma100": round(S["sma100"][i], 2) if S["sma100"][i] else None,
        "rsi2": round(S["rsi2"][i], 1) if S["rsi2"][i] is not None else None,
        "rsi14": round(S["rsi14"][i], 1) if S["rsi14"][i] is not None else None,
        "macd_hist": round(S["macd_hist"][i], 3) if S["macd_hist"][i] is not None else None,
        "atr": round(atr, 2) if atr else None,
        "atr_pct": round(100 * atr / last["close"], 2) if atr else None,
        "supertrend": "up" if ctx["dir"][i] == 1 else "down",
        "bb_pos": round(100 * (last["close"] - S["bb_lo"][i]) / (S["bb_up"][i] - S["bb_lo"][i])) if S["bb_up"][i] else None,
        "rvol": round(strategies._rvol(ctx, i) or 0, 2),
        "adx": round(row["ADX"], 1) if row.get("ADX") else None,
        "rating": row.get("Recommend.All"),
        "rating_label": tv.rating_label(row.get("Recommend.All")),
        "rating_ma": tv.rating_label(row.get("Recommend.MA")),
        "rating_osc": tv.rating_label(row.get("Recommend.Other")),
        "hh20": round(S["hh20"][i], 2) if S["hh20"][i] else None,
        "ll20": round(S["ll20"][i], 2) if S["ll20"][i] else None,
    }
    extra: dict[str, Any] = {}
    if market == "IN":
        s = india.build_series().get(inst["id"]) or {}
        base = last["oi"] - last["doi"]
        pchg = (last["close"] / bars[i - 1]["close"] - 1) * 100
        oichg = 100 * last["doi"] / base if base > 0 else 0
        if pchg > 0 and oichg > 0:
            read = "Long build-up (fresh buying)"
        elif pchg < 0 and oichg > 0:
            read = "Short build-up (fresh selling)"
        elif pchg > 0 and oichg < 0:
            read = "Short covering (weak rally)"
        elif pchg < 0 and oichg < 0:
            read = "Long unwinding (weak decline)"
        else:
            read = "Flat"
        extra = {
            "lot": s.get("lot"),
            "expiry": s.get("expiry"),
            "oi": last["oi"],
            "oi_chg_pct": round(oichg, 1),
            "oi_read": read,
            "lot_value": round(last["close"] * (s.get("lot") or 0)),
        }
    tvsym = row.get("tv_symbol") or ""
    return {
        "market": market,
        "symbol": inst["id"],
        "name": inst.get("name") or inst["id"],
        "ccy": MARKETS[market]["ccy"],
        "kind": inst.get("kind"),
        "as_of": last["date"],
        "bars": chart_bars,
        "series": series,
        "markers": markers,
        "picks": picks,
        "tech": tech,
        "pivots": {k: round(v, 2) for k, v in pivots(last).items()},
        "fundamentals": fund,
        "news": items,
        "news_summary": nsum,
        "snapshot": {
            "price": row.get("close"),
            "change": row.get("change"),
            "gap": row.get("gap"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "volume": row.get("volume"),
            "rvol_live": row.get("relative_volume_10d_calc"),
        },
        "per_symbol": per_sym,
        "extra": extra,
        "tv_symbol": tvsym,
        "market_status": market_status(market),
    }


# --------------------------------------------------------------------------- intraday + ORB
def _bucket(ticks: list[tuple[int, float]], tz: ZoneInfo, open_t: dtime, close_t: dtime) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = {}
    for sec, price in ticks:
        dt = datetime.fromtimestamp(sec, tz)
        if not (open_t <= dt.time() < close_t):
            continue
        b = sec - sec % 300
        cur = buckets.get(b)
        if cur is None:
            buckets[b] = {"time": b, "open": price, "high": price, "low": price, "close": price}
        else:
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
    return [buckets[k] for k in sorted(buckets)]


NSE_H = {"Referer": "https://www.nseindia.com/get-quotes/equity"}
NSE_INDEX = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FINANCIAL SERVICES", "MIDCPNIFTY": "NIFTY MIDCAP SELECT"}


def intraday_raw(market: str, symbol: str) -> list[dict[str, Any]]:
    """5-minute bars of the latest session (cash/underlying price). Raises on feed errors."""
    insts = {i["id"]: i for i in instruments(market)}
    inst = insts.get(symbol)
    if not inst:
        raise KeyError(symbol)
    m = MARKETS[market]
    if market == "US":
        raw = us.intraday(inst.get("nasdaq") or symbol, inst.get("asset") or "stocks")
        return [b for b in raw if m["open"] <= datetime.fromtimestamp(b["time"], NY).time() < m["close"]]
    if symbol in NSE_INDEX:
        idx, typ = NSE_INDEX[symbol].replace(" ", "%20"), "index"
    else:
        idx, typ = f"{symbol.replace('&', '%26')}EQN", "symbol"
    data = http_json(f"https://www.nseindia.com/api/chart-databyindex-dynamic?index={idx}&type={typ}", NSE_H)
    ticks: list[tuple[int, float]] = []
    for t in data.get("grapthData") or []:
        if len(t) < 3 or t[2] != "NM":
            continue
        # NSE sends IST wall-clock time labelled as UTC; convert to a real epoch
        wall = datetime.utcfromtimestamp(int(t[0]) / 1000)
        ticks.append((int(wall.replace(tzinfo=IST).timestamp()), float(t[1])))
    return _bucket(ticks, IST, m["open"], m["close"])


def intraday(market: str, symbol: str) -> dict[str, Any]:
    key = f"intra:{market}:{symbol}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    m = MARKETS[market]
    bars: list[dict[str, Any]] = []
    note = "Underlying cash price (futures track it within the basis)." if market == "IN" else ""
    try:
        bars = intraday_raw(market, symbol)
    except KeyError:
        raise
    except Exception as exc:  # noqa: BLE001
        note = f"intraday unavailable: {exc}"
    orb = orb_status(bars, m["tz"], m["open"])
    res = {"market": market, "symbol": symbol, "bars": bars, "orb": orb, "note": note, "market_status": market_status(market)}
    return cache.set(key, res, 20)


def orb_status(bars: list[dict], tz: ZoneInfo, open_t: dtime) -> dict[str, Any]:
    """5-minute Opening Range Breakout state (the Zarattini-Barbon-Aziz rule)."""
    if not bars:
        return {"state": "no_data", "text": "No intraday bars yet. Check back after the session opens."}
    d0 = datetime.fromtimestamp(bars[-1]["time"], tz).date()
    day = [b for b in bars if datetime.fromtimestamp(b["time"], tz).date() == d0]
    first = day[0]
    first_t = datetime.fromtimestamp(first["time"], tz).time()
    if abs((datetime.combine(d0, first_t) - datetime.combine(d0, open_t)).total_seconds()) > 600:
        return {"state": "no_data", "text": "Opening candle not in the data (feed started late)."}
    hi, lo = first["high"], first["low"]
    direction = "long" if first["close"] > first["open"] else ("short" if first["close"] < first["open"] else "none")
    base = {"or_high": round(hi, 2), "or_low": round(lo, 2), "direction": direction, "first_open": round(first["open"], 2), "first_close": round(first["close"], 2)}
    if direction == "none":
        return {**base, "state": "doji", "text": "Opening candle is a doji - the ORB rule takes no trade."}
    if len(day) == 1:
        return {**base, "state": "forming", "text": f"First 5-min candle is {'green' if direction == 'long' else 'red'}. {'Buy' if direction == 'long' else 'Sell'} on a break of {'%.2f' % (hi if direction == 'long' else lo)}; stop at {'%.2f' % (lo if direction == 'long' else hi)}."}
    trig = hi if direction == "long" else lo
    stop = lo if direction == "long" else hi
    for b in day[1:]:
        t = datetime.fromtimestamp(b["time"], tz).strftime("%H:%M")
        if direction == "long" and b["low"] <= stop and b["high"] < trig:
            return {**base, "state": "failed", "text": f"Fell through {stop:.2f} before breaking out ({t}). No trade today."}
        if (direction == "long" and b["high"] > trig) or (direction == "short" and b["low"] < trig):
            last = day[-1]["close"]
            sgn = 1 if direction == "long" else -1
            risk = abs(trig - stop)
            r_now = (last - trig) * sgn / risk if risk else 0
            stopped = any((x["low"] <= stop if direction == "long" else x["high"] >= stop) for x in day[day.index(b) + 1 :])
            if stopped:
                return {**base, "state": "stopped", "trigger_time": t, "text": f"Triggered at {trig:.2f} ({t}) then hit the stop {stop:.2f}."}
            return {**base, "state": "triggered", "trigger_time": t, "entry": round(trig, 2), "stop": round(stop, 2), "r_now": round(r_now, 2), "text": f"{'LONG' if direction == 'long' else 'SHORT'} triggered at {trig:.2f} ({t}). Stop {stop:.2f}. Now {r_now:+.2f}R. Exit at the close."}
    return {**base, "state": "waiting", "text": f"Waiting. {'Buy' if direction == 'long' else 'Sell'} on a break of {trig:.2f}; stop {stop:.2f}. Price now {day[-1]['close']:.2f}."}


def orb_board(market: str, limit: int = 25) -> dict[str, Any]:
    """Live board of the 5-minute ORB on the highest relative-volume names (the published 'Stocks in Play' rule)."""
    key = f"orbboard:{market}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    ctxs = contexts(market)
    snap = tv.snapshot(market, list(ctxs.keys()))
    cands = []
    for sid, ctx in ctxs.items():
        if ctx["inst"].get("kind") == "index":
            continue
        row = tv.lookup(snap, sid)
        price = ctx["bars"][-1]["close"]
        if market == "US" and price < 5:
            continue
        rv = row.get("relative_volume_10d_calc") or strategies._rvol(ctx, ctx["n"] - 1) or 0
        cands.append((rv, sid, row, ctx))
    cands.sort(key=lambda x: -x[0])
    top = cands[:limit]
    m = MARKETS[market]

    def one(x):
        rv, sid, row, ctx = x
        try:
            bars = intraday_raw(market, sid)
        except Exception:  # noqa: BLE001
            bars = []
        o = orb_status(bars, m["tz"], m["open"])
        return {
            "symbol": sid,
            "rvol": round(rv, 2),
            "gap": row.get("gap"),
            "change": row.get("change"),
            "price": row.get("close"),
            "state": o.get("state"),
            "direction": o.get("direction"),
            "or_high": o.get("or_high"),
            "or_low": o.get("or_low"),
            "entry": o.get("entry"),
            "stop": o.get("stop"),
            "r_now": o.get("r_now"),
            "text": o.get("text"),
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(one, top))
    order = {"triggered": 0, "waiting": 1, "forming": 2, "stopped": 3, "failed": 4, "doji": 5, "no_data": 6}
    rows.sort(key=lambda r: (order.get(r["state"], 9), -r["rvol"]))
    return cache.set(key, {"market": market, "rows": rows, "market_status": market_status(market)}, 25)
