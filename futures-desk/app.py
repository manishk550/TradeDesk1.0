from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from desk import agent, india, service, strategies, us

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

app = FastAPI(title="Futures Desk", version="1.0.0")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.on_event("startup")
def _startup() -> None:
    service.boot()


def _market(m: str) -> str:
    m = m.upper()
    if m not in service.MARKETS:
        raise HTTPException(404, "market must be IN or US")
    return m


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "india_sync": india.STATUS,
        "us_sync": {k: us.STATUS[k] for k in ("running", "done", "total", "failed")},
        "evaluating": sorted(service._STATE["evaluating"]),
    }


@app.get("/api/scan")
def scan(market: str = "IN", risk: Optional[float] = None, news: bool = True) -> dict:
    return service.scan(_market(market), risk_budget=risk, with_news=news)


@app.get("/api/detail")
def detail(market: str, symbol: str, risk: Optional[float] = None) -> dict:
    try:
        return service.detail(_market(market), symbol, risk_budget=risk)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown symbol {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/intraday")
def intraday(market: str, symbol: str) -> dict:
    try:
        return service.intraday(_market(market), symbol)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown symbol {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/orb")
def orb(market: str = "IN") -> dict:
    return service.orb_board(_market(market))


@app.post("/api/ask")
def ask(payload: dict = Body(...)) -> dict:
    q = str(payload.get("question") or "").strip()
    if not q:
        raise HTTPException(400, "Ask a question")
    if len(q) > 500:
        raise HTTPException(400, "Question is too long")
    risk = payload.get("risk")
    try:
        risk_f = float(risk) if risk not in (None, "") else None
    except (TypeError, ValueError):
        risk_f = None
    return agent.answer(_market(str(payload.get("market") or "IN")), q, risk_f)


@app.post("/api/rebuild")
def rebuild(market: str = Query("IN")) -> dict:
    ev = service.ensure_eval(_market(market), force=True)
    return {"ok": bool(ev), "seconds": ev and ev.get("seconds")}


@app.get("/api/rules")
def rules() -> list:
    return [{k: v for k, v in r.items() if k != "fn"} for r in strategies.RULES.values()]
