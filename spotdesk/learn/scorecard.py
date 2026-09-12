"""Vendor scorecard — learned routing priority from real outcomes.

Blends, per vendor with history:
    win rate       (their cost selected as the winner)   × 0.35
    reply rate     (RFQs answered)                        × 0.30
    response speed (reply within ~3 days = full marks)    × 0.20
    lead time      (~12-week lead = zero)                 × 0.15

score → dynamic_priority (1 best .. 5). Routing uses a MANUAL priority when set,
else this — the agent self-ranks the vendors nobody ranked, and a human always wins.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .. import db
from ..pipeline.pricing import lead_days

log = logging.getLogger("spotdesk.scorecard")

_RESP_CAP_H = 72.0
_LEAD_CAP_D = 84.0


def _hours_between(a, b) -> float | None:
    try:
        da = datetime.fromisoformat(str(a))
        dbb = datetime.fromisoformat(str(b))
        if dbb < da:
            return None
        return (dbb - da).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def compute_vendor_scores() -> int:
    emails = {r["vendor_email"] for r in db.query(
        "SELECT DISTINCT vendor_email FROM vendor_rfqs WHERE vendor_email IS NOT NULL")}
    emails |= {r["vendor_email"] for r in db.query(
        "SELECT DISTINCT vendor_email FROM vendor_quotes WHERE vendor_email IS NOT NULL")}
    scored = 0
    for email in emails:
        rfqs = db.query("SELECT * FROM vendor_rfqs WHERE vendor_email = ?", (email,))
        quotes = db.query("SELECT * FROM vendor_quotes WHERE vendor_email = ?", (email,))
        attempts = [r for r in rfqs if r["status"] in ("sent", "partial", "replied", "no_bid")]
        replied = [r for r in rfqs if r["status"] in ("partial", "replied")
                   or r.get("replied_at")]
        sent_n, replied_n = len(attempts), len(replied)
        reply_rate = (replied_n / sent_n) if sent_n else 0.0
        resp = [h for h in (_hours_between(r.get("sent_at"), r.get("replied_at"))
                            for r in replied if r.get("sent_at") and r.get("replied_at"))
                if h is not None]
        avg_resp = sum(resp) / len(resp) if resp else None
        q_n = len(quotes)
        won_n = sum(1 for q in quotes if q.get("is_selected"))
        win_rate = (won_n / q_n) if q_n else 0.0
        leads = [d for d in (lead_days(q.get("lead_time")) for q in quotes) if d is not None]
        avg_lead = sum(leads) / len(leads) if leads else None

        if sent_n or q_n:
            s_resp = max(0.0, 1 - avg_resp / _RESP_CAP_H) if avg_resp is not None else 0.5
            s_lead = max(0.0, 1 - avg_lead / _LEAD_CAP_D) if avg_lead is not None else 0.5
            score = round(100 * (0.35 * win_rate + 0.30 * reply_rate
                                 + 0.20 * s_resp + 0.15 * s_lead), 1)
            dyn = 1 if score >= 80 else 2 if score >= 60 else 3 if score >= 40 \
                else 4 if score >= 20 else 5
        else:
            score, dyn = None, None

        db.update("counterparties", {
            "rfqs_sent": sent_n, "rfqs_replied": replied_n,
            "quotes_received": q_n, "quotes_won": won_n,
            "avg_response_hours": round(avg_resp, 1) if avg_resp is not None else None,
            "avg_lead_days": round(avg_lead, 1) if avg_lead is not None else None,
            "score": score, "dynamic_priority": dyn,
        }, "email = ?", ((email or "").lower(),))
        scored += 1
    if scored:
        log.info("vendor scorecard recomputed for %d vendor(s)", scored)
    return scored
