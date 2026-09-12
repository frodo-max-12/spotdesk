"""Quoting — build the customer quote deterministically, gate it, draft it.

The quote table is CODE-BUILT (prices, totals, pending/no-bid markers, highlights) —
the LLM never touches a number the customer sees. The confidence gate then decides
what the draft's note says (and, only if policy allows auto-send, whether it may go
out untouched). In drafts-first operation every quote lands in Gmail Drafts threaded
under the customer's inquiry, with hold reasons listed for the reviewer.

When pricing changes (re-source, negotiation, revision), the old draft is SUPERSEDED —
deleted from Gmail and replaced — so the Drafts folder always shows the current answer.
"""

from __future__ import annotations

import html
import logging

from .. import config, db
from ..hitl import drafts
from ..knowledge import compliance
from ..ops import breakers
from .pricing import is_internal_remark
from .sourcing import addrs

log = logging.getLogger("spotdesk.quoting")

_CELL = 'padding:6px;border:1px solid #dddddd;'
_TH = 'padding:6px;border:1px solid #dddddd;text-align:left;'


def build_quote_html(deal: dict, line_items: list[dict], is_update: bool = False) -> str:
    """Render the customer quotation body. Priced rows show unit price + line total;
    pending rows show 'Quotation to follow shortly'; no-bid rows ask to re-confirm the
    MPN; newly-priced rows highlight green 'New'; revised quotes show the progression
    Last Quoted → Your Target → Revised Price."""
    esc = html.escape
    currency = (deal.get("currency") or "USD").upper()
    cust = esc(deal.get("customer_name") or "Customer")
    validity_days = deal.get("validity_days") or config.QUOTE_VALIDITY_DAYS
    is_revision = any(it.get("prev_unit_price") is not None for it in line_items)
    tag_label = "Revised" if is_revision else "New"

    def money(v):
        try:
            return f"{currency} {float(v):,.2f}"
        except (TypeError, ValueError):
            return f"{currency} {esc(str(v))}"

    rows = ""
    grand = 0.0
    has_pending = has_new = has_no_bid = has_eud = False
    for it in line_items:
        if not (it.get("mpn") or it.get("description")):
            continue
        mpn = esc(str(it.get("mpn") or "-"))
        desc = esc(str(it.get("description") or "-"))
        make = esc(str(it.get("manufacturer") or "-"))
        qty = it.get("quantity")
        qty_disp = esc(str(qty)) if qty not in (None, "") else "-"
        spq_disp = esc(str(it.get("spq"))) if it.get("spq") not in (None, "") else "-"
        moq_disp = esc(str(it.get("moq"))) if it.get("moq") not in (None, "") else "-"
        dc_disp = esc(str(it.get("date_code"))) if it.get("date_code") not in (None, "") else "-"
        lead = esc(str(it.get("lead_time") or "-"))
        remark_txt = it.get("remark") or it.get("pricing_notes")
        if is_internal_remark(remark_txt):
            remark_txt = ""              # defence-in-depth: internal wording never ships
        note = esc(str(remark_txt)) if remark_txt else ""

        if is_revision:
            pv, tpc = it.get("prev_unit_price"), it.get("customer_target_price")
            extra_cells = (
                f'<td style="{_CELL}text-align:right;color:#777;">{money(pv) if pv is not None else "-"}</td>'
                f'<td style="{_CELL}text-align:right;color:#777;">{money(tpc) if tpc is not None else "-"}</td>')
        else:
            extra_cells = ""

        up = it.get("unit_price")
        status = (it.get("pricing_status") or "").lower()
        newly = bool(it.get("newly_priced"))
        if up is not None and status == "priced":
            try:
                grand += float(up) * float(qty)
            except (TypeError, ValueError):
                pass
            price_cell = money(up)
            if newly:
                has_new = True
                row_style = ' style="background:#e8f7e9;"'
                remark_cell = (f'<span style="background:#2e7d32;color:#ffffff;font-size:11px;'
                               f'padding:2px 6px;border-radius:3px;">{tag_label}</span>'
                               + ((" " + note) if note else ""))
            else:
                row_style = ""
                remark_cell = note or "-"
        elif up is not None:            # priced but held (needs_review) — show, flagged
            try:
                grand += float(up) * float(qty)
            except (TypeError, ValueError):
                pass
            price_cell = money(up)
            row_style = ' style="background:#fff8e1;"'
            remark_cell = note or "-"
        elif status == "no_bid":
            has_no_bid = True
            row_style = ' style="background:#fdecea;"'
            price_cell = '<em style="color:#c62828;">No Bid</em>'
            spq_disp = moq_disp = lead = "-"
            remark_cell = note or '<em style="color:#c62828;">No bid — please verify the part number</em>'
        else:
            has_pending = True
            row_style = ' style="background:#fff8e1;"'
            price_cell = '<em style="color:#b26a00;">Pending</em>'
            spq_disp = moq_disp = lead = "-"
            remark_cell = note or '<em style="color:#b26a00;">Quotation to follow shortly</em>'

        if it.get("eud_required"):
            has_eud = True
            base = "" if remark_cell in ("-", "") else (remark_cell + " ")
            remark_cell = base + ('<span style="background:#8e24aa;color:#ffffff;font-size:10px;'
                                  'padding:1px 5px;border-radius:3px;">EUD</span>')

        rows += (f'<tr{row_style}><td style="{_CELL}"><strong>{mpn}</strong></td>'
                 f'<td style="{_CELL}">{desc}</td><td style="{_CELL}">{make}</td>'
                 f'<td style="{_CELL}text-align:right;">{qty_disp}</td>{extra_cells}'
                 f'<td style="{_CELL}text-align:right;">{price_cell}</td>'
                 f'<td style="{_CELL}text-align:right;">{spq_disp}</td>'
                 f'<td style="{_CELL}text-align:right;">{moq_disp}</td>'
                 f'<td style="{_CELL}">{lead}</td><td style="{_CELL}">{dc_disp}</td>'
                 f'<td style="{_CELL}">{remark_cell}</td></tr>')

    extra_headers = (f'<th style="{_TH}">Last Quoted ({currency})</th>'
                     f'<th style="{_TH}">Your Target ({currency})</th>') if is_revision else ""
    price_header = f"Revised Price ({currency})" if is_revision else f"Price ({currency})"
    header = (f'<tr style="background:#0d47a1;color:#ffffff;">'
              f'<th style="{_TH}">MPN</th><th style="{_TH}">Description</th>'
              f'<th style="{_TH}">Make</th><th style="{_TH}">Quantity</th>{extra_headers}'
              f'<th style="{_TH}">{price_header}</th><th style="{_TH}">SPQ</th>'
              f'<th style="{_TH}">MOQ</th><th style="{_TH}">Lead Time</th>'
              f'<th style="{_TH}">D/C</th><th style="{_TH}">Remark</th></tr>')

    if is_revision:
        update_note = ('<p style="padding:8px 12px;background:#e8f7e9;border-left:4px solid #2e7d32;">'
                       'This is a <strong>revised quotation</strong> against the target prices you '
                       'shared. For each line you can see your <strong>Last Quoted</strong> price, '
                       '<strong>Your Target</strong>, and our <strong>Revised Price</strong>.</p>')
    elif is_update and has_new:
        update_note = ('<p style="padding:8px 12px;background:#e8f7e9;border-left:4px solid #2e7d32;">'
                       'This is an <strong>updated quotation</strong>. Items newly quoted since the '
                       'previous version are highlighted in green and tagged <strong>New</strong>.</p>')
    else:
        update_note = ""

    total_note = (f'<p style="margin-top:12px;"><strong>Estimated total'
                  f'{" (quoted items only)" if has_pending else ""}: {money(grand)}</strong>'
                  f'{" — excludes items still pending" if has_pending else ""}</p>')
    pending_note = ('<p style="padding:8px 12px;background:#fff8e1;border-left:4px solid #f57c00;">'
                    'Pricing for the item(s) marked <em style="color:#b26a00;">Pending</em> is '
                    'being finalised and will follow shortly.</p>') if has_pending else ""
    no_bid_note = ('<p style="padding:8px 12px;background:#fdecea;border-left:4px solid #c62828;">'
                   'The item(s) marked <em style="color:#c62828;">No Bid</em> could not be sourced '
                   'against the part number supplied — please re-confirm the exact '
                   'MPN/manufacturer so we can quote them.</p>') if has_no_bid else ""
    eud_note = ('<p style="padding:8px 12px;background:#f3e5f5;border-left:4px solid #8e24aa;">'
                'Item(s) tagged <span style="background:#8e24aa;color:#ffffff;font-size:10px;'
                'padding:1px 5px;border-radius:3px;">EUD</span> may be controlled (dual-use) and '
                'will require a signed <strong>End-User Declaration</strong> before dispatch.</p>') \
        if has_eud else ""

    entity = html.escape(config.COMPANY_ENTITY)
    return (f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;">'
            f'<p>Dear {cust},</p>'
            f'<p>Thank you for your enquiry. Please find our quotation below from '
            f'<strong>{entity}</strong> (Ref: {html.escape(deal.get("quote_number") or "")}).</p>'
            f'{update_note}'
            f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;'
            f'font-family:Arial,sans-serif;font-size:13px;">{header}{rows}</table>'
            f'{total_note}{pending_note}{no_bid_note}{eud_note}'
            f'<p style="margin-top:12px;padding:10px 12px;background:#f5f7fa;'
            f'border-left:4px solid #0d47a1;font-size:13px;color:#333;">'
            f'<strong>Terms:</strong> Prices in {currency}, per piece, ex-tax &middot; '
            f'<strong>Ex-Works Singapore</strong> &middot; payment <strong>T/T</strong> &middot; '
            f'goods <strong>100% new &amp; original</strong> (RoHS / Pb-free) &middot; '
            f'lead time includes inbound transit &middot; quotation valid for '
            f'<strong>{validity_days} days</strong>. Datasheets and stock-label photos '
            f'(date code + packing) available on request.</p>'
            f'<p>We look forward to your confirmation.</p>'
            f'<p>Best regards,<br>{entity} Sales Team</p></div>')


# ============================================================
# The confidence gate → draft (or gated auto-send)
# ============================================================

def gate_reasons(deal: dict, line_items: list[dict]) -> list[str]:
    """Every reason this quote needs a human's eye. Empty list = fully confident."""
    reasons: list[str] = []
    quotable = [it for it in line_items if it.get("mpn") or it.get("description")]
    priced = [it for it in quotable
              if it.get("unit_price") is not None and it.get("pricing_status") == "priced"]
    unpriced = [it for it in quotable if it not in priced]
    if unpriced:
        reasons.append(f"{len(unpriced)} line(s) not confidently priced")
    if (deal.get("currency") or "USD").upper() != "USD":
        reasons.append(f"non-USD currency ({deal.get('currency')}) — auto-pricing is USD only")
    if deal.get("currency_assumed"):
        reasons.append("currency was assumed, not stated by the customer")
    eud = [it for it in quotable if it.get("eud_required")]
    if eud:
        reasons.append(f"{len(eud)} line(s) require an End-User Declaration (dual-use)")
    auth = [it for it in quotable if it.get("authenticity_review")]
    if auth:
        reasons.append(f"{len(auth)} line(s) priced far below market — verify authenticity "
                       f"(CoC / stock-label) before sending")
    thin = [it for it in priced if it.get("margin_percent") is not None
            and it.get("margin_percent") < config.MIN_MARGIN_PERCENT]
    if thin:
        reasons.append(f"{len(thin)} line(s) below the {config.MIN_MARGIN_PERCENT:.0f}% margin floor")
    total = sum((it.get("unit_price") or 0) * (it.get("quantity") or 0) for it in priced)
    if config.AUTO_SEND_MAX_VALUE and total > config.AUTO_SEND_MAX_VALUE:
        reasons.append(f"quote value {total:,.0f} exceeds the auto-send ceiling "
                       f"{config.AUTO_SEND_MAX_VALUE:,.0f}")
    screen = compliance.screen_counterparty(deal.get("customer_email") or "",
                                            deal.get("customer_company"))
    if not screen.cleared:
        reasons.append("compliance: " + "; ".join(screen.findings))
    exp_ok, exp_reason = breakers.check_capital_exposure(deal.get("customer_email") or "", total)
    if not exp_ok:
        reasons.append(exp_reason)
    return reasons


def draft_quote(gmail, deal: dict, *, is_update: bool = False) -> dict:
    """Build the quote body, run the gate, and put the CURRENT answer in front of the
    human — superseding any older quote draft on this deal. Auto-send happens only if
    the gate is clean AND policy/mode allow (drafts.dispatch enforces that)."""
    line_items = db.uj(deal.get("line_items"), []) or []
    body = build_quote_html(deal, line_items, is_update=is_update)
    reasons = gate_reasons(deal, line_items)

    email_row = db.query_one("SELECT thread_id, subject FROM emails WHERE id = ?",
                             (deal.get("email_id"),)) or {}
    # Customer subject must NOT carry the internal quote number — customers confuse
    # it with a part number. Reply on their own subject/thread.
    subject = email_row.get("subject") or "Quotation"
    note = ("HOLD: " + "; ".join(reasons)) if reasons else "gate clean"

    db.update("deals", {"draft_email_body": body,
                        "we_quoted_usd": sum((it.get("unit_price") or 0) * (it.get("quantity") or 0)
                                             for it in line_items if it.get("unit_price")),
                        "updated_at": db.utcnow()},
              "id = ?", (deal["id"],))

    res = drafts.supersede_open_drafts(
        gmail, deal_id=deal["id"], kind="quote",
        to=deal.get("customer_email"), subject=subject, body_html=body,
        cc=addrs(deal.get("customer_cc")), thread_id=email_row.get("thread_id"),
        note=note,
        force_draft=bool(reasons),      # a held quote may NEVER auto-send
    )
    new_status = "sent" if res["status"] == "auto_sent" else "quote_drafted"
    updates = {"status": new_status, "updated_at": db.utcnow()}
    if res["status"] == "auto_sent":
        updates["sent_at"] = db.utcnow()
    db.update("deals", updates, "id = ?", (deal["id"],))
    db.audit("quote_" + res["status"],
             {"deal": deal.get("quote_number"), "hold_reasons": reasons})
    log.info("[%s] quote %s%s", deal.get("quote_number"), res["status"],
             (" — HOLD: " + "; ".join(reasons)) if reasons else "")
    return {"status": res["status"], "hold_reasons": reasons, "action_id": res["action_id"]}
