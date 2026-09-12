"""Sourcing — the Purchase role: route parts to brokers, draft RFQs, read replies.

Routing = brand-matched vendors first (normalized via aliases), plus every open-market
broker (brands=["ANY"]), ranked by manual priority → learned scorecard → trust, capped
per part. Market-intel "approach_first" vendors (from recent offer sheets) jump the
queue. The inquiring customer's own domain is NEVER RFQ'd for their own part — in this
market the same company is often on both sides.

Vendor RFQs are born as Gmail DRAFTS (policy-gated); the operator reviews and releases
them in one click. Confidentiality: a vendor email never reveals the end customer —
only the internal quote number.
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from datetime import datetime, timezone

from .. import config, db
from ..hitl import drafts
from ..knowledge.line_card import brand_match
from ..llm import brain
from . import bom, pricing

log = logging.getLogger("spotdesk.sourcing")

_EMAIL_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_SYSTEM_LOCALPARTS = {"mailer-daemon", "mailerdaemon", "postmaster", "no-reply", "noreply",
                      "no_reply", "do-not-reply", "donotreply", "bounce", "bounces"}

_DECLINE_MARKERS = ("no bid", "no-bid", "not authorised", "not authorized", "no stock",
                    "cannot quote", "can't quote", "unable to quote", "no offer",
                    "we decline", "not able to", "no carry", "under allocation")


def addrs(raw) -> list[str]:
    """All addresses in a raw header string, lowercased, de-duped, order kept."""
    out: list[str] = []
    for a in _EMAIL_ADDR_RE.findall(str(raw or "")):
        a = a.lower()
        if a not in out:
            out.append(a)
    return out


def is_system_addr(a: str) -> bool:
    if not a or "@" not in a:
        return True
    return a.split("@", 1)[0] in _SYSTEM_LOCALPARTS


def _domain(e: str | None) -> str:
    return (e or "").lower().split("@")[-1].replace(">", "").strip()


# ============================================================
# Routing
# ============================================================

def _effective_priority(v: dict) -> int:
    if v.get("priority") is not None:
        return v["priority"]
    if v.get("dynamic_priority") is not None:
        return v["dynamic_priority"]
    return 3


def _rank_key(v: dict):
    # manual/learned priority first; trust and score break ties.
    return (_effective_priority(v), -(v.get("score") or 0), -(v.get("trust_score") or 50))


def route(line_items: list[dict], exclude_customer_email: str = "") -> tuple[dict, list]:
    """Map parts → vendors. Returns ({vendor_email: (vendor_row, [parts])}, unsourced).
    approach_first vendors attached to a line (from market intel) rank ahead of the
    general panel for that line."""
    vendors = db.query(
        "SELECT * FROM counterparties WHERE kind IN ('vendor','both') "
        "AND active = 1 AND blacklisted = 0")
    cust_dom = _domain(exclude_customer_email)
    if cust_dom:
        kept = [v for v in vendors
                if not any(_domain(a) == cust_dom for a in addrs(v.get("all_emails") or v["email"]))
                and _domain(v["email"]) != cust_dom]
        if len(kept) != len(vendors):
            log.info("route: excluding vendor(s) on the customer's own domain (%s)", cust_dom)
        vendors = kept

    def _brands(v) -> list[str]:
        return [str(b).upper() for b in (db.uj(v.get("brands"), []) or [])]

    brokers = [v for v in vendors if "ANY" in _brands(v) or "OPEN-MARKET" in _brands(v)]
    by_email = {v["email"]: v for v in vendors}

    assignments: dict = {}
    unsourced: list = []
    for item in line_items:
        mfr = (item.get("manufacturer") or "").strip()
        matched = [v for v in vendors if mfr and brand_match(mfr, db.uj(v.get("brands"), []))]
        matched_emails = {v["email"] for v in matched}

        # Market-intel first calls for this line, if the intake attached any.
        first_calls = []
        for af in (item.get("approach_first") or []):
            ve = (af.get("vendor_email") or "").lower()
            if ve and ve in by_email and ve not in {v["email"] for v in first_calls}:
                first_calls.append(by_email[ve])
        fc_emails = {v["email"] for v in first_calls}

        ordered = (first_calls
                   + sorted([v for v in matched if v["email"] not in fc_emails], key=_rank_key)
                   + sorted([b for b in brokers
                             if b["email"] not in matched_emails and b["email"] not in fc_emails],
                            key=_rank_key))
        cap = config.VENDOR_MAX_PER_PART
        part_vendors = ordered[:cap] if cap else ordered
        if not part_vendors:
            unsourced.append(item)
            continue
        for v in part_vendors:
            assignments.setdefault(v["email"], (v, []))[1].append(item)
    return assignments, unsourced


# ============================================================
# RFQ drafting
# ============================================================

_CELL = 'padding:6px;border:1px solid #dddddd;'
_TH = 'padding:6px;border:1px solid #dddddd;text-align:left;'


def _rfq_html(vendor: dict, parts: list[dict], quote_number: str) -> str:
    esc = html_mod.escape
    rows = ""
    has_target = False
    for p in parts:
        tc = p.get("_target_cost")     # OUR cost target (negotiation re-RFQ) — never the customer's
        if tc:
            has_target = True
            ask_pct = p.get("_ask_pct")
            prev = p.get("_prev_vendor_cost")
            ask_txt = (f" (about {float(ask_pct):.0f}% below your last USD {float(prev):.4f})"
                       if (ask_pct and prev) else "")
            remark = (f'<span style="color:#c62828;">Please improve to ~USD '
                      f'{float(tc):.4f}{ask_txt} — match or beat to win this order</span>')
        else:
            remark = ""
        rows += (f'<tr><td style="{_CELL}">{esc(str(p.get("mpn") or "(to identify)"))}</td>'
                 f'<td style="{_CELL}">{esc(str(p.get("description") or ""))}</td>'
                 f'<td style="{_CELL}">{esc(str(p.get("manufacturer") or ""))}</td>'
                 f'<td style="{_CELL}text-align:right;">{esc(str(p.get("quantity") or ""))}</td>'
                 f'<td style="{_CELL}"></td><td style="{_CELL}"></td><td style="{_CELL}"></td>'
                 f'<td style="{_CELL}"></td><td style="{_CELL}">{remark}</td></tr>')
    header = (f'<tr style="background:#0d47a1;color:#ffffff;">'
              f'<th style="{_TH}">MPN</th><th style="{_TH}">Description</th>'
              f'<th style="{_TH}">Make</th><th style="{_TH}">Quantity</th>'
              f'<th style="{_TH}">Unit Cost (USD)</th><th style="{_TH}">SPQ</th>'
              f'<th style="{_TH}">MOQ</th><th style="{_TH}">Lead Time</th>'
              f'<th style="{_TH}">Remark</th></tr>')
    if has_target:
        intro = (f'<p>Thank you for your earlier quotation (Ref: <strong>{esc(quote_number)}</strong>). '
                 f'This enquiry has become <strong>price-sensitive</strong> and we are keen to place '
                 f'the order with you &mdash; please see the <em>Remark</em> column for the improved '
                 f'cost we need, and reply with your <strong>best firm price</strong>:</p>')
    else:
        intro = (f'<p>We have a <strong>firm customer enquiry</strong> for the part(s) below '
                 f'(Ref: <strong>{esc(quote_number)}</strong>). '
                 f'Please quote your <strong>best firm price</strong>:</p>')
    name = esc(vendor.get("name") or vendor.get("company") or "Team")
    return f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;">
    <p>Dear {name},</p>
    {intro}
    <table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">
    {header}{rows}</table>
    <p style="margin-top:16px;padding:12px;background:#fff3e0;border-left:4px solid #f57c00;">
    <strong>Please advise, for each line item above:</strong><br>
    1. Available Quantity / Stock<br>
    2. Firm Unit Price (USD)<br>
    3. Date Code (YY+)<br>
    4. Lead Time<br>
    5. MOQ &amp; SPQ<br>
    6. Condition, Packaging (Tray/Reel/Tube) &amp; COO (Country of Origin)<br><br>
    Terms: <strong>USD, EXW, T/T</strong> unless stated otherwise. Where available, please also
    share a <strong>photo of the stock label</strong> (date code + packing).<br>
    Kindly reply on <strong>this same email thread</strong> with your best pricing.</p>
    <p>Best regards,<br>{html_mod.escape(config.COMPANY_ENTITY)} &mdash; Sourcing</p>
    </div>"""


def draft_rfqs(gmail, deal: dict, line_items: list[dict]) -> tuple[list[int], list[dict]]:
    """One RFQ per matched vendor, dispatched per policy (default: Gmail draft).
    Returns ([vendor_rfq ids], [unsourced parts])."""
    assignments, unsourced = route(line_items,
                                   exclude_customer_email=deal.get("customer_email") or "")
    created: list[int] = []
    for vendor, parts in assignments.values():
        to = (vendor.get("email") or "").strip()
        if not to:
            continue
        body = _rfq_html(vendor, parts, deal["quote_number"])
        subject = f"RFQ {deal['quote_number']} - {len(parts)} item(s) - {config.COMPANY_ENTITY}"
        mpns = [(p.get("mpn") or p.get("description") or "") for p in parts]
        vrfq_id = db.insert("vendor_rfqs", {
            "deal_id": deal["id"], "vendor_email": to,
            "vendor_name": vendor.get("name") or vendor.get("company"),
            "requested_mpns": db.j(mpns), "status": "drafted"})
        cc = addrs(vendor.get("all_emails"))
        cc = [a for a in cc if a != to]
        res = drafts.dispatch(gmail, kind="vendor_rfq", to=to, subject=subject,
                              body_html=body, cc=cc or None, deal_id=deal["id"],
                              vendor_rfq_id=vrfq_id,
                              note=f"{len(parts)} part(s) to {vendor.get('name')}")
        if res["status"] == "auto_sent":
            db.update("vendor_rfqs",
                      {"status": "sent", "thread_id": res["thread_id"],
                       "message_id": res["message_id"], "sent_at": db.utcnow()},
                      "id = ?", (vrfq_id,))
        else:
            db.update("vendor_rfqs", {"thread_id": res["thread_id"],
                                      "message_id": res["message_id"]},
                      "id = ?", (vrfq_id,))
        created.append(vrfq_id)
        db.execute("UPDATE counterparties SET rfqs_sent = rfqs_sent + 1, last_seen = ? "
                   "WHERE email = ?", (db.utcnow(), to))
    if unsourced:
        log.warning("[%s] %d part(s) had NO matching vendor", deal.get("quote_number"),
                    len(unsourced))
    return created, unsourced


def redraft_target_cost(gmail, deal: dict, parts: list[dict]) -> list[int]:
    """Negotiation re-RFQ: ask vendors to sharpen THEIR OWN cost, replying on each
    vendor's existing quote thread (one mail trail per vendor). The customer's target
    is never included — only our capped percentage ask on the vendor's last cost."""
    assignments, _ = route(parts, exclude_customer_email=deal.get("customer_email") or "")
    touched: list[int] = []
    for vendor, vparts in assignments.values():
        to = (vendor.get("email") or "").strip()
        if not to:
            continue
        prior = db.query_one(
            "SELECT * FROM vendor_rfqs WHERE deal_id = ? AND vendor_email = ? "
            "AND thread_id IS NOT NULL ORDER BY id DESC LIMIT 1", (deal["id"], to))
        body = _rfq_html(vendor, vparts, deal["quote_number"])
        subject = f"RFQ {deal['quote_number']} - revised target - {config.COMPANY_ENTITY}"
        mpns = [(p.get("mpn") or p.get("description") or "") for p in vparts]
        if prior:
            res = drafts.dispatch(gmail, kind="vendor_rfq", to=to, subject=subject,
                                  body_html=body, thread_id=prior["thread_id"],
                                  deal_id=deal["id"], vendor_rfq_id=prior["id"],
                                  note="negotiation re-ask (in-thread)")
            db.update("vendor_rfqs",
                      {"requested_mpns": db.j(mpns),
                       "status": "sent" if res["status"] == "auto_sent" else "drafted",
                       "sent_at": db.utcnow() if res["status"] == "auto_sent" else None,
                       "replied_at": None},
                      "id = ?", (prior["id"],))
            touched.append(prior["id"])
        else:
            vrfq_id = db.insert("vendor_rfqs", {
                "deal_id": deal["id"], "vendor_email": to,
                "vendor_name": vendor.get("name") or vendor.get("company"),
                "requested_mpns": db.j(mpns), "status": "drafted"})
            res = drafts.dispatch(gmail, kind="vendor_rfq", to=to, subject=subject,
                                  body_html=body, deal_id=deal["id"], vendor_rfq_id=vrfq_id,
                                  note="negotiation re-ask (new thread)")
            db.update("vendor_rfqs", {"thread_id": res["thread_id"],
                                      "message_id": res["message_id"]}, "id = ?", (vrfq_id,))
            touched.append(vrfq_id)
    return touched


# ============================================================
# Reading vendor replies
# ============================================================

def check_vendor_replies(gmail, deal: dict) -> int:
    """Parse vendor COST replies on each RFQ thread into vendor_quotes rows.

    A vendor thread keeps being read until EVERY requested part is quoted — vendors
    often price part 1 now and part 2 in a later reply. Terminal states: 'replied'
    (all parts in) or 'no_bid'; a partial thread is re-read every cycle. Also LEARNS
    the vendor's team: reply-from + CC'd colleagues are saved onto the vendor row so
    the next RFQ reaches the whole desk first try (bounce addresses filtered)."""
    known_mpns = [it.get("mpn") for it in (db.uj(deal.get("line_items"), []) or [])
                  if it.get("mpn")]
    rfqs = db.query("SELECT * FROM vendor_rfqs WHERE deal_id = ? "
                    "AND status IN ('sent','partial')", (deal["id"],))
    our = (gmail.user_email or "").lower()
    our_dom = our.split("@")[-1] if "@" in our else ""
    new_count = 0

    for vrfq in rfqs:
        if not vrfq.get("thread_id"):
            continue
        try:
            replies = gmail.get_thread_replies(vrfq["thread_id"],
                                               after_message_id=vrfq.get("message_id"))
        except Exception as e:
            log.error("[deal %s] vendor thread fetch failed (%s): %s",
                      deal["id"], vrfq.get("vendor_name"), e)
            continue

        requested = {(m or "").strip().upper()
                     for m in (db.uj(vrfq.get("requested_mpns"), []) or []) if m}
        already = {(q["mpn"] or "").strip().upper() for q in db.query(
            "SELECT mpn FROM vendor_quotes WHERE vendor_rfq_id = ?", (vrfq["id"],))}

        vendor = db.query_one("SELECT * FROM counterparties WHERE email = ?",
                              ((vrfq.get("vendor_email") or "").lower(),))
        cur_hint = (vendor or {}).get("currency") or deal.get("currency") or "USD"

        parsed_any = declined_any = False
        for reply in replies:
            frm = (reply.get("from_email") or "").lower()
            if our and our in frm:
                continue

            # Learn the vendor's team from ANY reply (even a no-pricing ack).
            if vendor:
                learned = [a for a in (addrs(reply.get("from_email")) + addrs(reply.get("cc")))
                           if not (our_dom and a.endswith("@" + our_dom))
                           and not is_system_addr(a)]
                have = addrs(vendor.get("all_emails")) + [vendor["email"]]
                fresh = [a for a in learned if a not in have]
                if fresh:
                    new_all = "; ".join(addrs(vendor.get("all_emails")) + fresh)
                    db.update("counterparties", {"all_emails": new_all},
                              "email = ?", (vendor["email"],))
                    vendor["all_emails"] = new_all
                    log.info("[deal %s] learned %d contact(s) for %s: %s",
                             deal["id"], len(fresh), vrfq.get("vendor_name"), fresh)

            body = bom.best_body(reply.get("body_text", ""), reply.get("body_html", ""),
                                 min_len=20)
            if not body or len(body.strip()) < 10:
                continue
            if any(w in body.lower() for w in _DECLINE_MARKERS):
                declined_any = True
            try:
                pricing_out = brain.extract_pricing_from_reply(body, known_mpns, cur_hint)
            except Exception as e:
                log.error("[deal %s] pricing extract failed (%s): %s",
                          deal["id"], vrfq.get("vendor_name"), e)
                continue
            if not (pricing_out and pricing_out.get("has_pricing")):
                continue
            det_cur = (pricing_out.get("currency") or cur_hint or "USD").upper()
            for it in pricing_out.get("items", []):
                cost = pricing.to_float(it.get("unit_price"))
                if cost is None:
                    continue
                mkey = (it.get("mpn") or "").strip().upper()
                if mkey and mkey in already:
                    continue                        # re-read of the thread — never duplicate
                validity_raw = it.get("validity") or pricing_out.get("pricing_notes")
                db.insert("vendor_quotes", {
                    "deal_id": deal["id"], "vendor_rfq_id": vrfq["id"],
                    "vendor_email": vrfq.get("vendor_email"),
                    "vendor_name": vrfq.get("vendor_name"),
                    "mpn": it.get("mpn"), "cost_price": cost, "currency": det_cur,
                    "moq": pricing.to_int(it.get("moq")), "spq": pricing.to_int(it.get("spq")),
                    "offered_qty": pricing.to_int(it.get("available_qty")),
                    "lead_time": it.get("lead_time"),
                    "packaging": it.get("packaging") or None,
                    "date_code": it.get("date_code") or None,
                    "valid_until": pricing.parse_validity(it.get("validity")),
                    "validity_raw": (str(validity_raw)[:120] if validity_raw else None),
                    "notes": it.get("notes"),
                })
                if det_cur == "USD":
                    from ..market import history
                    history.record_price(it.get("mpn") or "", cost, side="vendor_quote",
                                         source=vrfq.get("vendor_name") or "vendor",
                                         qty=pricing.to_int(it.get("available_qty")),
                                         date_code=it.get("date_code"))
                if mkey:
                    already.add(mkey)
                new_count += 1
                parsed_any = True

        now = db.utcnow()
        if parsed_any and (not requested or requested.issubset(already)):
            db.update("vendor_rfqs", {"status": "replied", "replied_at": now},
                      "id = ?", (vrfq["id"],))
            db.execute("UPDATE counterparties SET rfqs_replied = rfqs_replied + 1, "
                       "quotes_received = quotes_received + 1 WHERE email = ?",
                       ((vrfq.get("vendor_email") or "").lower(),))
        elif already:
            db.update("vendor_rfqs", {"status": "partial", "replied_at": now},
                      "id = ?", (vrfq["id"],))
        elif declined_any:
            db.update("vendor_rfqs", {"status": "no_bid", "replied_at": now},
                      "id = ?", (vrfq["id"],))
            log.info("[deal %s] %s declined — No-Bid recorded",
                     deal["id"], vrfq.get("vendor_name"))
    return new_count


def sourcing_ready(deal_id: int) -> tuple[bool, bool]:
    """(all_replied_or_terminal, wait_window_elapsed) for the consolidation trigger."""
    rfqs = db.query("SELECT status, sent_at FROM vendor_rfqs WHERE deal_id = ?", (deal_id,))
    live = [r for r in rfqs if r["status"] not in ("suppressed", "drafted")]
    if not live:
        return False, False
    all_done = all(r["status"] in ("replied", "no_bid", "no_reply") for r in live)
    sent_times = [r["sent_at"] for r in live if r.get("sent_at")]
    elapsed = False
    if sent_times:
        try:
            oldest = min(datetime.fromisoformat(str(t)) for t in sent_times)
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            hours = (datetime.now(timezone.utc) - oldest).total_seconds() / 3600
            elapsed = hours > config.VENDOR_REPLY_WAIT_HOURS
        except (ValueError, TypeError):
            pass
    return all_done, elapsed
