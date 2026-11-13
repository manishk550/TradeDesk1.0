from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

from .common import cache, http_get

BULL = (
    "beat", "beats", "surge", "surges", "rally", "rallies", "upgrade", "upgraded", "record", "profit", "growth",
    "bullish", "soar", "soars", "jump", "jumps", "outperform", "raised", "strong", "wins", "win", "order", "orders",
    "approval", "rebound", "buyback", "dividend", "gains", "gain", "rises", "climbs", "buy", "target raised",
    "tops", "topped", "boost", "expansion", "contract", "deal",
)
BEAR = (
    "miss", "misses", "fall", "falls", "drop", "drops", "downgrade", "downgraded", "lawsuit", "probe", "crash",
    "bearish", "cut", "cuts", "warning", "slump", "underperform", "weak", "fraud", "investigation", "layoff",
    "layoffs", "ban", "tariff", "delay", "recall", "loss", "losses", "slumps", "plunge", "plunges", "sell",
    "penalty", "fine", "raid", "default", "resigns", "resignation", "sebi", "short seller",
)


def score_headline(text: str) -> int:
    hay = f" {re.sub(r'[^a-z0-9]+', ' ', text.lower())} "
    s = sum(1 for w in BULL if f" {w} " in hay) - sum(1 for w in BEAR if f" {w} " in hay)
    return max(-2, min(2, s))


def fetch(query: str, market: str, limit: int = 8) -> list[dict[str, Any]]:
    key = f"news:{market}:{query}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    loc = {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"} if market == "IN" else {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": f"{query} when:3d", **loc})
    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(http_get(url, {"Accept": "application/rss+xml"}, timeout=15, retries=2))
        for node in root.findall("./channel/item")[:limit]:
            title = (node.findtext("title") or "").strip()
            source = ""
            src = node.find("source")
            if src is not None and src.text:
                source = src.text.strip()
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]
            try:
                ts = int(parsedate_to_datetime(node.findtext("pubDate") or "").timestamp())
            except (TypeError, ValueError):
                ts = None
            sc = score_headline(title)
            items.append(
                {
                    "title": title,
                    "url": (node.findtext("link") or "").strip(),
                    "source": source,
                    "ts": ts,
                    "sentiment": "bullish" if sc > 0 else ("bearish" if sc < 0 else "neutral"),
                    "score": sc,
                }
            )
    except Exception:  # noqa: BLE001
        items = []
    return cache.set(key, items, 900)


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(i["score"] for i in items)
    label = "bullish" if total >= 2 else ("bearish" if total <= -2 else "neutral")
    return {"score": total, "label": label, "count": len(items)}


def query_for(inst: dict[str, Any], market: str) -> str:
    sym = inst["id"]
    if market == "IN":
        special = {"NIFTY": "Nifty 50", "BANKNIFTY": "Bank Nifty", "FINNIFTY": "Fin Nifty", "MIDCPNIFTY": "Nifty Midcap Select"}
        if sym in special:
            return special[sym]
        return f"{sym} share NSE stock"
    return f"{inst.get('name') or sym} {inst.get('nasdaq') or sym} stock"
