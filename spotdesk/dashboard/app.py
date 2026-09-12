"""The dashboard — deliberately minimal. Gmail is the workspace; this is the index.

One page: pipeline board (deals by stage), pending drafts with DEEP LINKS into the
Gmail thread, breaker status, KPI strip. Two actions only: release a draft (sends the
reviewed draft as-is) and close a deal with an outcome label. Everything else happens
inside Gmail.

Binds localhost by default; refuses a network bind without basic auth (it can send
reviewed drafts).
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .. import config, db
from ..hitl import drafts
from ..learn import outcomes
from ..ops import breakers, health

log = logging.getLogger("spotdesk.dashboard")

app = FastAPI(title="SpotDesk")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
_security = HTTPBasic(auto_error=False)
_gmail = None       # injected by main.py


def set_gmail(client) -> None:
    global _gmail
    _gmail = client


def _auth(credentials: HTTPBasicCredentials = Depends(_security)):
    if not (config.DASHBOARD_USER and config.DASHBOARD_PASSWORD):
        return True
    if (credentials and
            secrets.compare_digest(credentials.username, config.DASHBOARD_USER) and
            secrets.compare_digest(credentials.password, config.DASHBOARD_PASSWORD)):
        return True
    raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def gmail_thread_url(thread_id: str | None) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else "#"


_STAGES = [
    ("sourcing_vendors", "Sourcing"),
    ("quote_drafted", "Quote in Drafts"),
    ("sent", "Quoted"),
    ("follow_up", "Following up"),
    ("negotiation", "Negotiating"),
    ("po_received", "PO received"),
    ("po_placed", "PO placed"),
    ("in_logistics", "In logistics"),
]


@app.get("/", response_class=HTMLResponse)
def board(request: Request, _=Depends(_auth)):
    deals_by_stage = []
    for status, label in _STAGES:
        # Pre-quote stages show only open deals; post-PO stages keep showing a WON
        # deal until it is delivered (winning is not the end of the coordination).
        outcome_filter = "" if status in ("po_received", "po_placed", "in_logistics") \
            else "AND d.outcome IS NULL "
        rows = db.query(
            "SELECT d.*, e.thread_id AS email_thread FROM deals d "
            "LEFT JOIN emails e ON e.id = d.email_id "
            f"WHERE d.status = ? {outcome_filter}ORDER BY d.updated_at DESC LIMIT 25",
            (status,))
        for r in rows:
            r["gmail_url"] = gmail_thread_url(r.get("email_thread"))
        deals_by_stage.append({"label": label, "status": status, "deals": rows})

    pending = db.query(
        "SELECT * FROM outbound_actions WHERE status = 'drafted' ORDER BY id DESC LIMIT 60")
    for p in pending:
        p["gmail_url"] = gmail_thread_url(p.get("thread_id"))

    kpis = db.query("SELECT * FROM system_kpis ORDER BY date DESC LIMIT 1")
    won = db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(clearing_price_usd),0) AS rev "
                       "FROM deals WHERE outcome = 'won'")
    return templates.TemplateResponse(request, "board.html", {
        "stages": deals_by_stage,
        "pending": pending,
        "breakers": breakers.status(),
        "health": health.check(),
        "kpi": kpis[0] if kpis else {},
        "won": won or {},
        "mode": config.AGENT_MODE,
        "company": config.COMPANY_NAME,
    })


@app.post("/action/{action_id}/release")
def release(action_id: int, _=Depends(_auth)):
    """Send the reviewed draft as-is (drafts.send) — whatever the human edited in
    Gmail is exactly what goes out."""
    if _gmail is None:
        raise HTTPException(503, "gmail client not attached")
    res = drafts.release(_gmail, action_id)
    if not res.get("ok"):
        raise HTTPException(409, res.get("reason", "release failed"))
    return RedirectResponse("/", status_code=303)


@app.post("/deal/{deal_id}/close")
def close(deal_id: int, outcome: str = Form(...), clearing: str = Form(""),
          reason: str = Form(""), _=Depends(_auth)):
    price = None
    try:
        price = float(clearing) if clearing.strip() else None
    except ValueError:
        pass
    res = outcomes.close_deal(deal_id, outcome=outcome, clearing_price_usd=price,
                              reason=reason, actor="human")
    if not res.get("ok"):
        raise HTTPException(409, res.get("error", "close failed"))
    return RedirectResponse("/", status_code=303)


@app.get("/api/health")
def api_health(_=Depends(_auth)):
    return health.check()


def validate_security() -> None:
    host = (config.DASHBOARD_HOST or "").strip()
    if host not in ("127.0.0.1", "localhost", "::1", "") and not (
            config.DASHBOARD_USER and config.DASHBOARD_PASSWORD):
        raise SystemExit(
            f"Dashboard host '{host}' is network-exposed with no DASHBOARD_USER/"
            f"DASHBOARD_PASSWORD. Refusing to start — it can send reviewed drafts.")
