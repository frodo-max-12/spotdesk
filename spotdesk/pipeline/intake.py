"""Intake — the front door. One inbound email in, the right handler out.

Order of guards matters and each traces to a live mis-handling:
  0   already-seen? skip if finished, RESUME if interrupted (power cut between the
      dedup commit and the RFQ fan-out used to strand inquiries forever);
  0.2 reply on one of OUR vendor-RFQ threads → the sourcing monitor's job, never a
      customer email (a supplier's quote once became a bogus "customer negotiation");
  0.3 vendor replying to one of OUR POs → order status update, never a "thank you for
      your PO" back at the supplier;
  0.4 off-thread vendor quote (fresh email carrying our quote number) → matched back
      by the reference so a real quote/no-bid is never lost;
  0.5 internal domain → skip chatter, but process forwarded customer business;
  then classify → handler.

The dedup marker (emails row, UNIQUE gmail_id) commits BEFORE any handler runs, and
mark-as-read happens only AFTER the handler commits — so a crash anywhere leaves the
system resumable with no duplicate outbound.
"""

from __future__ import annotations

import logging
import re

from .. import config, db
from ..hitl import drafts
from ..knowledge.line_card import line_card
from ..llm import brain
from ..market import history, lookup as market_lookup
from . import bom, negotiation, orders, sourcing
from .sourcing import addrs

log = logging.getLogger("spotdesk.intake")

_QUOTE_RE = re.compile(re.escape(config.QUOTE_PREFIX) + r"-Q-\d{8}-\d{4}")
_PO_RE = re.compile(re.escape(config.QUOTE_PREFIX) + r"-PO-\d{8}-\d{4}(?:-\d+)?")


def _is_internal(from_email: str) -> bool:
    dom = (from_email or "").lower().split("@")[-1].replace(">", "").strip()
    return any(d in dom for d in config.INTERNAL_DOMAINS)


def _extract_name(from_email: str) -> str:
    if "<" in (from_email or ""):
        name = from_email.split("<")[0].strip().strip('"')
        if name:
            return name
    email_part = (from_email or "").split("<")[-1].replace(">", "").strip()
    return email_part.split("@")[0].replace(".", " ").title()


def _bare_addr(from_email: str) -> str:
    e = from_email or ""
    return (e.split("<")[-1].replace(">", "").strip() if "<" in e else e.strip()).lower()


def _customer_team(email_data: dict) -> list[str]:
    """The sender's own colleagues on the mail (To+Cc minus us minus the sender) —
    kept in CC so their whole team stays in the loop on every reply."""
    sender = addrs(email_data.get("from_email"))
    team = []
    for a in addrs(email_data.get("to_email")) + addrs(email_data.get("cc")):
        if _is_internal(a) or a in sender or a in team:
            continue
        team.append(a)
    return team


# ============================================================
# Entry point
# ============================================================

def process_email(gmail, email_data: dict) -> dict:
    result = {"email_id": None, "type": None, "actions": []}

    # --- 0: dedup / resume ---
    existing = db.query_one("SELECT * FROM emails WHERE gmail_id = ?",
                            (email_data["gmail_id"],))
    if existing:
        skip, acts = _resume_incomplete(gmail, existing, email_data)
        if skip:
            return {"email_id": existing["id"], "type": existing["email_type"],
                    "actions": acts}
        _safe_mark_read(gmail, email_data["gmail_id"])
        return {"email_id": existing["id"], "type": existing["email_type"],
                "actions": acts + ["resumed"]}

    # --- 0.2: vendor reply on one of our RFQ threads ---
    if email_data.get("thread_id"):
        on_vendor_thread = db.query_one(
            "SELECT id, deal_id FROM vendor_rfqs WHERE thread_id = ?",
            (email_data["thread_id"],))
        if on_vendor_thread:
            _safe_mark_read(gmail, email_data["gmail_id"], label="Desk/VendorQuote")
            return {"email_id": None, "type": "vendor_reply",
                    "actions": ["left_for_sourcing_monitor"]}

    # --- 0.3: vendor reply to one of OUR POs ---
    vpo = None
    if email_data.get("thread_id"):
        vpo = db.query_one("SELECT * FROM vendor_pos WHERE thread_id = ?",
                           (email_data["thread_id"],))
    if not vpo:
        m = _PO_RE.search(email_data.get("subject") or "")
        if m:
            vpo = db.query_one("SELECT * FROM vendor_pos WHERE po_number = ?",
                               (m.group(0),))
    if vpo:
        acts = orders.handle_vendor_po_reply(gmail, vpo, email_data)
        _safe_mark_read(gmail, email_data["gmail_id"], label="Desk/Order")
        return {"email_id": None, "type": "vendor_po_reply", "actions": acts}

    # --- 0.4: off-thread vendor quote carrying our quote number ---
    mq = _QUOTE_RE.search(email_data.get("subject") or "")
    if mq and not _is_internal(email_data.get("from_email", "")):
        deal = db.query_one("SELECT * FROM deals WHERE quote_number = ?", (mq.group(0),))
        if deal:
            cust_dom = (deal.get("customer_email") or "").lower().split("@")[-1]
            frm_dom = _bare_addr(email_data.get("from_email", "")).split("@")[-1]
            if cust_dom and frm_dom and cust_dom != frm_dom:
                acts = sourcing_offthread_quote(deal, email_data)
                _safe_mark_read(gmail, email_data["gmail_id"], label="Desk/VendorQuote")
                return {"email_id": None, "type": "vendor_quote", "actions": acts}

    # --- 0.5: internal domain — chatter vs forwarded business (decided post-classify) ---
    internal_forward = _is_internal(email_data.get("from_email", ""))

    # --- classify ---
    body_for_classify = bom.best_body(email_data.get("body_text", ""),
                                      email_data.get("body_html", ""))
    learned = _learned_context(email_data)
    classification = brain.classify_email(email_data.get("subject", ""), body_for_classify,
                                          email_data.get("from_email", ""), learned)
    email_type = classification["type"]
    urgency = {"rfq": "high", "negotiation": "critical", "po": "priority"}.get(
        email_type, classification.get("urgency", "normal"))

    if internal_forward and email_type not in ("rfq", "po", "negotiation"):
        _safe_mark_read(gmail, email_data["gmail_id"], label="Desk/Internal")
        return {"email_id": None, "type": "internal",
                "actions": [f"skipped_internal_{email_type}"]}

    # --- persist the idempotency marker BEFORE any handler sends anything ---
    email_id = db.insert("emails", {
        "gmail_id": email_data["gmail_id"], "thread_id": email_data.get("thread_id"),
        "from_email": email_data.get("from_email"),
        "from_name": _extract_name(email_data.get("from_email", "")),
        "to_email": email_data.get("to_email"), "cc": email_data.get("cc"),
        "subject": email_data.get("subject"),
        "body_text": email_data.get("body_text"), "body_html": email_data.get("body_html"),
        "email_type": email_type, "urgency": urgency,
        "has_attachments": 1 if email_data.get("has_attachments") else 0,
        "attachment_names": db.j(email_data.get("attachment_names")),
        "classification": db.j(classification),
    })
    result["email_id"] = email_id
    result["type"] = email_type

    label = {"rfq": "Desk/RFQ", "po": "Desk/PO", "negotiation": "Desk/Negotiation",
             "vendor_offer": "Desk/MarketIntel", "logistics_update": "Desk/Logistics",
             "complaint": "Desk/Escalate", "spam": "Desk/Spam"}.get(email_type, "Desk/Other")
    try:
        gmail.add_label(email_data["gmail_id"], label)
    except Exception as e:
        log.warning("label failed: %s", e)

    if email_type not in ("spam",):
        try:
            _upsert_lead(email_id, email_data, classification)
        except Exception as e:
            log.warning("lead upsert failed: %s", e)

    # --- dispatch handler ---
    email_row = db.query_one("SELECT * FROM emails WHERE id = ?", (email_id,))
    try:
        if email_type == "rfq":
            result["actions"] = handle_rfq(gmail, email_row, email_data)
        elif email_type == "po":
            result["actions"] = orders.handle_customer_po(gmail, email_row, email_data)
        elif email_type == "negotiation":
            result["actions"] = negotiation.handle_negotiation(gmail, email_row, email_data)
        elif email_type == "vendor_offer":
            result["actions"] = handle_vendor_offer(email_row, email_data)
        elif email_type == "logistics_update":
            result["actions"] = orders.handle_logistics_update(gmail, email_row, email_data)
        elif email_type == "complaint":
            result["actions"] = handle_complaint(gmail, email_row, email_data)
        elif email_type == "spam":
            db.update("emails", {"status": "archived"}, "id = ?", (email_id,))
            result["actions"] = ["archived_spam"]
        else:
            db.update("emails", {"status": "processing"}, "id = ?", (email_id,))
            result["actions"] = ["classified_as_other"]
    except Exception:
        # The email row is committed — mark-as-read is skipped so the next cycle
        # resumes the missing work instead of double-sending.
        log.exception("handler failed for email %s", email_id)
        raise

    db.audit("email_processed", {"email_id": email_id, "type": email_type,
                                 "actions": result["actions"]})
    _safe_mark_read(gmail, email_data["gmail_id"])
    return result


def _safe_mark_read(gmail, gmail_id: str, label: str | None = None):
    try:
        if label:
            gmail.add_label(gmail_id, label)
        gmail.mark_as_read(gmail_id)
    except Exception as e:
        log.warning("mark-read/label failed for %s: %s", gmail_id, e)


def _learned_context(email_data: dict) -> str:
    """Few-shot examples from past human corrections with overlapping keywords —
    prompt-based learning, effective immediately, no retraining."""
    subject = (email_data.get("subject") or "").lower()
    keywords = {w for w in subject.split() if len(w) > 3}
    if not keywords:
        return ""
    rows = db.query("SELECT * FROM classification_feedback ORDER BY id DESC LIMIT 200")
    scored = []
    for c in rows:
        overlap = len(keywords & {w for w in (c.get("subject") or "").lower().split()})
        if overlap:
            scored.append((overlap, c))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return ""
    lines = ["LEARNED FROM PAST CORRECTIONS:"]
    for i, (_, c) in enumerate(scored[:3], 1):
        lines.append(f"Example {i}: subject '{(c.get('subject') or '')[:80]}' was wrongly "
                     f"classified '{c.get('agent_classification')}', correct class is "
                     f"'{c.get('human_correction')}'. {c.get('reason') or ''}")
    return "\n".join(lines)


def _upsert_lead(email_id: int, email_data: dict, classification: dict):
    addr = _bare_addr(email_data.get("from_email", ""))
    if "@" not in addr or _is_internal(addr):
        return
    info = brain.extract_customer_info(email_data.get("from_email", ""),
                                       email_data.get("body_text") or
                                       email_data.get("body_html") or "")
    email_data["_customer_info"] = info    # memoized — handlers reuse without a second call
    row = db.query_one("SELECT email FROM counterparties WHERE email = ?", (addr,))
    if row:
        db.execute("UPDATE counterparties SET n_inquiries = n_inquiries + 1, last_seen = ? "
                   "WHERE email = ?", (db.utcnow(), addr))
        updates = {}
        if info.get("name"):
            updates["name"] = info["name"]
        if info.get("company"):
            updates["company"] = info["company"]
        if updates:
            db.update("counterparties", updates,
                      "email = ? AND (name IS NULL OR company IS NULL)", (addr,))
    else:
        db.insert("counterparties", {
            "email": addr, "name": info.get("name"), "company": info.get("company"),
            "domain": addr.split("@")[-1], "kind": "buyer",
            "trust_score": config.DEFAULT_TRUST_SCORE, "n_inquiries": 1,
            "notes": f"auto-captured from inbox ({classification.get('type')})"})


# ============================================================
# RFQ handler
# ============================================================

def handle_rfq(gmail, email_row: dict, email_data: dict) -> list[str]:
    actions: list[str] = []
    email_id = email_row["id"]

    # -- extract from body (HTML preferred) + attachments, then composite-merge --
    collected: list[dict] = []
    html_body = email_data.get("body_html") or ""
    plain_body = email_data.get("body_text") or ""
    html_items: list[dict] = []
    if html_body:
        try:
            html_items = bom.extract_from_email(email_data.get("subject", ""), html_body,
                                                our_domains=config.INTERNAL_DOMAINS)
            collected.extend(html_items)
        except Exception as e:
            log.warning("HTML BOM extraction failed: %s", e)
    if not html_items and len(plain_body.strip()) >= 20:
        try:
            collected.extend(bom.extract_from_email(email_data.get("subject", ""), plain_body,
                                                    our_domains=config.INTERNAL_DOMAINS))
        except Exception as e:
            log.warning("plain BOM extraction failed: %s", e)
    for att in (email_data.get("attachment_refs") or []):
        if not att.get("attachment_id"):
            continue
        try:
            file_bytes = gmail.download_attachment(email_data["gmail_id"], att["attachment_id"])
            collected.extend(bom.extract_from_attachment(file_bytes, att["filename"],
                                                         att.get("mime_type", "")))
        except Exception as e:
            log.warning("attachment %s failed: %s", att.get("filename"), e)

    items = bom.merge_items(collected)
    log.info("extracted %d unique BOM items (from %d raw)", len(items), len(collected))
    if not items:
        db.update("emails", {"status": "escalated"}, "id = ?", (email_id,))
        return ["no_bom_extracted_escalated"]

    # -- revision? a thread that already carries a deal gets updated, never duplicated --
    existing_deal = _deal_for_thread(email_data.get("thread_id"), exclude_email_id=email_id)
    if existing_deal:
        return _apply_revision(gmail, existing_deal, email_row, email_data, items)

    # -- ground bare part numbers in a real distributor database --
    identified = 0
    for it in items:
        mpn = (it.get("mpn") or "").strip()
        if mpn and (not it.get("manufacturer") or not it.get("description")):
            info = market_lookup.identify_part(mpn)
            if info:
                it.setdefault("manufacturer", None)
                if not it.get("manufacturer") and info.get("manufacturer"):
                    it["manufacturer"] = info["manufacturer"]
                if not it.get("description") and info.get("description"):
                    it["description"] = info["description"]
                identified += 1
    if identified:
        actions.append(f"identified_{identified}_parts")

    # -- market intel: match against the standing offer book (LLM judges, code retrieves) --
    try:
        offers = history.recent_market_offers()
        if offers:
            matches = brain.match_market_offers(items, offers)
            by_vendor = {(o["vendor"] or "").lower(): o.get("vendor_email") for o in offers}
            n = 0
            for idx, it in enumerate(items):
                m = matches.get(str(idx)) or matches.get(idx)
                if not m or not (m.get("offers") or m.get("approach_first")):
                    continue
                it["market_offers"] = m.get("offers") or []
                it["approach_first"] = [{"vendor": v, "vendor_email": by_vendor.get((v or "").lower())}
                                        for v in (m.get("approach_first") or [])]
                it["market_note"] = m.get("note")
                n += 1
            if n:
                actions.append(f"market_intel_matched_{n}")
    except Exception as e:
        log.warning("market-offer match failed: %s", e)

    # -- line card + currency --
    lc = line_card()
    for it in items:
        it["line_status"] = "green" if lc.is_authorized(it.get("manufacturer") or "") else "amber"
    currency, assumed, source = _infer_currency(items, email_data)

    # -- create the deal --
    info = email_data.get("_customer_info") or {}
    customer_name = info.get("name") or _extract_name(email_data.get("from_email", ""))
    quote_number = f"{config.QUOTE_PREFIX}-Q-{db.utcnow()[:10].replace('-', '')}-{email_id:04d}"
    team_cc = _customer_team(email_data)
    deal_id = db.insert("deals", {
        "email_id": email_id, "quote_number": quote_number,
        "customer_name": customer_name,
        "customer_email": _bare_addr(email_data.get("from_email", "")),
        "customer_cc": "; ".join(team_cc),
        "customer_company": info.get("company"),
        "currency": currency, "currency_assumed": 1 if assumed else 0,
        "currency_source": source, "status": "awaiting_pricing",
        "line_items": db.j([_quote_line(it) for it in items]),
        "validity_days": config.QUOTE_VALIDITY_DAYS,
    })
    deal = db.query_one("SELECT * FROM deals WHERE id = ?", (deal_id,))
    actions += [f"deal_{quote_number}", f"extracted_{len(items)}_items"]

    # -- acknowledgement draft (policy-gated; testing mode still drafts safely) --
    try:
        summary = "\n".join(
            f"- {(it.get('mpn') or ('[' + (it.get('description') or '')[:40] + ']'))} "
            f"({it.get('manufacturer') or 'make TBC'}) x {it.get('quantity') or '?'} pcs"
            for it in items)
        ack = brain.draft_acknowledgement(customer_name, info.get("company") or "", summary)
        drafts.dispatch(gmail, kind="ack", to=deal["customer_email"],
                        subject=email_data.get("subject") or "Your enquiry",
                        body_html=ack, cc=team_cc, thread_id=email_data.get("thread_id"),
                        deal_id=deal_id, note="acknowledgement")
        actions.append("ack_dispatched")
    except Exception as e:
        log.error("ack draft failed: %s", e)
        actions.append("ack_failed")

    # -- fan RFQs to the vendor panel (drafts by default) --
    try:
        created, unsourced = sourcing.draft_rfqs(gmail, deal, items)
        db.update("deals", {"status": "sourcing_vendors", "updated_at": db.utcnow()},
                  "id = ?", (deal_id,))
        actions.append(f"rfqs_to_{len(created)}_vendors")
        if unsourced:
            actions.append(f"{len(unsourced)}_parts_unsourced")
    except Exception as e:
        log.error("vendor sourcing failed: %s", e)
        actions.append("sourcing_failed")

    db.update("emails", {"status": "processing"}, "id = ?", (email_id,))
    return actions


def _quote_line(it: dict) -> dict:
    return {"mpn": it.get("mpn"), "manufacturer": it.get("manufacturer"),
            "quantity": it.get("quantity"), "description": it.get("description"),
            "package": it.get("package"), "annual_quantity": it.get("annual_quantity"),
            "needs_identification": it.get("needs_identification", False),
            "line_status": it.get("line_status"),
            "approach_first": it.get("approach_first"),
            "market_note": it.get("market_note"),
            "target_price": it.get("target_price"),
            "unit_price": None, "lead_time": None, "moq": None, "spq": None}


def _deal_for_thread(thread_id: str | None, exclude_email_id: int | None = None) -> dict | None:
    if not thread_id:
        return None
    sql = ("SELECT d.* FROM deals d JOIN emails e ON d.email_id = e.id "
           "WHERE e.thread_id = ?")
    params: tuple = (thread_id,)
    if exclude_email_id:
        sql += " AND e.id != ?"
        params += (exclude_email_id,)
    rows = db.query(sql + " ORDER BY d.id DESC LIMIT 1", params)
    return rows[0] if rows else None


def _apply_revision(gmail, deal: dict, email_row: dict, email_data: dict,
                    items: list[dict]) -> list[str]:
    """Customer follow-up on an existing deal thread (typically a changed quantity):
    update THAT deal, wipe the affected lines' pricing, re-source — never a duplicate."""
    actions = [f"revision_on_{deal['quote_number']}"]
    lines = [dict(it) for it in (db.uj(deal.get("line_items"), []) or [])]
    by_mpn = {(l.get("mpn") or "").strip().upper(): l for l in lines}
    changed = 0
    for it in items:
        mpn = (it.get("mpn") or "").strip().upper()
        newq = it.get("quantity")
        if not (mpn and newq):
            continue
        if mpn in by_mpn:
            if by_mpn[mpn].get("quantity") != newq:
                by_mpn[mpn].update(quantity=newq, unit_price=None, cost_price=None,
                                   selected_vendor=None, fulfillment=None,
                                   pricing_status=None, remark=None)
                changed += 1
        else:
            lines.append(_quote_line(it))
            changed += 1
    db.update("deals", {"line_items": db.j(lines),
                        "status": "sourcing_vendors" if changed else deal["status"],
                        "updated_at": db.utcnow()}, "id = ?", (deal["id"],))
    actions.append(f"updated_{changed}_lines")
    if changed:
        deal = db.query_one("SELECT * FROM deals WHERE id = ?", (deal["id"],))
        try:
            created, _ = sourcing.draft_rfqs(gmail, deal, lines)
            actions.append(f"re_sourced_{len(created)}_vendors")
        except Exception as e:
            log.error("revision re-source failed: %s", e)
    try:
        import html as _h
        ack = (f'<p>Dear {_h.escape(deal.get("customer_name") or "Customer")},</p>'
               f'<p>Thank you — we have noted your revised requirement and are re-checking '
               f'pricing with our sources. A revised quotation will follow shortly.</p>'
               f'<p>Best regards,<br>{_h.escape(config.COMPANY_ENTITY)} Sales Team</p>')
        drafts.dispatch(gmail, kind="ack", to=deal["customer_email"],
                        subject=email_data.get("subject") or "Your enquiry", body_html=ack,
                        cc=_customer_team(email_data), thread_id=email_data.get("thread_id"),
                        deal_id=deal["id"], note="revision ack")
        actions.append("revision_ack_dispatched")
    except Exception as e:
        log.error("revision ack failed: %s", e)
    return actions


def _infer_currency(items: list[dict], email_data: dict) -> tuple[str, bool, str]:
    """USD desk: detect an explicitly-stated currency; non-USD holds at the gate.
    Order matters — S$/SGD and € before the bare $."""
    for it in items:
        cur = (it.get("currency") or "").strip().upper()
        if cur in ("USD", "SGD", "EUR", "INR"):
            return cur, False, f"stated:{cur.lower()}"
    blob = " ".join(f"{it.get('target_price') or ''} {it.get('special_requirements') or ''}"
                    for it in items)
    blob += " " + (email_data.get("body_text") or "")[:2000]
    low = blob.lower()
    if ("s$" in low) or ("sgd" in low) or ("singapore dollar" in low):
        return "SGD", False, "symbol:sgd"
    if ("€" in blob) or ("eur" in low) or ("euro" in low):
        return "EUR", False, "symbol:eur"
    if ("₹" in blob) or ("inr" in low) or ("rs." in low) or (" rs " in low) or ("rupee" in low):
        return "INR", False, "symbol:inr"
    if ("$" in blob) or ("usd" in low) or ("us$" in low) or ("dollar" in low):
        return "USD", False, "symbol:usd"
    return "USD", True, "default:usd"


# ============================================================
# Resume-after-interrupt
# ============================================================

def _resume_incomplete(gmail, existing: dict, email_data: dict) -> tuple[bool, list]:
    """(skip, actions). RFQ emails whose earlier run was cut off between deal creation
    and the vendor fan-out are resumed idempotently; everything finished is skipped."""
    if existing.get("email_type") != "rfq":
        return True, ["skipped_duplicate"]
    deal = db.query_one("SELECT * FROM deals WHERE email_id = ?", (existing["id"],))
    if deal is None:
        log.warning("resuming interrupted RFQ %s — no deal yet, re-running handler",
                    email_data["gmail_id"])
        actions = handle_rfq(gmail, existing, email_data)
        return False, actions + ["resumed_from_scratch"]
    if deal["status"] == "awaiting_pricing":
        n = (db.query_one("SELECT COUNT(*) AS n FROM vendor_rfqs WHERE deal_id = ?",
                          (deal["id"],)) or {}).get("n", 0)
        if n == 0:
            log.warning("resuming %s — deal exists but RFQs never went out",
                        deal["quote_number"])
            items = db.uj(deal.get("line_items"), []) or []
            created, _ = sourcing.draft_rfqs(gmail, deal, items)
            db.update("deals", {"status": "sourcing_vendors"}, "id = ?", (deal["id"],))
            return False, [f"resumed_rfqs_{len(created)}"]
        db.update("deals", {"status": "sourcing_vendors"}, "id = ?", (deal["id"],))
        return False, ["resumed_status_fixed"]
    return True, ["skipped_duplicate"]


# ============================================================
# Off-thread vendor quote / vendor offer / complaint
# ============================================================

def sourcing_offthread_quote(deal: dict, email_data: dict) -> list[str]:
    """A vendor replied in a FRESH thread carrying our quote number (a colleague, a
    new email). Match the sender to a vendor we RFQ'd by domain and parse it like any
    reply — a real quote or no-bid must never be lost to threading."""
    from .pricing import parse_validity, to_float, to_int
    actions: list[str] = []
    frm_dom = _bare_addr(email_data.get("from_email", "")).split("@")[-1]
    rfqs = db.query("SELECT * FROM vendor_rfqs WHERE deal_id = ?", (deal["id"],))
    vrfq = next((r for r in rfqs
                 if (r.get("vendor_email") or "").split("@")[-1] == frm_dom), None)
    body = bom.best_body(email_data.get("body_text", ""), email_data.get("body_html", ""),
                         min_len=20)
    known = [it.get("mpn") for it in (db.uj(deal.get("line_items"), []) or []) if it.get("mpn")]
    pricing_out = None
    try:
        pricing_out = brain.extract_pricing_from_reply(body, known,
                                                       deal.get("currency") or "USD")
    except Exception as e:
        log.warning("off-thread quote parse failed: %s", e)

    if pricing_out and pricing_out.get("has_pricing") and vrfq:
        det = (pricing_out.get("currency") or deal.get("currency") or "USD").upper()
        n = 0
        for it in pricing_out.get("items", []):
            cost = to_float(it.get("unit_price"))
            if cost is None:
                continue
            db.insert("vendor_quotes", {
                "deal_id": deal["id"], "vendor_rfq_id": vrfq["id"],
                "vendor_email": vrfq.get("vendor_email"), "vendor_name": vrfq.get("vendor_name"),
                "mpn": it.get("mpn"), "cost_price": cost, "currency": det,
                "moq": to_int(it.get("moq")), "spq": to_int(it.get("spq")),
                "offered_qty": to_int(it.get("available_qty")),
                "lead_time": it.get("lead_time"), "packaging": it.get("packaging"),
                "date_code": it.get("date_code"),
                "valid_until": parse_validity(it.get("validity")),
                "notes": it.get("notes")})
            n += 1
        if n:
            db.update("vendor_rfqs", {"status": "replied", "replied_at": db.utcnow()},
                      "id = ?", (vrfq["id"],))
            actions.append(f"offthread_quote_captured_{n}")
    elif vrfq and any(w in body.lower() for w in
                      ("no bid", "no-bid", "no stock", "cannot quote", "unable to quote",
                       "we decline", "not able to")):
        db.update("vendor_rfqs", {"status": "no_bid", "replied_at": db.utcnow()},
                  "id = ?", (vrfq["id"],))
        actions.append("offthread_no_bid")
    else:
        actions.append("offthread_unmatched")
    return actions or ["offthread_seen"]


def handle_vendor_offer(email_row: dict, email_data: dict) -> list[str]:
    """A vendor pushed an unsolicited stock offer: capture the lines as standing
    market intel (no reply). Surfaced later when a customer asks for a matching part."""
    body = bom.best_body(email_data.get("body_text", ""), email_data.get("body_html", ""),
                         min_len=60)
    offer = brain.extract_vendor_offer(email_data.get("subject", ""), body)
    vemail = _bare_addr(email_data.get("from_email", ""))
    vname = _extract_name(email_data.get("from_email", ""))
    stored = 0
    for it in offer.get("items", []):
        mpn = (it.get("mpn") or "").strip()
        price = None
        try:
            price = float(str(it.get("unit_price")).replace(",", ""))
        except (TypeError, ValueError):
            pass
        if not mpn or price is None:
            continue
        db.insert("market_offers", {
            "mpn": mpn.upper(), "manufacturer": it.get("manufacturer"),
            "vendor_email": vemail or None, "vendor_name": vname,
            "unit_price": price, "currency": (it.get("currency") or "USD").upper(),
            "date_code": it.get("date_code"), "lead_time": it.get("lead_time"),
            "moq": it.get("moq"), "offered_qty": it.get("quantity"),
            "packaging": it.get("packaging"),
            "source_gmail_id": email_data.get("gmail_id"),
            "source_thread_id": email_data.get("thread_id")})
        if (it.get("currency") or "USD").upper() == "USD":
            history.record_price(mpn, price, side="vendor_offer", source=vname,
                                 qty=it.get("quantity"), date_code=it.get("date_code"))
        stored += 1
    # Vendors who blast offers are counterparties too — make sure they're on file.
    if vemail and stored and not db.query_one(
            "SELECT email FROM counterparties WHERE email = ?", (vemail,)):
        db.insert("counterparties", {"email": vemail, "name": vname,
                                     "domain": vemail.split("@")[-1], "kind": "vendor",
                                     "brands": db.j(["ANY"]),
                                     "notes": "auto-captured from stock-offer blast"})
    db.update("emails", {"status": "done"}, "id = ?", (email_row["id"],))
    return [f"captured_{stored}_market_offers"]


def handle_complaint(gmail, email_row: dict, email_data: dict) -> list[str]:
    import html as _h
    name = _extract_name(email_data.get("from_email", ""))
    body = (f"<p>Dear {_h.escape(name)},</p>"
            f"<p>Thank you for reaching out. We take your concern very seriously.</p>"
            f"<p>Your message has been escalated to our senior team and you will receive a "
            f"detailed response shortly.</p><p>We sincerely apologize for any inconvenience.</p>"
            f"<p>Best regards,<br>{_h.escape(config.COMPANY_ENTITY)} Sales Team</p>")
    drafts.dispatch(gmail, kind="ack", to=_bare_addr(email_data.get("from_email", "")),
                    subject=email_data.get("subject") or "Your message",
                    body_html=body, cc=_customer_team(email_data),
                    thread_id=email_data.get("thread_id"), note="complaint holding reply")
    db.update("emails", {"status": "escalated"}, "id = ?", (email_row["id"],))
    return ["holding_reply_dispatched", "escalated_to_human"]
