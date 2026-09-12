"""Drafts-first human-in-the-loop.

The contract with the operator:

  * The agent NEVER emails a counterparty on its own unless a per-action policy says
    "auto" AND AGENT_MODE=automatic AND the gates pass. Everything else becomes a
    Gmail DRAFT, threaded into the right conversation. The human's whole job is a
    judgment check and the Send button — high-value input, very few bits.
  * The Drafts folder always holds the CURRENT answer: when the underlying state
    changes (re-priced quote, revised quantity), the agent deletes the stale draft and
    writes a fresh one (`supersede`). A human never has to wonder which version is live.
  * The agent then WATCHES its drafts: draft gone + a sent message from us on the
    thread ⇒ the human approved (advance the deal); draft gone + nothing sent ⇒ the
    human dismissed it (record and stop pushing).

Every outbound is one row in outbound_actions, whatever its transport.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import config, db
from ..ops import breakers

log = logging.getLogger("spotdesk.hitl")


def _policy(kind: str) -> str:
    return config.SEND_POLICY.get(kind, "draft")


def dispatch(gmail, *, kind: str, to: str, subject: str, body_html: str,
             cc: list[str] | None = None, thread_id: str | None = None,
             in_reply_to: str | None = None, deal_id: int | None = None,
             vendor_rfq_id: int | None = None, vendor_po_id: int | None = None,
             note: str | None = None, force_draft: bool = False) -> dict:
    """Send-or-draft one outbound according to policy. Returns
    {action_id, status, thread_id, message_id}.

    Auto-send happens ONLY when: policy says auto, AGENT_MODE=automatic, not
    force_draft, and the send-rate breaker clears. Anything else drafts."""
    cc = [a for a in (cc or []) if a]
    # Oversight CC rides on every REAL outbound (never on drafts — the human adds
    # nothing by being CC'd on a mail that hasn't been sent yet; Gmail sends the CC
    # list that's on the draft, which already includes it below).
    for a in config.OVERSIGHT_CC:
        if a and a not in cc and a.lower() != (to or "").lower():
            cc.append(a)

    want_auto = (_policy(kind) == "auto") and config.IS_AUTOMATIC and not force_draft
    if want_auto:
        ok, reason = breakers.check_send_rate()
        if not ok:
            log.warning("auto-send blocked by breaker (%s) — drafting instead", reason)
            want_auto = False

    row = {
        "kind": kind, "deal_id": deal_id, "vendor_rfq_id": vendor_rfq_id,
        "vendor_po_id": vendor_po_id, "to_email": to, "cc": ", ".join(cc) or None,
        "subject": subject, "body_html": body_html, "thread_id": thread_id, "note": note,
    }

    if want_auto:
        res = gmail.send(to, subject, body_html, cc=cc, thread_id=thread_id,
                         in_reply_to=in_reply_to)
        row.update(status="auto_sent", sent_gmail_id=res.get("id"),
                   thread_id=res.get("threadId") or thread_id, sent_at=db.utcnow())
        action_id = db.insert("outbound_actions", row)
        db.audit("outbound_auto_sent", {"action_id": action_id, "kind": kind, "to": to})
        return {"action_id": action_id, "status": "auto_sent",
                "thread_id": row["thread_id"], "message_id": res.get("id")}

    d = gmail.create_draft(to, subject, body_html, cc=cc, thread_id=thread_id,
                           in_reply_to=in_reply_to)
    row.update(status="drafted", gmail_draft_id=d["draft_id"],
               draft_message_id=d["message_id"], thread_id=d["thread_id"] or thread_id)
    action_id = db.insert("outbound_actions", row)
    db.audit("outbound_drafted", {"action_id": action_id, "kind": kind, "to": to,
                                  "draft_id": d["draft_id"]})
    return {"action_id": action_id, "status": "drafted",
            "thread_id": row["thread_id"], "message_id": d["message_id"]}


def supersede(gmail, old_action_id: int, **dispatch_kwargs) -> dict:
    """Replace a stale draft with a fresh one (the 'always a running version' rule).
    Deletes the old Gmail draft if it still exists, marks the row superseded, then
    dispatches the replacement."""
    old = db.query_one("SELECT * FROM outbound_actions WHERE id = ?", (old_action_id,))
    if old and old["status"] == "drafted" and old["gmail_draft_id"]:
        try:
            gmail.delete_draft(old["gmail_draft_id"])
        except Exception as e:
            log.warning("could not delete stale draft %s: %s", old["gmail_draft_id"], e)
        db.update("outbound_actions", {"status": "superseded", "updated_at": db.utcnow()},
                  "id = ?", (old_action_id,))
    res = dispatch(gmail, **dispatch_kwargs)
    db.update("outbound_actions", {"supersedes_id": old_action_id},
              "id = ?", (res["action_id"],))
    return res


def supersede_open_drafts(gmail, *, deal_id: int, kind: str, **dispatch_kwargs) -> dict:
    """Convenience: supersede whatever draft of `kind` is currently open on a deal
    (or plain-dispatch if none is)."""
    open_rows = db.query(
        "SELECT id FROM outbound_actions WHERE deal_id = ? AND kind = ? AND status = 'drafted' "
        "ORDER BY id DESC", (deal_id, kind))
    if open_rows:
        # Replace the newest; mark any older strays superseded too.
        for stray in open_rows[1:]:
            row = db.query_one("SELECT gmail_draft_id FROM outbound_actions WHERE id = ?",
                               (stray["id"],))
            if row and row.get("gmail_draft_id"):
                try:
                    gmail.delete_draft(row["gmail_draft_id"])
                except Exception:
                    pass
            db.update("outbound_actions", {"status": "superseded"}, "id = ?", (stray["id"],))
        return supersede(gmail, open_rows[0]["id"], deal_id=deal_id, kind=kind, **dispatch_kwargs)
    return dispatch(gmail, deal_id=deal_id, kind=kind, **dispatch_kwargs)


def release(gmail, action_id: int) -> dict:
    """One-click release: send the reviewed draft AS-IS via drafts.send (whatever the
    human edited in Gmail is exactly what goes out). Used by the dashboard/CLI for
    batch actions like 'release all RFQs on this deal'."""
    row = db.query_one("SELECT * FROM outbound_actions WHERE id = ?", (action_id,))
    if not row:
        return {"ok": False, "reason": "not found"}
    if row["status"] != "drafted":
        return {"ok": False, "reason": f"status is {row['status']}"}
    ok, reason = breakers.check_send_rate()
    if not ok:
        return {"ok": False, "reason": reason}
    res = gmail.send_draft(row["gmail_draft_id"])
    if res is None:   # human beat us to it (or deleted it) — the poller will reconcile
        return {"ok": False, "reason": "draft no longer exists"}
    msg = res  # drafts.send returns the sent message resource
    db.update("outbound_actions",
              {"status": "sent", "sent_gmail_id": msg.get("id"),
               "thread_id": msg.get("threadId") or row["thread_id"],
               "sent_at": db.utcnow(), "updated_at": db.utcnow()},
              "id = ?", (action_id,))
    db.audit("outbound_released", {"action_id": action_id, "kind": row["kind"],
                                   "to": row["to_email"]}, actor="human")
    return {"ok": True, "gmail_msg_id": msg.get("id"),
            "thread_id": msg.get("threadId") or row["thread_id"]}


def poll_draft_outcomes(gmail) -> dict:
    """Reconcile every open draft with reality:
       draft still exists            → still pending, nothing to do
       draft gone + we sent on thread → human pressed Send → status 'sent'
       draft gone + nothing sent      → human discarded it → status 'dismissed'
    Returns counters. Callers react to newly-'sent' rows via their own monitors."""
    stats = {"pending": 0, "sent": 0, "dismissed": 0}
    rows = db.query("SELECT * FROM outbound_actions WHERE status = 'drafted' ORDER BY id")
    for row in rows:
        draft_id = row.get("gmail_draft_id")
        if not draft_id:
            continue
        try:
            if gmail.draft_exists(draft_id):
                stats["pending"] += 1
                continue
        except Exception as e:
            log.warning("draft_exists(%s) failed: %s", draft_id, e)
            continue
        # Draft is gone. Did it become a sent message?
        created = row.get("created_at") or ""
        try:
            epoch_ms = int(datetime.fromisoformat(created).replace(
                tzinfo=timezone.utc).timestamp() * 1000)
        except (ValueError, TypeError):
            epoch_ms = 0
        sent_msg = gmail.find_sent_in_thread(row["thread_id"], after_epoch_ms=epoch_ms) \
            if row.get("thread_id") else None
        if sent_msg:
            db.update("outbound_actions",
                      {"status": "sent", "sent_gmail_id": sent_msg.get("id"),
                       "sent_at": db.utcnow(), "updated_at": db.utcnow()},
                      "id = ?", (row["id"],))
            db.audit("outbound_sent_by_human", {"action_id": row["id"], "kind": row["kind"],
                                                "to": row["to_email"]}, actor="human")
            stats["sent"] += 1
        else:
            db.update("outbound_actions",
                      {"status": "dismissed", "updated_at": db.utcnow()},
                      "id = ?", (row["id"],))
            db.audit("outbound_dismissed", {"action_id": row["id"], "kind": row["kind"],
                                            "to": row["to_email"]}, actor="human")
            stats["dismissed"] += 1
    return stats


def newly_sent(kind: str | None = None, since_id: int = 0) -> list[dict]:
    """Rows that transitioned to sent/auto_sent — monitors use this to advance deals."""
    sql = ("SELECT * FROM outbound_actions WHERE status IN ('sent','auto_sent') "
           "AND id > ?")
    params: tuple = (since_id,)
    if kind:
        sql += " AND kind = ?"
        params += (kind,)
    return db.query(sql + " ORDER BY id", params)
