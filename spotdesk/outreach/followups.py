"""Threaded weekly follow-ups on silent cold contacts.

Threading needs three things (all handled here): Gmail's threadId, an In-Reply-To /
References header carrying the ORIGINAL RFC Message-ID, and the SAME subject (Gmail
threads on it — no manufactured "Re:"). RFC ids are fetched lazily and cached on the
ledger row (gotcha: Gmail returns headers lowercase).

Threaded replies on owned threads are high-trust: 1s flat pacing is safe (validated —
621 sends, 0 failures). Never apply that to cold sends.

Excluded always: bounced, already-replied, suppressed, blocked domains.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .. import config, db
from . import offers
from .campaign import _domain_blocked, _suppressed

log = logging.getLogger("spotdesk.followups")

# Fixed row-position quantities for follow-ups — same numbers week over week for
# visual consistency (ugly but coherent).
FOLLOWUP_QUANTITIES = [2400, 1800, 4800, 6200, 4000]


def _ensure_rfc_id(gmail, send_row: dict) -> str | None:
    if send_row.get("rfc_message_id"):
        return send_row["rfc_message_id"]
    if not send_row.get("gmail_msg_id"):
        return None
    rfc = gmail.get_rfc_message_id(send_row["gmail_msg_id"])
    if rfc:
        db.update("outreach_sends", {"rfc_message_id": rfc}, "id = ?", (send_row["id"],))
    return rfc


def run(gmail, *, min_days_since_touch: int = 7, limit: int = 0,
        send: bool = False) -> dict:
    """Weekly bump to every silent cold contact. Dry-run by default."""
    bad = _suppressed()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=min_days_since_touch)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.query(
        "SELECT s.*, c.subject AS campaign_subject FROM outreach_sends s "
        "JOIN outreach_campaigns c ON c.id = s.campaign_id "
        "WHERE s.status = 'sent' "
        "AND COALESCE(s.last_followup_at, s.sent_at) < ? ORDER BY s.id", (cutoff,))
    todo = [r for r in rows if r["to_email"] not in bad
            and not _domain_blocked(r["to_email"]) and r.get("thread_id")]
    plan = {"eligible": len(todo), "sent_now": 0, "errors": []}
    if not send:
        plan["dry_run"] = True
        return plan
    if not config.IS_AUTOMATIC:
        plan["errors"].append("AGENT_MODE=testing — follow-up sends disabled")
        return plan

    cap = limit or len(todo)
    for r in todo[:cap]:
        from ..ops import breakers
        ok, reason = breakers.check_send_rate()
        if not ok:
            plan["errors"].append(f"stopped: {reason}")
            break
        company = r.get("company") or r["to_email"].split("@")[-1]
        combo = offers.suggest_combo(company)          # rotate: different from last time
        table = offers.offer_table_html(company, combo,
                                        fixed_quantities=FOLLOWUP_QUANTITIES)
        body = ("<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;"
                "line-height:1.5;color:#222\">"
                "<p style='margin:0 0 12px'>Hi,</p>"
                "<p style='margin:0 0 12px'>Spot stock update for this week - see below:</p>"
                f"{table}"
                "<p style='margin:0 0 12px'>Reply with what's open and I'll send pricing "
                "today.</p>"
                f"{offers.signature_html()}</div>")
        rfc = _ensure_rfc_id(gmail, r)
        # Same subject as the original — that's what makes Gmail thread it.
        subject = r.get("campaign_subject") or "Spot Stock Available"
        try:
            gmail.send(r["to_email"], subject, body, thread_id=r["thread_id"],
                       in_reply_to=rfc)
        except Exception as e:
            plan["errors"].append(f"{r['to_email']}: {e}")
            continue
        db.update("outreach_sends",
                  {"followup_count": (r.get("followup_count") or 0) + 1,
                   "last_followup_at": db.utcnow(), "sku_combo": db.j(combo)},
                  "id = ?", (r["id"],))
        plan["sent_now"] += 1
        time.sleep(config.FOLLOWUP_PACING_S)
    db.audit("followup_run", plan, actor="human")
    return plan
