"""Negotiation — react to a customer's target prices the way a disciplined buyer would.

Per line with a target:
  * If trimming margin down to (never below) the negotiation floor meets it, quote AT
    the target — we absorb it; no need to trouble the vendor.
  * Otherwise the vendor cost is the problem: ask the vendor to sharpen THEIR OWN
    cost by a sensible, CAPPED amount (never a customer lowball passed through
    verbatim — one round's ask is bounded by MAX_VENDOR_ASK_PERCENT of their last
    cost). When the improved cost lands, consolidation flexes our margin within
    [floor, normal] to give our best price.

Playbook rules enforced in code: the customer's target price is NEVER shared with any
vendor; the customer gets a warm hold ("checking with our sources") on the same
thread instead of silence while we re-source.
"""

from __future__ import annotations

import html as html_mod
import logging
import re

from .. import config, db
from ..hitl import drafts
from ..llm import brain
from . import bom, quoting, sourcing
from .pricing import to_float
from .sourcing import addrs

log = logging.getLogger("spotdesk.negotiation")


def _find_deal(email_data: dict) -> dict | None:
    if email_data.get("thread_id"):
        row = db.query_one(
            "SELECT d.* FROM deals d JOIN emails e ON d.email_id = e.id "
            "WHERE e.thread_id = ? ORDER BY d.id DESC LIMIT 1", (email_data["thread_id"],))
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
        "('sent','follow_up','quote_drafted','negotiation') ORDER BY id DESC LIMIT 1", (frm,))


def handle_negotiation(gmail, email_row: dict, email_data: dict) -> list[str]:
    actions: list[str] = []
    deal = _find_deal(email_data)
    if not deal:
        db.update("emails", {"status": "escalated"}, "id = ?", (email_row["id"],))
        return ["negotiation_no_deal_found_escalated"]

    known = [it.get("mpn") for it in (db.uj(deal.get("line_items"), []) or [])
             if it.get("mpn")]
    body = bom.best_body(email_data.get("body_text", ""), email_data.get("body_html", ""))
    targets = brain.extract_target_prices(email_data.get("subject", ""), body, known,
                                          deal.get("currency") or "USD")

    new_round = (deal.get("negotiation_round") or 0) + 1
    db.update("deals", {"negotiation_round": new_round, "status": "negotiation",
                        "updated_at": db.utcnow()}, "id = ?", (deal["id"],))
    round_id = db.insert("negotiation_rounds", {
        "deal_id": deal["id"], "round_number": new_round,
        "customer_email_id": email_row["id"],
        "customer_target_prices": db.j(targets.get("items", []))})
    actions.append(f"round_{new_round}_on_{deal['quote_number']}")

    floor = config.NEGOTIATION_MIN_MARGIN_PERCENT
    tp_map = {}
    for t in targets.get("items", []):
        k = (t.get("mpn") or "").strip().upper()
        tp = to_float(t.get("target_price"))
        if k and tp:
            tp_map[k] = tp

    items = [dict(it) for it in (db.uj(deal.get("line_items"), []) or [])]
    met = 0
    resource_parts: list[dict] = []
    response_log: list[dict] = []
    for it in items:
        tp = tp_map.get((it.get("mpn") or "").strip().upper())
        if tp is None:
            continue
        landed = it.get("landed_cost") or it.get("cost_price")
        if not landed or landed <= 0:
            continue
        implied_margin = (tp / landed - 1.0) * 100.0
        prev_price = it.get("unit_price")
        if implied_margin >= floor:
            # Meet the target by trimming margin. The remark is CUSTOMER-FACING —
            # margin/cost detail goes to internal_note only.
            it.update(margin_percent=round(implied_margin, 2), unit_price=round(tp, 4),
                      newly_priced=True, negotiated=True, pricing_status="priced",
                      prev_unit_price=prev_price, customer_target_price=round(tp, 4),
                      remark="Revised price",
                      internal_note=f"met target by margin trim ({implied_margin:.0f}%)")
            met += 1
            response_log.append({"mpn": it.get("mpn"), "action": "margin_trim",
                                 "new_margin": round(implied_margin, 2)})
        else:
            # Vendor must come down. Gauge the push, ask for the cost that keeps our
            # NORMAL margin at their target — capped so a blind lowball is never
            # demanded verbatim; margin flex absorbs the rest.
            base_cost = it.get("cost_price") or landed
            default_m = config.DEFAULT_MARGIN_PERCENT
            ideal_cost = (tp / (1.0 + default_m / 100.0)) * (base_cost / landed)
            needed_pct = (base_cost - ideal_cost) / base_cost * 100.0 if base_cost else 0.0
            ask_pct = round(max(1.0, min(needed_pct, config.MAX_VENDOR_ASK_PERCENT)), 1)
            target_cost = round(base_cost * (1.0 - ask_pct / 100.0), 4)
            resource_parts.append({"mpn": it.get("mpn"), "description": it.get("description"),
                                   "manufacturer": it.get("manufacturer"),
                                   "quantity": it.get("quantity"),
                                   "_target_cost": target_cost, "_ask_pct": ask_pct,
                                   "_prev_vendor_cost": round(base_cost, 4)})
            it.update(unit_price=None, newly_priced=False, negotiated=False,
                      pricing_status="needs_review",
                      prev_unit_price=prev_price, customer_target_price=round(tp, 4),
                      remark="",
                      internal_note=f"asked vendor for ~{ask_pct:.0f}% off "
                                    f"(target cost USD {target_cost})")
            response_log.append({"mpn": it.get("mpn"), "action": "re_source",
                                 "ask_pct": ask_pct})

    db.update("deals", {"line_items": db.j(items), "updated_at": db.utcnow()},
              "id = ?", (deal["id"],))
    db.update("negotiation_rounds", {"our_response": db.j(response_log),
                                     "forwarded_at": db.utcnow()}, "id = ?", (round_id,))

    if resource_parts:
        deal = db.query_one("SELECT * FROM deals WHERE id = ?", (deal["id"],))
        touched = sourcing.redraft_target_cost(gmail, deal, resource_parts)
        db.update("deals", {"status": "sourcing_vendors", "updated_at": db.utcnow()},
                  "id = ?", (deal["id"],))
        actions.append(f"re_asked_{len(touched)}_vendors_capped")
        # Warm hold to the customer on the SAME thread — never silence while re-sourcing.
        ack = _target_ack_html(deal, targets.get("items", []))
        drafts.dispatch(gmail, kind="negotiation_reply", to=deal["customer_email"],
                        subject=email_data.get("subject") or "Your target price",
                        body_html=ack, cc=addrs(deal.get("customer_cc")),
                        thread_id=email_data.get("thread_id"), deal_id=deal["id"],
                        note=f"target-price ack (round {new_round})")
        actions.append("target_ack_dispatched")
    elif met:
        deal = db.query_one("SELECT * FROM deals WHERE id = ?", (deal["id"],))
        res = quoting.draft_quote(gmail, deal, is_update=True)
        db.update("negotiation_rounds", {"revised_quote_at": db.utcnow()},
                  "id = ?", (round_id,))
        actions.append(f"revised_quote_{res['status']}_met_{met}_by_margin_trim")
    else:
        actions.append("no_targets_matched_escalated")
        db.update("emails", {"status": "escalated"}, "id = ?", (email_row["id"],))
    return actions


def _target_ack_html(deal: dict, target_items: list[dict]) -> str:
    esc = html_mod.escape
    cust = esc(str(deal.get("customer_name") or "there").split()[0]
               if deal.get("customer_name") else "there")
    currency = deal.get("currency") or "USD"
    rows = ""
    for t in target_items or []:
        tp = t.get("target_price")
        if not (t.get("mpn") and tp not in (None, "")):
            continue
        cell = 'padding:6px;border:1px solid #dddddd;'
        rows += (f'<tr><td style="{cell}"><strong>{esc(str(t.get("mpn")))}</strong></td>'
                 f'<td style="{cell}text-align:right;">{esc(str(currency))} '
                 f'{esc(str(tp))}</td></tr>')
    table = ""
    if rows:
        th = 'padding:6px;border:1px solid #dddddd;text-align:left;'
        table = ('<table cellspacing="0" cellpadding="0" '
                 'style="border-collapse:collapse;margin:10px 0;">'
                 f'<tr style="background:#0d47a1;color:#ffffff;"><th style="{th}">MPN</th>'
                 f'<th style="{th}">Your target</th></tr>' + rows + "</table>")
    return (f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;line-height:1.5;">'
            f'<p>Dear {cust},</p>'
            f'<p>Thank you for sharing your target price. We have noted the following and are '
            f'<strong>checking with our sources to match it</strong>:</p>{table}'
            f'<p>We will revert with a <strong>revised quotation</strong> shortly. We appreciate '
            f'your business and will do our best to meet your target.</p>'
            f'<p>Best regards,<br>{esc(config.COMPANY_ENTITY)} Sales Team</p></div>')
