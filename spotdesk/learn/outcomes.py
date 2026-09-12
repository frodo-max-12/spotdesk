"""Outcomes — the FEEDBACK edge of the compounding loop.

Accumulation is more rows; compounding is each closed deal sharpening the NEXT
decision. That requires labels, and labels require honesty about silence:

  * `close_deal` writes the label AND fires the trust update AND appends the clearing
    price to price_history — one call, one transaction-shaped action. (The predecessor
    system logged outcomes but never called the trust updater; 2.19M rows taught it
    nothing. The arrow is connected here, structurally.)
  * A quote nobody answered is NO_RESPONSE — right-censored, never "lost". ~80% of
    spot RFQs simply ghost; lumping them into losses poisons every downstream read
    with survivorship bias.
  * A customer PO auto-labels the deal WON at intake (capture as a byproduct of the
    workflow, never a separate logging chore).

Also scaffolds the deal's case-study file in deals/ — one SQL row for the numbers,
one markdown file for the story, same quote number joining them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .. import config, db
from ..market import history
from . import trust

log = logging.getLogger("spotdesk.outcomes")

VALID_OUTCOMES = ("won", "lost", "no_response", "abandoned")


def close_deal(deal_id: int, *, outcome: str, clearing_price_usd: float | None = None,
               reason: str = "", actor: str = "human") -> dict:
    """The one command that turns a deal into a label. Returns a summary dict."""
    outcome = (outcome or "").strip().lower()
    if outcome not in VALID_OUTCOMES:
        return {"ok": False, "error": f"outcome must be one of {VALID_OUTCOMES}"}
    deal = db.query_one("SELECT * FROM deals WHERE id = ?", (deal_id,))
    if not deal:
        return {"ok": False, "error": "deal not found"}
    if deal.get("outcome"):
        return {"ok": False, "error": f"already closed as {deal['outcome']}"}

    status_map = {"won": "won", "lost": "lost", "no_response": "no_response",
                  "abandoned": "lost"}
    db.update("deals", {
        "outcome": outcome, "clearing_price_usd": clearing_price_usd,
        "loss_reason": reason or None, "closed_at": db.utcnow(),
        "status": status_map[outcome], "updated_at": db.utcnow(),
    }, "id = ?", (deal_id,))

    # --- the connected flywheel ---------------------------------------------
    cust = (deal.get("customer_email") or "").lower()
    if cust:
        if outcome == "won":
            revenue = clearing_price_usd or deal.get("we_quoted_usd") or 0
            db.execute(
                "UPDATE counterparties SET n_deals_won = n_deals_won + 1, "
                "total_quoted_usd = total_quoted_usd + ?, "
                "total_closed_usd = total_closed_usd + ?, last_seen = ? WHERE email = ?",
                (deal.get("we_quoted_usd") or 0, revenue, db.utcnow(), cust))
            trust.on_deal_won(cust)
        elif outcome in ("lost", "abandoned"):
            db.execute(
                "UPDATE counterparties SET n_deals_lost = n_deals_lost + 1, "
                "total_quoted_usd = total_quoted_usd + ?, last_seen = ? WHERE email = ?",
                (deal.get("we_quoted_usd") or 0, db.utcnow(), cust))
            trust.on_deal_lost(cust)
        # no_response: neutral — censored, moves nothing.

    # Winning vendors learn too (their quote converted to a real order).
    if outcome == "won":
        for vq in db.query(
                "SELECT DISTINCT vendor_email FROM vendor_quotes "
                "WHERE deal_id = ? AND is_selected = 1", (deal_id,)):
            if vq.get("vendor_email"):
                db.execute("UPDATE counterparties SET quotes_won = quotes_won + 1 "
                           "WHERE email = ?", (vq["vendor_email"].lower(),))
                trust.on_deal_won(vq["vendor_email"])

    # Price memory: the clearing price is the most valuable price point there is.
    if clearing_price_usd:
        for it in (db.uj(deal.get("line_items"), []) or []):
            if it.get("mpn") and it.get("unit_price"):
                history.record_price(it["mpn"], it["unit_price"], side="clearing",
                                     source=deal.get("quote_number") or f"deal:{deal_id}",
                                     qty=it.get("quantity"))

    _write_case_study(deal, outcome, clearing_price_usd, reason)
    db.audit("deal_closed", {"deal": deal.get("quote_number"), "outcome": outcome,
                             "clearing": clearing_price_usd, "reason": reason},
             actor=actor)
    log.info("[%s] closed: %s (%s)", deal.get("quote_number"), outcome, reason or "-")
    return {"ok": True, "deal": deal.get("quote_number"), "outcome": outcome}


def expire_silent_deals() -> int:
    """Sent quotes silent past the window → censored no_response (never 'lost').
    The BROKER_PLAYBOOK close-the-loop line is the cheap label-acquisition tool for
    turning these into real outcomes later."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=config.DEAL_NO_RESPONSE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.query(
        "SELECT id FROM deals WHERE status IN ('sent','follow_up') "
        "AND outcome IS NULL AND sent_at IS NOT NULL AND sent_at < ?", (cutoff,))
    n = 0
    for r in rows:
        res = close_deal(r["id"], outcome="no_response",
                         reason=f"silent > {config.DEAL_NO_RESPONSE_DAYS}d", actor="agent")
        if res.get("ok"):
            n += 1
    if n:
        log.info("expired %d silent deal(s) as censored no_response", n)
    return n


def _write_case_study(deal: dict, outcome: str, clearing, reason: str) -> None:
    """deals/<quote_number>.md — the narrative home next to the SQL row."""
    try:
        config.DEALS_DIR.mkdir(parents=True, exist_ok=True)
        path = config.DEALS_DIR / f"{deal.get('quote_number') or deal['id']}.md"
        items = db.uj(deal.get("line_items"), []) or []
        lines = [
            "---",
            f"deal_id: {deal.get('quote_number')}",
            f"customer_company: {deal.get('customer_company') or ''}",
            f"customer_email: {deal.get('customer_email') or ''}",
            f"opened: {deal.get('created_at')}",
            f"closed: {db.utcnow()}",
            f"status: {outcome}",
            f"we_quoted_usd: {deal.get('we_quoted_usd') or ''}",
            f"clearing_price_usd: {clearing or ''}",
            f"loss_reason: {reason or ''}",
            "---", "",
            f"# {deal.get('customer_company') or deal.get('customer_email')} — "
            f"{deal.get('quote_number')}", "",
            "## Lines",
            "| MPN | Qty | Our price | Vendor | Margin % |",
            "|---|---|---|---|---|",
        ]
        for it in items:
            lines.append(f"| {it.get('mpn') or it.get('description') or '-'} "
                         f"| {it.get('quantity') or '-'} | {it.get('unit_price') or '-'} "
                         f"| {it.get('selected_vendor') or '-'} "
                         f"| {it.get('margin_percent') or '-'} |")
        lines += ["", "## Lessons (for cross-deal mining)", "- ",
                  "", "## What we'd do differently", "- "]
        path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        log.warning("case-study write failed: %s", e)
