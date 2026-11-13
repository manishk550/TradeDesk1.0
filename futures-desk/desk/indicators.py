from __future__ import annotations

from typing import Any

Series = list


def sma(vals: list[float], n: int) -> Series:
    out: Series = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals: list[float], n: int) -> Series:
    out: Series = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(vals: list[float], n: int) -> Series:
    out: Series = [None] * len(vals)
    if len(vals) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars: list[dict], n: int = 14) -> Series:
    out: Series = [None] * len(bars)
    if len(bars) <= n:
        return out
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    a = sum(trs[1 : n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def macd(vals: list[float]) -> tuple[Series, Series, Series]:
    e12, e26 = ema(vals, 12), ema(vals, 26)
    line: Series = [None if (a is None or b is None) else a - b for a, b in zip(e12, e26)]
    seq = [x for x in line if x is not None]
    sig_seq = ema(seq, 9) if len(seq) >= 9 else [None] * len(seq)
    sig: Series = [None] * len(vals)
    j = 0
    for i, x in enumerate(line):
        if x is not None:
            sig[i] = sig_seq[j]
            j += 1
    hist: Series = [None if (a is None or b is None) else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def rolling_max(vals: list[float], n: int) -> Series:
    """Max of the previous n values (current bar excluded)."""
    out: Series = [None] * len(vals)
    for i in range(n, len(vals)):
        out[i] = max(vals[i - n : i])
    return out


def rolling_min(vals: list[float], n: int) -> Series:
    out: Series = [None] * len(vals)
    for i in range(n, len(vals)):
        out[i] = min(vals[i - n : i])
    return out


def compute(bars: list[dict]) -> dict[str, Any]:
    """All indicator series used by the strategies and the chart."""
    close = [b["close"] for b in bars]
    vol = [b["volume"] for b in bars]
    n = len(bars)
    s: dict[str, Series] = {
        "ema9": ema(close, 9),
        "ema20": ema(close, 20),
        "sma50": sma(close, 50),
        "sma200": sma(close, 200),
        "rsi2": rsi(close, 2),
        "rsi14": rsi(close, 14),
        "atr": atr(bars, 14),
        "sma5": sma(close, 5),
        "vol20": sma(vol, 20),
        "hh20": rolling_max([b["high"] for b in bars], 20),
        "ll20": rolling_min([b["low"] for b in bars], 20),
    }
    m, sg, hist = macd(close)
    s["macd"], s["macd_sig"], s["macd_hist"] = m, sg, hist
    # Bollinger(20,2)
    bb_mid = s["ema20"]
    bb_up: Series = [None] * n
    bb_lo: Series = [None] * n
    sm20 = sma(close, 20)
    for i in range(19, n):
        mean = sm20[i]
        var = sum((close[j] - mean) ** 2 for j in range(i - 19, i + 1)) / 20
        sd = var**0.5
        bb_up[i] = mean + 2 * sd
        bb_lo[i] = mean - 2 * sd
    s["bb_up"], s["bb_lo"] = bb_up, bb_lo
    # Supertrend(10,3)
    st: Series = [None] * n
    st_dir: list[int | None] = [None] * n
    a10 = atr(bars, 10)
    fub = flb = None
    direction = 1
    for i in range(n):
        if a10[i] is None:
            continue
        hl2 = (bars[i]["high"] + bars[i]["low"]) / 2
        ub, lb = hl2 + 3 * a10[i], hl2 - 3 * a10[i]
        if fub is None:
            fub, flb = ub, lb
        else:
            fub = ub if (ub < fub or close[i - 1] > fub) else fub
            flb = lb if (lb > flb or close[i - 1] < flb) else flb
        if direction == 1 and close[i] < flb:
            direction = -1
        elif direction == -1 and close[i] > fub:
            direction = 1
        st[i] = flb if direction == 1 else fub
        st_dir[i] = direction
    s["supertrend"] = st
    return {"series": s, "dir": st_dir}


def last(series: Series) -> float | None:
    for v in reversed(series):
        if v is not None:
            return v
    return None
