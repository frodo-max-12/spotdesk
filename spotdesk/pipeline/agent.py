"""The orchestrator — one process, four rhythms.

  POLL      (every POLL_INTERVAL_SECONDS)    new inbound mail → intake
  MONITOR   (every MONITOR_INTERVAL_SECONDS) draft outcomes → state advances;
                                             vendor replies → consolidate → quote
  DAILY     follow-ups on silent quotes; censor stale deals; KPI snapshot
  STARTUP   crash recovery + missed-mail backfill (read AND unread since the
            watermark, floored at BACKFILL_FLOOR so an old inbox is never replayed)

The human never has to wake the agent: everything it wants to say is already sitting
in Drafts, and everything the human sends is noticed within one monitor tick.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from .. import config, db
from ..gmail.client import GmailClient
from ..hitl import drafts
from ..knowledge import playbooks
from ..learn import distill, outcomes, scorecard
from ..market import fx as fx_mod
from ..ops import health
from . import intake, orders, pricing, quoting, sourcing
from .sourcing import addrs

log = logging.getLogger("spotdesk.agent")


class DeskAgent:
    def __init__(self, gmail: GmailClient | None = None):
        db.init_schema()
        self.gmail = gmail or GmailClient()
        if self.gmail.service is None:
            self.gmail.authenticate()
        self.running = False
        log.info("agent ready — mode=%s, mailbox=%s", config.AGENT_MODE,
                 self.gmail.user_email)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def run(self):
        self.running = True
        log.info("=" * 60)
        log.info("SpotDesk agent STARTED (%s)", config.AGENT_MODE)
        log.info("=" * 60)
        try:
            self._recover_stranded()
        except Exception:
            log.exception("startup recovery failed")
        try:
            self._backfill_missed_mail()
        except Exception:
            log.exception("startup backfill failed")

        threading.Thread(target=self._monitor_loop, daemon=True).start()
        threading.Thread(target=self._daily_loop, daemon=True).start()

        while self.running:
            try:
                playbooks.reload()          # live playbook edits apply within a cycle
                self._poll_inbox()
                health.heartbeat()
            except Exception:
                log.exception("poll cycle error")
            time.sleep(config.POLL_INTERVAL_SECONDS)

    def stop(self):
        self.running = False

    def run_once(self) -> dict:
        """One full cycle (cron-style / debugging): poll + monitor + daily."""
        playbooks.reload()
        polled = self._poll_inbox()
        monitored = self._monitor_cycle()
        daily = self._daily_cycle()
        health.heartbeat()
        return {"polled": polled, "monitor": monitored, "daily": daily}

    # ------------------------------------------------------------------
    # POLL
    # ------------------------------------------------------------------
    def _watermark(self) -> str | None:
        floor = config.BACKFILL_FLOOR or None
        row = db.query_one("SELECT MAX(received_at) AS m FROM emails")
        wm = None
        if row and row.get("m"):
            wm = str(row["m"])[:10].replace("-", "/")
        if wm and floor:
            return wm if wm > floor else floor
        return wm or floor

    def _poll_inbox(self) -> int:
        emails = self.gmail.list_inbox(after_date=self._watermark(), max_results=10,
                                       unread_only=True)
        n = 0
        for email_data in emails:
            try:
                result = intake.process_email(self.gmail, email_data)
                log.info("processed %s: %s", email_data.get("gmail_id"), result)
                n += 1
            except Exception:
                log.exception("error processing %s", email_data.get("gmail_id"))
        return n

    def _backfill_missed_mail(self):
        """Sweep READ + unread mail since the watermark once at startup — a mail a
        human already opened while the agent was off would otherwise never be seen.
        Dedup makes this idempotent."""
        after = self._watermark()
        if not after:
            log.info("no watermark/floor — skipping backfill (set BACKFILL_FLOOR to enable)")
            return
        emails = self.gmail.list_inbox(after_date=after, max_results=100, unread_only=False)
        if len(emails) >= 100:
            log.warning("backfill hit the 100-mail cap since %s — older missed mail "
                        "may remain", after)
        for email_data in emails:
            try:
                intake.process_email(self.gmail, email_data)
            except Exception:
                log.exception("backfill error on %s", email_data.get("gmail_id"))
        log.info("startup backfill swept %d mail(s) since %s", len(emails), after)

    def _recover_stranded(self):
        """Deals stuck awaiting_pricing with zero RFQ rows = an interrupted intake.
        Resume the fan-out idempotently."""
        for deal in db.query("SELECT * FROM deals WHERE status = 'awaiting_pricing'"):
            n = (db.query_one("SELECT COUNT(*) AS n FROM vendor_rfqs WHERE deal_id = ?",
                              (deal["id"],)) or {}).get("n", 0)
            if n:
                db.update("deals", {"status": "sourcing_vendors"}, "id = ?", (deal["id"],))
                continue
            items = db.uj(deal.get("line_items"), []) or []
            if not items:
                continue
            log.warning("[recovery] %s — deal existed but RFQs never went out",
                        deal["quote_number"])
            sourcing.draft_rfqs(self.gmail, deal, items)
            db.update("deals", {"status": "sourcing_vendors"}, "id = ?", (deal["id"],))

    # ------------------------------------------------------------------
    # MONITOR
    # ------------------------------------------------------------------
    def _monitor_loop(self):
        while self.running:
            try:
                self._monitor_cycle()
            except Exception:
                log.exception("monitor cycle error")
            time.sleep(config.MONITOR_INTERVAL_SECONDS)

    def _monitor_cycle(self) -> dict:
        stats: dict = {}
        try:
            fx_mod.ensure_fresh()
        except Exception as e:
            log.warning("fx refresh failed: %s", e)

        # 1. Reconcile drafts with what the human did in Gmail.
        stats["drafts"] = drafts.poll_draft_outcomes(self.gmail)
        stats["applied"] = self._apply_sent_actions()

        # 2. Deals out sourcing: read vendor replies; when ready, consolidate + quote.
        sourcing_deals = db.query(
            "SELECT * FROM deals WHERE status = 'sourcing_vendors'")
        parsed = consolidated = 0
        for deal in sourcing_deals:
            try:
                parsed += sourcing.check_vendor_replies(self.gmail, deal)
                deal = db.query_one("SELECT * FROM deals WHERE id = ?", (deal["id"],))
                all_done, elapsed = sourcing.sourcing_ready(deal["id"])
                has_quotes = (db.query_one(
                    "SELECT COUNT(*) AS n FROM vendor_quotes WHERE deal_id = ?",
                    (deal["id"],)) or {}).get("n", 0) > 0
                if (all_done or elapsed) and has_quotes:
                    pricing.consolidate_deal(deal)
                    scorecard.compute_vendor_scores()
                    deal = db.query_one("SELECT * FROM deals WHERE id = ?", (deal["id"],))
                    quoting.draft_quote(self.gmail, deal,
                                        is_update=deal.get("sent_at") is not None)
                    consolidated += 1
                elif elapsed and not has_quotes:
                    db.update("deals", {"status": "quote_drafted",
                                        "updated_at": db.utcnow()}, "id = ?", (deal["id"],))
                    db.audit("sourcing_timeout_no_quotes",
                             {"deal": deal["quote_number"]})
                    log.warning("[%s] no vendor quotes within %dh — needs a human",
                                deal["quote_number"], config.VENDOR_REPLY_WAIT_HOURS)
            except Exception:
                log.exception("monitor error on deal %s", deal.get("quote_number"))
        stats["vendor_quotes_parsed"] = parsed
        stats["consolidated"] = consolidated
        return stats

    def _apply_sent_actions(self) -> int:
        """Advance domain state for every outbound that actually went out (whether the
        human pressed Send in Gmail, released it from the dashboard, or policy
        auto-sent it). Watermarked so each action is applied exactly once."""
        row = db.query_one("SELECT last_msg_id FROM ingest_state WHERE source = 'outbound_applied'")
        since_id = int((row or {}).get("last_msg_id") or 0)
        actions = drafts.newly_sent(since_id=since_id)
        applied = 0
        for a in actions:
            try:
                self._apply_one_action(a)
                applied += 1
            except Exception:
                log.exception("apply action %s failed", a["id"])
            since_id = max(since_id, a["id"])
        db.execute("INSERT INTO ingest_state (source, last_msg_id) VALUES ('outbound_applied', ?) "
                   "ON CONFLICT(source) DO UPDATE SET last_msg_id = excluded.last_msg_id",
                   (str(since_id),))
        return applied

    def _apply_one_action(self, a: dict) -> None:
        kind = a["kind"]
        now = db.utcnow()
        if kind == "vendor_rfq" and a.get("vendor_rfq_id"):
            db.update("vendor_rfqs",
                      {"status": "sent", "sent_at": now,
                       "thread_id": a.get("thread_id"),
                       "message_id": a.get("sent_gmail_id")},
                      "id = ? AND status IN ('drafted','sent')", (a["vendor_rfq_id"],))
        elif kind == "quote" and a.get("deal_id"):
            db.update("deals", {"status": "sent", "sent_at": now, "updated_at": now},
                      "id = ? AND status IN ('quote_drafted','negotiation','sent')",
                      (a["deal_id"],))
        elif kind == "vendor_po" and a.get("vendor_po_id"):
            db.update("vendor_pos",
                      {"status": "placed", "placed_at": now,
                       "thread_id": a.get("thread_id"),
                       "message_id": a.get("sent_gmail_id"), "updated_at": now},
                      "id = ? AND status = 'drafted'", (a["vendor_po_id"],))
            db.update("deals", {"status": "po_placed", "updated_at": now},
                      "id = ?", (a.get("deal_id"),))
        elif kind == "followup" and a.get("deal_id"):
            which = (a.get("note") or "").strip()
            field = "followup_2_sent_at" if which == "day7" else "followup_1_sent_at"
            db.update("deals", {field: now, "status": "follow_up", "updated_at": now},
                      "id = ?", (a["deal_id"],))

    # ------------------------------------------------------------------
    # DAILY
    # ------------------------------------------------------------------
    def _daily_loop(self):
        while self.running:
            try:
                self._daily_cycle()
            except Exception:
                log.exception("daily cycle error")
            time.sleep(3600)

    def _daily_cycle(self) -> dict:
        stats = {"followups": self._draft_followups(),
                 "censored": outcomes.expire_silent_deals()}
        try:
            distill.snapshot_kpis()
        except Exception as e:
            log.warning("kpi snapshot failed: %s", e)
        return stats

    def _draft_followups(self) -> int:
        """Day-3 and day-7 nudges on silent quotes — drafted, not auto-sent, and only
        one open follow-up draft per deal at a time."""
        import html as _h
        n = 0
        now = datetime.now(timezone.utc)
        for days, flag_field, which, enabled in (
                (3, "followup_1_sent_at", "day3", config.FOLLOWUP_DAY3_ENABLED),
                (7, "followup_2_sent_at", "day7", config.FOLLOWUP_DAY7_ENABLED)):
            if not enabled:
                continue
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            rows = db.query(
                f"SELECT * FROM deals WHERE status IN ('sent','follow_up') "
                f"AND outcome IS NULL AND sent_at IS NOT NULL AND sent_at < ? "
                f"AND {flag_field} IS NULL", (cutoff,))
            for deal in rows:
                open_fu = db.query_one(
                    "SELECT id FROM outbound_actions WHERE deal_id = ? AND kind='followup' "
                    "AND status = 'drafted'", (deal["id"],))
                if open_fu:
                    continue
                email_row = db.query_one("SELECT thread_id, subject FROM emails WHERE id = ?",
                                         (deal.get("email_id"),)) or {}
                name = _h.escape(deal.get("customer_name") or "Customer")
                if which == "day3":
                    body = (f"<p>Dear {name},</p><p>I hope this message finds you well. "
                            f"I wanted to follow up on our quotation "
                            f"<strong>{deal['quote_number']}</strong> sent a few days ago.</p>"
                            f"<p>Please let us know if you have any questions about the "
                            f"pricing, specifications, or lead times — happy to discuss "
                            f"alternatives if needed.</p>")
                else:
                    body = (f"<p>Dear {name},</p><p>Just a gentle reminder regarding our "
                            f"quotation <strong>{deal['quote_number']}</strong>. If the "
                            f"quoted parts or pricing do not meet your requirements, we "
                            f"would be glad to work on alternatives or revised pricing.</p>")
                body += (f"<p>Best regards,<br>"
                         f"{_h.escape(config.COMPANY_ENTITY)} Sales Team</p>")
                drafts.dispatch(self.gmail, kind="followup", to=deal["customer_email"],
                                subject=email_row.get("subject") or
                                f"Follow-up: {deal['quote_number']}",
                                body_html=body, cc=addrs(deal.get("customer_cc")),
                                thread_id=email_row.get("thread_id"),
                                deal_id=deal["id"], note=which)
                n += 1
        return n
