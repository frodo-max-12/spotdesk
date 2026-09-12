"""Logistics — the coordination leg after the buy.

State machine per vendor PO:
    awaiting_dispatch → dispatched → in_transit → customs → delivered
                                          ↘ exception (needs a human)

Driven entirely by reading mail: vendor dispatch notes, courier notifications, and
forwarder updates are classified `logistics_update` at intake and land here. Each
advance extracts the tracking facts (carrier / AWB / ETA), updates the shipment row,
and puts a customer-facing shipping update in Drafts. When every shipment on a deal
is delivered, the deal closes its loop (status → delivered).
"""

from __future__ import annotations

import html as html_mod
import logging

from .. import config, db
from ..hitl import drafts
from ..llm import brain
from . import bom
from .sourcing import addrs

log = logging.getLogger("spotdesk.logistics")

_EVENT_TO_STATUS = {
    "dispatched": "dispatched",
    "in_transit": "in_transit",
    "customs": "customs",
    "delivered": "delivered",
    "exception": "exception",
}
_ORDER = ["awaiting_dispatch", "dispatched", "in_transit", "customs", "delivered"]


def handle_logistics_update(gmail, email_row: dict, email_data: dict) -> list[str]:
    """An inbound mail classified logistics_update: find which shipment it belongs to
    (thread → PO number → AWB), extract facts, advance the state machine."""
    body = bom.best_body(email_data.get("body_text", ""), email_data.get("body_html", ""),
                         min_len=30)
    info = brain.extract_shipment_info(email_data.get("subject", ""), body)

    ship = None
    if email_data.get("thread_id"):
        vpo = db.query_one("SELECT id FROM vendor_pos WHERE thread_id = ?",
                           (email_data["thread_id"],))
        if vpo:
            ship = db.query_one("SELECT * FROM shipments WHERE vendor_po_id = ?",
                                (vpo["id"],))
    if not ship and info.get("awb"):
        ship = db.query_one("SELECT * FROM shipments WHERE awb = ?", (info["awb"],))
    if not ship:
        db.update("emails", {"status": "escalated"}, "id = ?", (email_row["id"],))
        db.audit("logistics_update_unmatched", {"subject": email_data.get("subject"),
                                                "info": info})
        return ["logistics_update_unmatched_escalated"]

    advance_shipment(gmail, ship, info)
    db.update("emails", {"status": "done"}, "id = ?", (email_row["id"],))
    return [f"shipment_{ship['id']}_advanced"]


def advance_shipment(gmail, ship: dict, info: dict, fallback_event: str | None = None) -> None:
    event = (info.get("event") or "").strip().lower()
    new_status = _EVENT_TO_STATUS.get(event) or _EVENT_TO_STATUS.get(fallback_event or "")
    updates = {"updated_at": db.utcnow(),
               "last_event": info.get("notes") or event or fallback_event,
               "last_event_at": db.utcnow()}
    for field in ("carrier", "awb", "incoterm", "origin", "destination", "eta"):
        if info.get(field):
            updates[field] = info[field]
    # Never regress the state machine off a late-arriving older email.
    if new_status:
        cur_i = _ORDER.index(ship["status"]) if ship["status"] in _ORDER else 0
        new_i = _ORDER.index(new_status) if new_status in _ORDER else None
        if new_status == "exception" or (new_i is not None and new_i > cur_i):
            updates["status"] = new_status
    db.update("shipments", updates, "id = ?", (ship["id"],))
    db.audit("shipment_update", {"shipment_id": ship["id"], **{k: v for k, v in
                                                               updates.items()
                                                               if k != "updated_at"}})
    effective = updates.get("status") or ship["status"]
    log.info("shipment %s → %s (awb %s)", ship["id"], effective,
             updates.get("awb") or ship.get("awb"))
    # Mirror onto the deal so the pipeline board shows the coordination leg.
    if ship.get("deal_id") and updates.get("status") in ("dispatched", "in_transit",
                                                          "customs"):
        db.update("deals", {"status": "in_logistics", "updated_at": db.utcnow()},
                  "id = ? AND status IN ('po_placed','po_received','in_logistics')",
                  (ship["deal_id"],))

    # Customer-facing shipping update lands in Drafts on dispatch / delivery.
    if updates.get("status") in ("dispatched", "delivered"):
        _draft_customer_update(gmail, ship, updates.get("status"),
                               updates.get("awb") or ship.get("awb"),
                               updates.get("carrier") or ship.get("carrier"),
                               updates.get("eta") or ship.get("eta"))
    if updates.get("status") == "delivered":
        _maybe_close_deal(ship)


def _draft_customer_update(gmail, ship: dict, status: str, awb, carrier, eta) -> None:
    deal = db.query_one("SELECT * FROM deals WHERE id = ?", (ship.get("deal_id"),))
    if not deal or not deal.get("customer_email"):
        return
    email_row = db.query_one("SELECT thread_id, subject FROM emails WHERE id = ?",
                             (deal.get("email_id"),)) or {}
    esc = html_mod.escape
    name = esc(deal.get("customer_name") or "Customer")
    if status == "dispatched":
        headline = "your order has been dispatched"
        detail = " ".join(x for x in [
            f"Carrier: <strong>{esc(str(carrier))}</strong>." if carrier else "",
            f"Tracking / AWB: <strong>{esc(str(awb))}</strong>." if awb else "",
            f"Expected arrival: <strong>{esc(str(eta))}</strong>." if eta else ""]).strip()
    else:
        headline = "your order has been delivered"
        detail = "Please confirm receipt at your convenience."
    body = (f"<p>Dear {name},</p>"
            f"<p>A quick update on your order (Ref: {esc(deal.get('quote_number') or '')}) — "
            f"{headline}.</p>" + (f"<p>{detail}</p>" if detail else "") +
            f"<p>Please feel free to reach out with any questions.</p>"
            f"<p>Best regards,<br>{esc(config.COMPANY_ENTITY)} Sales Team</p>")
    drafts.dispatch(gmail, kind="logistics_update", to=deal["customer_email"],
                    subject=email_row.get("subject") or f"Order update — {deal.get('quote_number')}",
                    body_html=body, cc=addrs(deal.get("customer_cc")),
                    thread_id=email_row.get("thread_id"), deal_id=deal["id"],
                    note=f"shipping update: {status}")


def _maybe_close_deal(ship: dict) -> None:
    deal_id = ship.get("deal_id")
    if not deal_id:
        return
    open_ships = db.query_one(
        "SELECT COUNT(*) AS n FROM shipments WHERE deal_id = ? AND status != 'delivered'",
        (deal_id,))
    if (open_ships or {}).get("n", 0) == 0:
        db.update("deals", {"status": "delivered", "updated_at": db.utcnow()},
                  "id = ?", (deal_id,))
        db.audit("deal_delivered", {"deal_id": deal_id})
