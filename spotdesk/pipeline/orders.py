"""Orders — customer PO in, vendor POs out, vendor confirmations tracked.

A PO is a real financial commitment, so:
  * the quote-validity guard runs first — if a winning vendor quote expired since we
    quoted, the affected lines are re-sourced, never committed at a stale cost;
  * vendor POs default to DRAFTS whatever the other policies say;
  * a vendor's reply to OUR PO is read as a supplier response (confirmed / dispatched /
    delivered / declined) — never as a customer PO (the old failure mode thanked a
    supplier for "their order");
  * a declined or unclear PO reply flags the deal for a human re-source — the agent
    never auto-places a replacement PO.
"""

from __future__ import annotations

import html as html_mod
import logging
from datetime import datetime, timezone

from .. import config, db
from ..hitl import drafts
from ..llm import brain
from . import bom
from .logistics import handle_logistics_update  # noqa: F401  (re-export for intake)
from .sourcing import addrs

log = logging.getLogger("spotdesk.orders")

_CELL = 'padding:6px;border:1px solid #dddddd;'
_TH = 'padding:6px;border:1px solid #dddddd;text-align:left;'


# ============================================================
# Customer PO
# ============================================================

def _find_deal_for_po(email_data: dict) -> dict | None:
    import re
    if email_data.get("thread_id"):
        row = db.query_one(
            "SELECT d.* FROM deals d JOIN emails e ON d.email_id = e.id "
            "WHERE e.thread_id = ? ORDER BY d.id DESC LIMIT 1",
            (email_data["thread_id"],))
        if row:
            return row
    m = re.search(re.escape(config.QUOTE_PREFIX) + r"-Q-\d{8}-\d{4}",
                  email_data.get("subject") or "")
    if m:
        row = db.query_one("SELECT * FROM deals WHERE quote_number = ?", (m.group(0),))
        if row:
            return row
    frm = (email_data.get("from_email") or "")
    frm = (frm.split("<")[-1].replace(">", "").strip() if "<" in frm else frm).lower()
    return db.query_one(
        "SELECT * FROM deals WHERE customer_email = ? AND status IN "
        "('sent','follow_up','negotiation','quote_drafted') ORDER BY id DESC LIMIT 1",
        (frm,))


def _expired_lines(deal: dict) -> set:
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    expired = set()
    for it in (db.uj(deal.get("line_items"), []) or []):
        raw = it.get("quote_valid_until")
        if raw and str(raw) < now:
            key = it.get("mpn") or it.get("description")
            if key:
                expired.add(key)
    return expired


def handle_customer_po(gmail, email_row: dict, email_data: dict) -> list[str]:
    actions: list[str] = []
    deal = _find_deal_for_po(email_data)
    if not deal:
        db.update("emails", {"status": "escalated"}, "id = ?", (email_row["id"],))
        return ["po_no_deal_found_escalated"]
    log.info("PO linked to %s", deal["quote_number"])

    # PO acknowledgement to the customer (policy-gated draft).
    name = html_mod.escape(deal.get("customer_name") or "Customer")
    ack = (f"<p>Dear {name},</p>"
           f"<p>Thank you for your Purchase Order. We have received it and our team is "
           f"reviewing it now.</p>"
           f"<p>We will confirm the order details and delivery schedule shortly.</p>"
           f"<p>Best regards,<br>{html_mod.escape(config.COMPANY_ENTITY)} Sales Team</p>")
    drafts.dispatch(gmail, kind="po_ack", to=deal["customer_email"],
                    subject=email_data.get("subject") or "Your Purchase Order",
                    body_html=ack, cc=addrs(deal.get("customer_cc")),
                    thread_id=email_data.get("thread_id"), deal_id=deal["id"],
                    note="PO acknowledgement")
    actions.append("po_ack_dispatched")

    # Quote-validity guard: never commit a purchase at a lapsed cost.
    expired = _expired_lines(deal)
    if expired:
        items = [dict(it) for it in (db.uj(deal.get("line_items"), []) or [])]
        for it in items:
            if (it.get("mpn") or it.get("description")) in expired:
                it.update(unit_price=None, pricing_status="needs_review",
                          remark="Vendor quote expired — re-sourcing")
        db.update("deals", {"line_items": db.j(items), "status": "sourcing_vendors",
                            "updated_at": db.utcnow()}, "id = ?", (deal["id"],))
        actions.append(f"po_held_{len(expired)}_expired_resourcing")
        db.audit("po_held_expired", {"deal": deal["quote_number"],
                                     "expired": sorted(expired)})
        return actions

    # WON — the customer converted. Label the deal + move the flywheel BEFORE the
    # vendor legs (the label is the point; the POs are consequences).
    from ..learn import outcomes
    outcomes.close_deal(deal["id"], outcome="won",
                        clearing_price_usd=deal.get("we_quoted_usd"),
                        reason="customer PO received", actor="agent")
    actions.append("deal_won_labelled")

    placed = draft_vendor_pos(gmail, deal, customer_po_ref=email_data.get("subject"))
    actions.append(f"vendor_pos_{len(placed)}")
    db.update("deals", {"status": "po_received", "updated_at": db.utcnow()},
              "id = ?", (deal["id"],))
    db.update("emails", {"status": "processing"}, "id = ?", (email_row["id"],))
    return actions


# ============================================================
# Vendor POs
# ============================================================

def draft_vendor_pos(gmail, deal: dict, customer_po_ref: str | None = None) -> list[int]:
    """One PO per winning vendor (VendorQuote.is_selected), grouped, drafted."""
    selected = db.query("SELECT * FROM vendor_quotes WHERE deal_id = ? AND is_selected = 1",
                        (deal["id"],))
    if not selected:
        log.warning("[%s] no selected vendor quotes — no POs", deal["quote_number"])
        return []
    qty_by, desc_by = {}, {}
    for it in (db.uj(deal.get("line_items"), []) or []):
        key = (it.get("mpn") or "").strip().upper()
        qty_by[key] = it.get("quantity")
        desc_by[key] = it.get("description")

    groups: dict = {}
    for vq in selected:
        groups.setdefault(vq.get("vendor_email") or vq.get("vendor_name") or "?", []).append(vq)

    stamp = db.utcnow()[:10].replace("-", "")
    created: list[int] = []
    for seq, (vkey, vqs) in enumerate(groups.items(), 1):
        po_number = f"{config.QUOTE_PREFIX}-PO-{stamp}-{deal['id']:04d}-{seq}"
        currency = vqs[0].get("currency") or "USD"
        lines, total = [], 0.0
        for vq in vqs:
            key = (vq.get("mpn") or "").strip().upper()
            qty = qty_by.get(key) or 0
            cost = vq.get("cost_price") or 0
            total += cost * (qty or 0)
            lines.append({"mpn": vq.get("mpn"), "description": desc_by.get(key),
                          "quantity": qty, "unit_cost": cost,
                          "currency": vq.get("currency") or currency,
                          "lead_time": vq.get("lead_time")})
        body = _po_html(vqs[0].get("vendor_name"), po_number, lines, total, currency,
                        customer_po_ref)
        to = (vqs[0].get("vendor_email") or "").strip()
        vpo_id = db.insert("vendor_pos", {
            "deal_id": deal["id"], "vendor_email": to or None,
            "vendor_name": vqs[0].get("vendor_name"), "po_number": po_number,
            "customer_po_ref": customer_po_ref, "line_items": db.j(lines),
            "total_cost": round(total, 2), "currency": currency, "status": "drafted"})
        if to:
            res = drafts.dispatch(gmail, kind="vendor_po", to=to,
                                  subject=f"Purchase Order {po_number} - {config.COMPANY_ENTITY}",
                                  body_html=body, deal_id=deal["id"], vendor_po_id=vpo_id,
                                  note=f"{len(lines)} line(s), {currency} {round(total, 2)}")
            db.update("vendor_pos",
                      {"thread_id": res["thread_id"], "message_id": res["message_id"],
                       "status": "placed" if res["status"] == "auto_sent" else "drafted",
                       "placed_at": db.utcnow() if res["status"] == "auto_sent" else None},
                      "id = ?", (vpo_id,))
        created.append(vpo_id)
        log.info("[%s] vendor PO %s → %s (%s lines, %s %.2f)", deal["quote_number"],
                 po_number, vqs[0].get("vendor_name"), len(lines), currency, total)
    return created


def _po_html(vendor_name, po_number, lines, total, currency, customer_po_ref):
    esc = html_mod.escape
    rows = ""
    for ln in lines:
        line_total = (ln.get("unit_cost") or 0) * (ln.get("quantity") or 0)
        rows += (f'<tr><td style="{_CELL}"><strong>{esc(str(ln.get("mpn") or "-"))}</strong></td>'
                 f'<td style="{_CELL}">{esc(str(ln.get("description") or "-"))}</td>'
                 f'<td style="{_CELL}text-align:right;">{esc(str(ln.get("quantity") or "-"))}</td>'
                 f'<td style="{_CELL}text-align:right;">{ln.get("currency") or currency} '
                 f'{ln.get("unit_cost")}</td>'
                 f'<td style="{_CELL}text-align:right;">{currency} {round(line_total, 2)}</td>'
                 f'<td style="{_CELL}">{esc(str(ln.get("lead_time") or "-"))}</td></tr>')
    ref = (f'<br><strong>Ref (customer PO):</strong> {esc(customer_po_ref)}'
           if customer_po_ref else "")
    return f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;">
    <p><strong>PURCHASE ORDER</strong> &mdash; {esc(config.COMPANY_ENTITY)}</p>
    <p><strong>PO No:</strong> {esc(po_number)}{ref}</p>
    <p>Dear {esc(vendor_name or 'Supplier')},<br>
    Please supply the following against this Purchase Order:</p>
    <table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">
    <tr style="background:#0d47a1;color:#ffffff;">
    <th style="{_TH}">MPN</th><th style="{_TH}">Description</th><th style="{_TH}">Qty</th>
    <th style="{_TH}">Unit Cost</th><th style="{_TH}">Line Total</th><th style="{_TH}">Lead Time</th></tr>
    {rows}</table>
    <p style="margin-top:10px;"><strong>Order total: {currency} {round(total, 2)}</strong></p>
    <p style="margin-top:14px;padding:12px;background:#e8f5e9;border-left:4px solid #2e7d32;">
    Please <strong>confirm acceptance</strong>, unit prices, lead time and dispatch schedule by
    return. Advise date code, packaging and country of origin, and &mdash; for traceability
    &mdash; share a <strong>photo of the stock label</strong> (date code + packing) and the
    manufacturer <strong>CoC / authorised-distributor packing list</strong>.
    Reply on this same thread.</p>
    <p>Best regards,<br>{esc(config.COMPANY_ENTITY)} &mdash; Purchase</p>
    </div>"""


# ============================================================
# Vendor PO replies
# ============================================================

_STATUS_MAP = {
    "confirmed": "confirmed", "accepted": "confirmed", "acknowledged": "confirmed",
    "dispatched": "shipped", "shipped": "shipped",
    "delivered": "received", "received": "received",
    "declined": "cancelled", "rejected": "cancelled", "no_stock": "cancelled",
    "cancelled": "cancelled",
}


def handle_vendor_po_reply(gmail, vpo: dict, email_data: dict) -> list[str]:
    body = bom.best_body(email_data.get("body_text", ""), email_data.get("body_html", ""),
                         min_len=40)
    verdict = brain.classify_po_response(email_data.get("subject", ""), body[:3000],
                                         vpo.get("po_number") or "")
    raw = verdict["status"]
    new_status = _STATUS_MAP.get(raw)
    actions: list[str] = []

    if new_status:
        db.update("vendor_pos", {"status": new_status, "updated_at": db.utcnow()},
                  "id = ?", (vpo["id"],))
        actions.append(f"vendor_po_{new_status}")
        log.info("[%s] vendor %s replied → PO '%s' (%s)", vpo.get("po_number"),
                 vpo.get("vendor_name"), new_status, verdict.get("reason"))
    else:
        actions.append("vendor_po_reply_unclear")

    if new_status == "confirmed":
        # Open the logistics leg the moment the vendor accepts.
        if not db.query_one("SELECT id FROM shipments WHERE vendor_po_id = ?", (vpo["id"],)):
            db.insert("shipments", {"vendor_po_id": vpo["id"], "deal_id": vpo.get("deal_id"),
                                    "status": "awaiting_dispatch"})
            actions.append("shipment_opened")
    elif new_status in ("shipped", "received"):
        from . import logistics
        ship = db.query_one("SELECT * FROM shipments WHERE vendor_po_id = ?", (vpo["id"],))
        if not ship:
            sid = db.insert("shipments", {"vendor_po_id": vpo["id"],
                                          "deal_id": vpo.get("deal_id"),
                                          "status": "awaiting_dispatch"})
            ship = db.query_one("SELECT * FROM shipments WHERE id = ?", (sid,))
        info = brain.extract_shipment_info(email_data.get("subject", ""), body)
        logistics.advance_shipment(gmail, ship, info,
                                   fallback_event="dispatched" if new_status == "shipped"
                                   else "delivered")
        actions.append("shipment_advanced")

    if new_status == "cancelled" or not new_status:
        # Supplier can't fulfil (or is ambiguous) → a human re-sources. A replacement
        # PO is never auto-placed.
        if vpo.get("deal_id"):
            db.update("deals", {"status": "sourcing_vendors", "updated_at": db.utcnow()},
                      "id = ?", (vpo["deal_id"],))
        db.audit("vendor_po_declined" if new_status == "cancelled" else "vendor_po_unclear",
                 {"po": vpo.get("po_number"), "vendor": vpo.get("vendor_name"),
                  "reason": verdict.get("reason")})
        actions.append("flagged_for_human_resource")
    return actions
