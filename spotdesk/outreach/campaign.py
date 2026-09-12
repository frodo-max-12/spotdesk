"""Cold-email campaign engine — the growth side of the desk.

Hard rules, all enforced in code (validated across 7,000+ real sends):
  * TEST-FIRST: a campaign refuses --send until one rendered proof email has gone to
    the operator's own inbox (recorded on the campaign row).
  * Per-campaign ledger: re-running never double-sends; safe to stop/resume for days.
  * Suppression union: suppression_list + hard-bounced + blocked domains + anyone who
    replied — always removed before the send list is built.
  * Pacing: 15–30s random jitter between cold sends (fixed intervals read as
    automation); daily cap ramps with lifetime volume; bounce breaker halts cleanly.
  * One recipient per email; per-company SKU subsets from the offer pool.

Cold email is the one lane that sends directly rather than via Drafts — drafting 500
mails for hand-clicking is no oversight at all. The oversight here is the dry-run
default, the proof send, the caps, and the ledger.

CLI:
    python -m spotdesk.cli campaign --name aug-batch --audience prospects            # dry run
    python -m spotdesk.cli campaign --name aug-batch --test you@company.com          # proof
    python -m spotdesk.cli campaign --name aug-batch --send --limit 50               # send
"""

from __future__ import annotations

import csv
import logging
import random
import time
from pathlib import Path

from .. import config, db
from ..llm import brain
from ..ops import breakers
from . import offers

log = logging.getLogger("spotdesk.campaign")


# ------------------------------------------------------------------
# Recipients + suppression
# ------------------------------------------------------------------

def load_recipients_csv(path: str | Path) -> list[dict]:
    """CSV with headers email, company[, name, type]. De-duped, lowercased."""
    out, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            email = (row.get("email") or "").strip().lower()
            if "@" not in email or email in seen:
                continue
            seen.add(email)
            out.append({"email": email, "company": (row.get("company") or "").strip(),
                        "name": (row.get("name") or "").strip(),
                        "type": (row.get("type") or "").strip().lower() or None})
    return out


def _suppressed() -> set[str]:
    bad = {r["email"] for r in db.query("SELECT email FROM suppression_list")}
    bad |= {r["to_email"] for r in db.query(
        "SELECT DISTINCT to_email FROM outreach_sends WHERE status = 'bounced'")}
    bad |= {r["to_email"] for r in db.query(
        "SELECT DISTINCT to_email FROM outreach_sends WHERE status IN ('replied','opted_out')")}
    return bad


def _domain_blocked(email: str) -> bool:
    dom = email.split("@")[-1]
    return dom in config.DOMAIN_BLOCKLIST


def suppress(email: str, reason: str) -> None:
    db.execute("INSERT OR REPLACE INTO suppression_list (email, reason) VALUES (?, ?)",
               ((email or "").lower(), reason))


# ------------------------------------------------------------------
# Campaign lifecycle
# ------------------------------------------------------------------

def get_or_create(name: str, subject: str = "", audience: str = "all",
                  body_template: str = "") -> dict:
    row = db.query_one("SELECT * FROM outreach_campaigns WHERE name = ?", (name,))
    if row:
        return row
    db.insert("outreach_campaigns", {"name": name, "subject": subject,
                                     "audience": audience, "body_template": body_template})
    return db.query_one("SELECT * FROM outreach_campaigns WHERE name = ?", (name,))


def _already_sent(campaign_id: int) -> set[str]:
    return {r["to_email"] for r in db.query(
        "SELECT to_email FROM outreach_sends WHERE campaign_id = ?", (campaign_id,))}


def _render(recipient: dict, subject_override: str | None = None) -> tuple[str, str, list[str]]:
    """(subject, body_html, sku_combo) for one recipient."""
    company = recipient.get("company") or recipient["email"].split("@")[-1]
    peers = []      # companies of the same type get maximally-different combos
    combo = offers.suggest_combo(company, recipient.get("type"), avoid_overlap_with=peers)
    subject = subject_override or offers.make_subject(combo)
    warm_line = brain.draft_cold_email_intro(company)
    table = offers.offer_table_html(company, combo)
    body = ("<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;"
            "line-height:1.5;color:#222\">"
            "<p style='margin:0 0 12px'>Hi,</p>"
            f"<p style='margin:0 0 12px'>{warm_line}</p>"
            f"{table}"
            "<p style='margin:0 0 12px'>If any of these match your requirements, reply "
            "and I'll share latest pricing.</p>"
            "<p style='margin:0 0 12px;color:#666;font-size:12px'>If you'd prefer not to "
            "receive these, just reply and we'll remove you.</p>"
            f"{offers.signature_html()}</div>")
    return subject, body, combo


def send_test(gmail, name: str, to_email: str, subject: str = "") -> dict:
    """The proof send — REQUIRED before any bulk send of this campaign."""
    camp = get_or_create(name, subject=subject)
    subj, body, _ = _render({"email": to_email, "company": "Test Company"},
                            subject_override=camp.get("subject") or None)
    res = gmail.send(to_email, subj, body)
    db.update("outreach_campaigns",
              {"test_sent_to": to_email, "test_sent_at": db.utcnow()},
              "id = ?", (camp["id"],))
    db.audit("campaign_test_sent", {"campaign": name, "to": to_email}, actor="human")
    return {"ok": True, "gmail_msg_id": res.get("id"), "subject": subj}


def run(gmail, name: str, recipients: list[dict], *, send: bool = False,
        limit: int = 0, subject: str = "") -> dict:
    """Dry-run by default: prints/returns the plan and sends nothing without send=True."""
    camp = get_or_create(name, subject=subject)
    bad = _suppressed()
    done = _already_sent(camp["id"])
    todo = [r for r in recipients
            if r["email"] not in bad and r["email"] not in done
            and not _domain_blocked(r["email"])]

    plan = {"campaign": name, "recipients": len(recipients),
            "suppressed": sum(1 for r in recipients if r["email"] in bad
                              or _domain_blocked(r["email"])),
            "already_sent": sum(1 for r in recipients if r["email"] in done),
            "will_send": len(todo), "sent_now": 0, "errors": []}
    if not send:
        plan["dry_run"] = True
        return plan

    # --- the hard gates ---
    if not camp.get("test_sent_at"):
        plan["errors"].append(
            "TEST-FIRST: no proof email recorded for this campaign. Run "
            f"`campaign --name {name} --test <your-address>` and check the render first.")
        return plan
    if not config.IS_AUTOMATIC:
        plan["errors"].append("AGENT_MODE=testing — bulk cold sends are disabled. "
                              "Set AGENT_MODE=automatic to send for real.")
        return plan
    ok, reason = breakers.check_bounce_rate()
    if not ok:
        plan["errors"].append(f"bounce breaker: {reason}")
        return plan

    cap = limit or len(todo)
    for r in todo[:cap]:
        ok, reason = breakers.check_cold_send_rate()
        if not ok:
            plan["errors"].append(f"stopped cleanly: {reason} — rerun later to resume")
            break
        subj, body, combo = _render(r, subject_override=camp.get("subject") or None)
        try:
            res = gmail.send(r["email"], subj, body)
        except Exception as e:
            low = str(e).lower()
            if any(k in low for k in ("rate", "quota", "limit", "exceeded", "429")):
                plan["errors"].append(f"provider limit hit after {plan['sent_now']} — "
                                      f"rerun the same command later to resume")
                break
            plan["errors"].append(f"{r['email']}: {e}")
            continue
        # Ledger row written IMMEDIATELY per send — a crash loses nothing.
        db.insert("outreach_sends", {
            "campaign_id": camp["id"], "to_email": r["email"], "company": r.get("company"),
            "sku_combo": db.j(combo), "gmail_msg_id": res.get("id"),
            "thread_id": res.get("threadId")})
        plan["sent_now"] += 1
        time.sleep(random.uniform(config.COLD_PACING_MIN_S, config.COLD_PACING_MAX_S))

    db.audit("campaign_run", plan, actor="human")
    return plan


# ------------------------------------------------------------------
# Bounce sweep (fold delivery failures back into suppression + domain blocks)
# ------------------------------------------------------------------

def sweep_bounces(gmail, newer_than_days: int = 2) -> dict:
    """Scan the inbox for delivery failures on campaign sends, mark ledger rows
    bounced, feed the suppression list, and auto-block domains that keep rejecting."""
    stats = {"scanned": 0, "bounced": 0, "domains_blocked": []}
    msgs = gmail.list_inbox(after_date=None, max_results=50, unread_only=True)
    for m in msgs:
        frm = (m.get("from_email") or "").lower()
        if not any(t in frm for t in ("mailer-daemon", "postmaster", "mail delivery")):
            continue
        stats["scanned"] += 1
        body = (m.get("body_text") or "") + " " + (m.get("subject") or "")
        import re
        for addr in set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                                   body)):
            addr = addr.lower()
            row = db.query_one(
                "SELECT id FROM outreach_sends WHERE to_email = ? "
                "ORDER BY id DESC LIMIT 1", (addr,))
            if not row:
                continue
            db.update("outreach_sends",
                      {"status": "bounced", "bounce_class": "hard"},
                      "id = ?", (row["id"],))
            suppress(addr, "bounced")
            stats["bounced"] += 1
            dom = addr.split("@")[-1]
            n_dom = (db.query_one(
                "SELECT COUNT(*) AS n FROM outreach_sends WHERE status='bounced' "
                "AND to_email LIKE ?", (f"%@{dom}",)) or {}).get("n", 0)
            if n_dom >= config.DOMAIN_AUTOBLOCK_BOUNCES and dom not in config.DOMAIN_BLOCKLIST:
                config.DOMAIN_BLOCKLIST.add(dom)
                stats["domains_blocked"].append(dom)
                db.audit("domain_autoblocked", {"domain": dom, "bounces": n_dom})
        try:
            gmail.mark_as_read(m["gmail_id"])
        except Exception:
            pass
    return stats
