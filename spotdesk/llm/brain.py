"""The desk brain — every LLM task, one per function.

These prompt bodies are battle-tested against months of real desk mail; most rules
exist because a live email once broke a naive version (jagged Gmail tables, customers
replying on top of our own offers, vendors quoting our internal target back at us,
"10K nos" supply caps, invented cross-reference part numbers...). Change with care and
keep the worked examples.

Design contract shared by every task:
  * the model judges; the CODE computes and validates every number that can reach a
    counterparty (the sourcing planner returns lot choices, never a price);
  * playbook prose (knowledge/playbooks/*.md) is injected fresh each call, so operators
    steer behaviour by editing markdown;
  * JSON tasks tolerate prose-wrapped output via backend.parse_json.
"""

from __future__ import annotations

import logging
import re

from .. import config
from ..knowledge import playbooks
from . import backend
from .backend import parse_json_safe, strip_fences

log = logging.getLogger("spotdesk.brain")


# ============================================================
# Classification
# ============================================================

EMAIL_TYPES = ("rfq", "po", "negotiation", "vendor_offer", "logistics_update",
               "delivery_query", "technical", "complaint", "spam", "other")


def coerce_email_type(value) -> str:
    """Never let a stray classifier string ('RFQ', 'inquiry') crash the pipeline —
    an unhandled value would leave the mail unread and re-processed forever."""
    v = (str(value or "other")).strip().lower()
    return v if v in EMAIL_TYPES else "other"


def classify_email(subject: str, body: str, from_email: str, learned_context: str = "") -> dict:
    system = f"""You are an email classifier for {config.COMPANY_ENTITY}, a semiconductor component distributor.

Classify the email into EXACTLY ONE of these types:
- rfq: Request for Quotation / Inquiry - customer asking for pricing/availability of parts. Includes emails with part numbers, component descriptions, tables of items, or requests like "please quote", "send pricing". Even DESCRIPTION-ONLY items (no part numbers) are an rfq if the customer wants us to quote.
- po: Purchase Order - customer sending a PO to proceed with an order
- negotiation: Price negotiation - customer countering price or asking for discount
- vendor_offer: A SUPPLIER pushing an UNSOLICITED stock/price OFFER at us - "offer" / "available stock" / daily "DRAM offer" / "stock list" blasts that CONTAIN prices/quantities they are SELLING. Market intel FROM a supplier, NOT a customer asking us to quote. Key tell: the email OFFERS parts with prices rather than ASKING for a price.
- logistics_update: shipment/dispatch information on an order - tracking numbers, AWB, courier details, dispatch confirmations, delivery notifications, customs updates
- delivery_query: customer asking about delivery/shipment status
- technical: technical question about parts, datasheets, specifications
- complaint: complaint, quality issue, return request
- spam: marketing, promotional content, newsletters, generic mass mail
- other: internal chatter, system notifications, automated reports, generic FYI

SUBJECT LINE CLUES: "RFQ", "Request for Quotation", "Inquiry", "Enquiry", "Requirement", "Quote Request", "Please Quote", "Need Quotation" strongly suggest rfq.

IMPORTANT RULES:
- Look at BOTH subject and body. Subject "Inquiry"/"RFQ" + a table of items (even descriptions only) = rfq.
- Customers can write from ANY address; the sender address does NOT decide the class.
- If the body references our quote number ({config.QUOTE_PREFIX}-Q-...), thanks us for a quotation, mentions a target price, or replies to our pricing - it is a CUSTOMER REPLY, NEVER spam. Classify as negotiation (target/counter price), po (order confirmed), or rfq (new parts).
- Customer replies with target prices or counter-offers = negotiation, NOT spam.
- No part numbers AND no component descriptions AND just asking about services = other.
- Spam only for clearly promotional content / newsletters / mass mail with no specific inquiry.
- Daily reports, BI tools, dashboards = other.

Also determine urgency: critical ("production stopped", "line down", "ASAP"), high ("urgent", "rush", short deadlines), normal otherwise.

Respond in this EXACT JSON format only, no other text:
{{"type": "<type>", "urgency": "<urgency>", "confidence": <0.0-1.0>, "reasoning": "<brief reason>"}}

BUSINESS CONTEXT (learned from our real mailbox):
{playbooks.classification_context()}"""
    user = f"From: {from_email}\nSubject: {subject}\n\nBody:\n{(body or '')[:3000]}"
    if learned_context:
        user = learned_context + "\n\n" + user
    out = parse_json_safe(backend.complete(system, user, max_tokens=300), None)
    if not isinstance(out, dict):
        return {"type": "other", "urgency": "normal", "confidence": 0.0, "reasoning": "parse error"}
    out["type"] = coerce_email_type(out.get("type"))
    return out


# ============================================================
# BOM extraction
# ============================================================

def extract_bom(subject: str, body: str, our_domains: list[str] | None = None) -> list[dict]:
    system = """You are a BOM (Bill of Materials) extraction engine for a semiconductor distributor.
Extract ALL part numbers and related information from the email. Tables may be in HTML, plain text, or markdown.

FIELD SYNONYMS - customer column headers vary globally. ALL of these mean the same field:
- MPN: "Part No", "Part Number", "Part#", "P/N", "PN", "MPN", "Mfr Part", "Mfr P/N", "Manufacturer Part Number", "CPN", "Item No", "Item", "Component", "Device", "Model", "Order Code", "Product Code", "Stock No", "Stock Code", "Item Code", "Material No", "Material"
- MANUFACTURER: "Make", "Mfr", "MFG", "Manufacturer", "Brand", "Vendor", "Maker", "OEM", "Origin", "Producer"
- QUANTITY: "Qty", "Quantity", "Pieces", "Pcs", "Nos", "Order Qty", "Required Qty", "Req Qty", "Demand", "Required"
- PACKAGE: "Package", "Pkg", "Case", "Footprint", "PCB Package", "Package Type", "Housing"
- ANNUAL: "Annual", "Annual Qty", "Annual Volume", "Yearly", "EAU", "Forecast"
- DESCRIPTION: "Description", "Desc", "Item Description", "Specification", "Details", "Remarks"
- TARGET PRICE: "Price", "Target", "Target Price", "Budget", "Unit Price", "Expected Price", "Last Price"
- DELIVERY: "Delivery", "Required Date", "Need Date", "Lead Time", "ETA", "Schedule"

CRITICAL RULES:
1. EXTRACT EVERY ROW. If a table has 10 rows, return 10 items. NEVER skip a row.
2. NEVER HALLUCINATE A MANUFACTURER. Use the EXACT text from the Make/Mfr column. If it says "Mornsun", output "Mornsun" - not any other brand. Only infer from the MPN prefix if the column is completely empty AND no manufacturer appears anywhere.
3. NEVER CHANGE QUANTITY. "10000" stays 10000 (count zeros carefully). "10K"=10000, "1K"=1000.
4. DESCRIPTION-ONLY ITEMS: a row with a Description but NO part number gets mpn=null and needs_identification=true. NEVER skip it.
5. ANNUAL vs QUANTITY are DIFFERENT fields - extract both.
6. SPREADSHEET STRUCTURE: BOMs often have TITLE/METADATA rows above the real header ("Bill Of Materials for ...", company name, "Design Title", "Author", "Revision", "Total Parts"). IGNORE those; find the real header row and extract ONLY component rows below it. Ignore side "Totals"/summary tables. NEVER output a metadata label or category name ("Capacitors", "ARYA SYSTEMS") as an MPN.
7. NON-STANDARD PART-NUMBER COLUMNS: the part number may sit under "Stock Code"/"Stock No" or even the "Value" column. With Category + Value + Stock Code, use Stock Code as mpn and build description from Category + Value (+ Package).
8. AN MPN IS AN ALPHANUMERIC CODE (letters+digits, often dashes/slashes: "1N4007", "BC337-25BK", "CC1206KRX7R9BB104", "IRF1407"). A plain VALUE ("100n", "10k", "470uF", "22pF", "10R") is NOT an MPN - it belongs in description. Only set mpn=null when the row genuinely has no code-like part number anywhere.
9. REPLIES & FORWARDS - READ THE WHOLE THREAD, BUT TELL WHOSE PARTS ARE WHOSE. Decide which case applies:
   (a) The real request sits in a FORWARDED/QUOTED message from the CUSTOMER or a third party (a colleague forwards a customer's RFQ). Then the parts further down ARE what to extract.
   (b) The customer is REPLYING on top of an OFFER WE sent (the quoted message's From: is one of OUR OWN domains, listed below). Those quoted parts/prices are OURS, not their request. Extract ONLY what the customer now asks for in their own words; NEVER treat a price from our own quoted offer as their target price.
   Use your own judgement from who sent the quoted text and what the sender asks. Do not mechanically pull every part number in the body.

WORKED EXAMPLE (6-column RFQ table):
Description                        | Manufacturer Part Number | Manufacturer              | Quantity | Package   | Annual
DIODE GEN PURP 1KV 1A SOD123FL    | 1N4007FL                 | SMC Diode Solutions        | 2200     | SOD-123F  | 2000000
CMC 10MH 200MA 2LN TH             |                          | Sumida America Components  | 100      | TH        | 10000

Correct output:
[
  {"mpn": "1N4007FL", "manufacturer": "SMC Diode Solutions", "quantity": 2200, "package": "SOD-123F", "annual_quantity": 2000000, "needs_identification": false, "target_price": null, "currency": null, "required_date": null, "special_requirements": null, "description": "DIODE GEN PURP 1KV 1A SOD123FL"},
  {"mpn": null, "manufacturer": "Sumida America Components", "quantity": 100, "package": "TH", "annual_quantity": 10000, "needs_identification": true, "target_price": null, "currency": null, "required_date": null, "special_requirements": null, "description": "CMC 10MH 200MA 2LN TH"}
]

Per part: mpn (exact code or null) / manufacturer (exact text) / quantity (int) / package / annual_quantity / needs_identification / target_price (number) / currency / required_date / special_requirements (RoHS, AEC-Q100...) / description (exact text if given).

Respond with a JSON array ONLY, no other text, no markdown fences. If no parts found, return: []"""
    body_for_model = (body or "")[:100000]
    our_note = ""
    doms = ", ".join(d for d in (our_domains or []) if d)
    if doms:
        our_note = (f"\nOUR OWN email domains: {doms}. (Per rule 9b: if the customer is replying on "
                    f"top of an offer WE sent, extract only what they now ask for.)\n")
    user = (f"Subject: {subject}\n{our_note}\n"
            f"Content (email body or attachment text) — extract EVERY row, do not stop early:\n"
            f"{body_for_model}")
    out = parse_json_safe(backend.complete(system, user, heavy=True, max_tokens=16000), [])
    return out if isinstance(out, list) else []


def extract_customer_info(from_field: str, body: str) -> dict:
    system = """Extract the customer's name and company from the email information provided.
Respond in this EXACT JSON format only: {"name": "First Last", "company": "Company Name", "designation": "Title if found"}
Use null for anything you cannot determine."""
    user = f"From field: {from_field}\nEmail body (first 500 chars): {(body or '')[:500]}"
    out = parse_json_safe(backend.complete(system, user, max_tokens=150), {})
    return out if isinstance(out, dict) else {}


# ============================================================
# Vendor replies (cost quotes) + vendor offers
# ============================================================

def extract_pricing_from_reply(reply_body: str, known_mpns: list[str], currency: str = "USD") -> dict:
    system = f"""You are a pricing extraction assistant for a semiconductor distributor.
You are reading an email that provides prices per part number (a vendor quoting COST, or a team member providing prices). Formats vary: tables, lists, inline text.

For each part number extract:
- mpn: the part number
- unit_price: the price PER PIECE (number only)
- lead_time: if mentioned
- moq: minimum order qty (int) if mentioned
- spq: standard pack qty (int) if mentioned
- available_qty: the quantity the sender can actually SUPPLY — the SUPPLY CAP. Vendors label it many ways; treat ALL of these as the offered supply quantity: "Offered Qty", "Quoted Qty", "Supported Qty", "Available", "In Stock", "Stock", "We can offer/supply", "Can support", and bare "Qty"/"Pcs"/"Nos"/"Units" next to a number. Read the NUMBER, stripping commas and unit words ("8,000 pcs" -> 8000, "10k nos" -> 10000). If they quote LESS than the requested quantity ("8000 against your 10000"), that 8000 IS available_qty. Null ONLY if they can cover the full ask or state no limit. This is a SUPPLY CAP — not MOQ (a minimum) and not SPQ (a pack multiple).
- packaging: "Tape & Reel"/"T&R"/"Reel"/"Cut Tape"/"Tray"/"Tube"/"Bulk" if stated, else null
- date_code: e.g. "DC 2340", "23+" if stated, else null
- validity: how long the price holds if stated — "48 hours", "7 days", "valid till 2026-07-10", "subject to prior sale" — else null
- notes: conditions, stock status, remarks

ALSO detect the CURRENCY ("USD"/"$", "EUR"/"€", "INR"/"Rs."/"₹"). Default: "{currency}".

Known MPNs from the RFQ: {', '.join(known_mpns)}
Match MPNs to the known list — they may appear slightly different in the reply.

Respond ONLY with this JSON:
{{"items": [{{"mpn": "...", "unit_price": 1.25, "lead_time": "4-6 weeks", "moq": 1000, "spq": 500, "available_qty": null, "packaging": "Tape & Reel", "date_code": "2340", "validity": "48 hours", "notes": "ex-stock"}}],
 "has_pricing": true, "currency": "USD", "pricing_notes": "overall notes"}}
If the email contains NO pricing (status update, question, FYI, bounce), respond:
{{"items": [], "has_pricing": false, "currency": null, "pricing_notes": "why"}}

MARKET CONVENTIONS (learned from our real mailbox):
{playbooks.extraction_context()}"""
    user = (f"Email reply:\n\n{(reply_body or '')[:3000]}\n\n"
            f"Extract pricing for these MPNs: {', '.join(known_mpns)}\nCurrency: {currency}")
    out = parse_json_safe(backend.complete(system, user, heavy=True, max_tokens=2500), None)
    if not isinstance(out, dict):
        # Non-fatal: usually a non-pricing reply (bounce / FYI). Quietly "no pricing".
        return {"items": [], "has_pricing": False, "pricing_notes": "no pricing content"}
    return out


def extract_vendor_offer(subject: str, body: str) -> dict:
    system = f"""You are reading a SUPPLIER's unsolicited STOCK OFFER email — a vendor pushing parts they have TO SELL, WITH prices. Extract every offered line (there may be many).

{playbooks.extraction_context()}

Per offered part: mpn (exact) / manufacturer / unit_price (per piece, number) / currency (default USD) / date_code ("25+") / lead_time / moq (int) / quantity (available, int) / packaging (Tray/Reel/Tube).

Respond ONLY as JSON:
{{"items": [{{"mpn": "MT41K128M16JT-125AAT:K", "manufacturer": "Micron", "unit_price": 5.20, "currency": "USD", "date_code": "25+", "lead_time": "2-3 days", "moq": 2000, "quantity": 6000, "packaging": "Reel"}}]}}
If this is NOT actually an offer listing parts with prices, respond {{"items": []}}."""
    user = f"Subject: {subject}\n\nVendor offer email:\n{(body or '')[:8000]}"
    out = parse_json_safe(backend.complete(system, user, heavy=True, max_tokens=4000), {})
    return {"items": (out or {}).get("items") or []}


def match_market_offers(items: list[dict], offers: list[dict]) -> dict:
    """Given a customer's inquiry lines and the stock vendors recently OFFERED us,
    decide who to approach first — the way a trader who reads the daily offer sheets
    would. Deliberately NOT an equality lookup: grade suffixes, speed bins, equivalents
    and 'that vendor blasts DRAM every morning' all count. Judgement is the model's."""
    if not items or not offers:
        return {}
    parts_text = "".join(
        f"{i}. MPN={it.get('mpn') or '(none)'} | Manufacturer={it.get('manufacturer') or '(none)'} | "
        f"Qty={it.get('quantity') or '?'} | Description={it.get('description') or ''}\n"
        for i, it in enumerate(items))
    offers_text = "".join(
        f"- {o.get('vendor')}: {o.get('mpn')} [{o.get('manufacturer') or '?'}] "
        f"qty {o.get('qty') or '?'} @ {o.get('currency') or 'USD'} {o.get('price')} "
        f"D/C {o.get('date_code') or '?'} lead {o.get('lead_time') or '?'} "
        f"({o.get('age_days')} days ago)\n"
        for o in offers)
    system = f"""You are a spot-market trader at {config.COMPANY_ENTITY}.

Every day your vendor panel emails you stock offer sheets. A customer enquiry has just come in. Before blasting an RFQ to the whole panel, check what you have ALREADY been offered — a vendor who offered the part last week very likely still has it, at a price you already know.

STOCK RECENTLY OFFERED TO US:
{offers_text[:9000]}

Work through each enquiry line and decide whether anything above is worth acting on. Use your own judgement about what counts as a usable match — part numbering, grade suffixes, speed bins, packaging codes, second-source equivalents, and which vendors specialise in what. A vendor who sends a DRAM sheet every morning is a good first call for a DRAM part even if today's sheet doesn't show it.

Be honest about staleness: an offer more than a few weeks old is a lead to re-confirm, not a price to quote — say so in `why`.

Per enquiry line:
- offers: the useful ones, each {{vendor, mpn, relation, why}} with relation = "same_part" | "equivalent" | "same_vendor_category"
- approach_first: vendor names to RFQ ahead of the wider panel, best first
- note: one line for the salesperson

Respond ONLY with a JSON object keyed by enquiry index as a string. Omit any index where the offer history holds nothing genuinely useful. Do not stretch for a match."""
    out = parse_json_safe(backend.complete(system, f"Customer enquiry lines:\n{parts_text}",
                                           heavy=True, max_tokens=6000), {})
    return out if isinstance(out, dict) else {}


# ============================================================
# Sourcing plan (the AI buyer) — lots + quantities only, never a price
# ============================================================

def plan_sourcing(mpn: str, description: str, manufacturer: str, req_qty: int,
                  lots: list[dict], market: dict | None = None) -> dict | None:
    """Decide the sourcing plan for ONE part the way a human buyer would — which vendor
    lot(s), how much from each, hold-for-review, shortage. Returns ONLY lot indices +
    quantities + flags; the caller VALIDATES the plan and computes every price itself.
    None on any failure → deterministic fallback."""
    if not lots:
        return None
    lines = []
    for l in lots:
        cap = l.get("offered_qty")
        lines.append(f'Lot {l["lot"]}: cost USD {l.get("cost_usd")}/pc, '
                     f'can supply {cap if cap else "the full quantity"}, '
                     f'date code {l.get("date_code") or "n/a"}, packaging {l.get("packaging") or "n/a"}, '
                     f'lead {l.get("lead_time") or "n/a"}, vendor trust {l.get("trust", "n/a")}')
    mkt = ""
    if market:
        mkt = (f'\nLive market (distributor): {market.get("in_stock", "?")} in stock, '
               f'factory lead {market.get("lead_time") or "n/a"}.')
    system = """You are an experienced semiconductor-distribution BUYER deciding how to source ONE part for a customer order. You are given the EXACT vendor lots available (already cost-normalised to USD) and the quantity needed.

Principles:
- Prefer buying the whole quantity from ONE vendor (a single clean PO). BUT if the cheapest single vendor covering the full quantity is MATERIALLY more expensive than combining a couple of cheaper lots, using more than one lot to save real money is fine — judge whether the premium is worth avoiding a split.
- You may ONLY use the lots listed. Never invent lots, never exceed a lot's available quantity, never state any price — you only choose lot numbers and quantities.
- If your plan uses more than one different SUPPLIER, set hold_for_review to true (a human confirms a multi-supplier buy).
- Judge "shortage" from the live market data and the kind of part — no fixed rule. Out of stock everywhere or a very long factory lead = shortage.

Return ONLY strict JSON, no prose:
{"allocation": [{"lot": <int>, "qty": <int>}, ...], "hold_for_review": <bool>, "shortage": <bool>, "reason": "<one short sentence>"}"""
    user = (f"Part: {mpn} — {description or ''} ({manufacturer or 'make n/a'})\n"
            f"Quantity needed: {req_qty}\n\nAvailable vendor lots:\n" + "\n".join(lines) + mkt +
            "\n\nReturn the sourcing plan JSON.")
    try:
        plan = parse_json_safe(backend.complete(system, user, heavy=True, max_tokens=600), None)
        return plan if isinstance(plan, dict) and plan.get("allocation") else None
    except Exception as e:
        log.warning("plan_sourcing failed for %s: %s", mpn, e)
        return None


# ============================================================
# Negotiation
# ============================================================

def extract_target_prices(subject: str, body: str, known_mpns: list[str],
                          currency: str = "USD") -> dict:
    system = f"""You are a pricing extraction assistant for a semiconductor distributor.
You are reading a CUSTOMER email where they negotiate/counter our quotation prices. Formats vary: tables, lists, inline text.

Per part number extract: mpn / current_price (what WE quoted, if mentioned) / target_price (what the CUSTOMER wants) / quantity (they may have changed it) / notes.

Known MPNs from our quotation: {', '.join(known_mpns)}
Detect the CURRENCY ("INR"/"Rs."/"₹", "USD"/"$", "EUR"/"€"). Default: {currency}.

IMPORTANT:
- Match MPNs to the known list even if slightly different.
- "target price", "our budget", "expected price", "best price", "last buy price" all mean target.
- "please check" / "can you match" next to a price — that IS the target price.
- Extract ALL items mentioned, not just ones with explicit targets.

Respond ONLY with:
{{"items": [{{"mpn": "...", "current_price": 12.0, "target_price": 10.5, "quantity": 2200, "notes": "..."}}],
 "has_targets": true, "currency": "USD", "customer_message": "brief summary"}}
If no targets found: {{"items": [], "has_targets": false, "currency": null, "customer_message": "summary"}}"""
    user = (f"Subject: {subject}\n\nCustomer's negotiation email:\n{(body or '')[:3000]}\n\n"
            f"Extract target prices for: {', '.join(known_mpns)}")
    out = parse_json_safe(backend.complete(system, user, heavy=True, max_tokens=2000), None)
    if not isinstance(out, dict):
        return {"items": [], "has_targets": False, "customer_message": "parse failure"}
    return out


# ============================================================
# Vendor PO replies + logistics
# ============================================================

def classify_po_response(subject: str, body: str, po_number: str = "") -> dict:
    system = f"""You are reading a VENDOR's reply to a PURCHASE ORDER that WE (the buyer) sent them.
This is a SUPPLIER responding to OUR order {po_number} — NOT a customer placing an order with us.

Return exactly ONE status:
- "confirmed": accepts / confirms / acknowledges and will supply.
- "dispatched": has shipped / dispatched (mentions courier, tracking, AWB, dispatch invoice).
- "delivered": goods delivered / received.
- "declined": CANNOT supply — no stock, discontinued/EOL, MOQ not met, price changed and won't honour, refuses/cancels.
- "unclear": a question, info request, partial, or anything not confidently mappable.

Respond ONLY as JSON:
{{"status": "confirmed|dispatched|delivered|declined|unclear", "reason": "<short paraphrase>"}}"""
    user = f"Subject: {subject}\n\nVendor's reply:\n{(body or '')[:3000]}"
    out = parse_json_safe(backend.complete(system, user, heavy=True, max_tokens=300), {})
    status = (out.get("status") or "unclear").strip().lower() if isinstance(out, dict) else "unclear"
    if status not in ("confirmed", "dispatched", "delivered", "declined", "unclear"):
        status = "unclear"
    return {"status": status, "reason": (out.get("reason") or "").strip() if isinstance(out, dict) else ""}


def extract_shipment_info(subject: str, body: str) -> dict:
    """Pull tracking facts out of a dispatch / logistics email."""
    system = """You are reading a shipment/dispatch email on a components order (from a vendor, courier, or freight forwarder). Extract the logistics facts.

Respond ONLY as JSON (null for anything not stated):
{"carrier": "DHL", "awb": "1234567890", "incoterm": "EXW", "origin": "Hong Kong", "destination": null, "eta": "2026-09-04", "event": "dispatched", "notes": "<one line>"}
`event` is one of: dispatched | in_transit | customs | delivered | exception | unknown."""
    user = f"Subject: {subject}\n\nEmail:\n{(body or '')[:3000]}"
    out = parse_json_safe(backend.complete(system, user, max_tokens=300), {})
    return out if isinstance(out, dict) else {}


def estimate_transit_days(country: str) -> float | None:
    """Typical inbound door-to-door transit (days) from `country` to Singapore on
    cost-effective freight, the way a Singapore trader judges it. Cached by caller."""
    system = ("You are a Singapore-based electronics trading desk sourcing on cost. For the given "
              "ORIGIN, give the realistic DOOR-TO-DOOR inbound transit to Singapore in DAYS using our "
              "usual COST-EFFECTIVE freight (sea / consolidated LCL by default, air only when nothing "
              "else works), INCLUDING booking, port handling, sailing and customs clearance. "
              "Calibration anchor: China ≈ 14 days door-to-door this way; Hong Kong a bit less; "
              "Europe / US more. Singapore itself is 0. Answer with ONLY an integer number of days.")
    out = backend.complete(system, f"Origin country: {country}\nInbound transit to Singapore (days):",
                           max_tokens=50)
    m = re.search(r"\d+(?:\.\d+)?", out or "")
    return float(m.group(0)) if m else None


# ============================================================
# Drafting (customer-facing prose; tables are built deterministically in code)
# ============================================================

def _human_rules() -> str:
    return (f"Write entirely as a human member of the {config.COMPANY_NAME} sales team. "
            f"NEVER state, hint, or imply this email was written or sent by an AI, bot, agent, "
            f"or automation; never add 'automated message' / 'auto-generated' / 'do not reply' notes. "
            f"Use a plain hyphen, never an em-dash.\n\n{playbooks.reply_style()}")


def draft_acknowledgement(customer_name: str, customer_company: str, items_summary: str) -> str:
    system = f"""You are drafting a professional acknowledgement email on behalf of {config.COMPANY_ENTITY}, a semiconductor component distributor.

Rules:
- Warm, professional, concise. Acknowledge receipt; list the requested items briefly; promise pricing shortly (we're confirming with our sources).
- Do NOT include pricing or availability. Do NOT promise stock (be honest about supply).
- Output HTML formatted email body only (no subject line).
{_human_rules()}"""
    user = (f"Customer: {customer_name} from {customer_company or 'their company'}\n"
            f"Items requested:\n{items_summary}\n\nDraft the acknowledgement email body in HTML.")
    return strip_fences(backend.complete(system, user, heavy=True, max_tokens=900))


def draft_cold_email_intro(company: str, context_note: str = "") -> str:
    """One short cold-intro paragraph (the offer table + signature are code-built)."""
    system = f"""You write ONE short opening line for a spot-stock offer email from {config.COMPANY_ENTITY}.
Follow the outreach playbook exactly — the locked body starts "Hi," then one warm line, then the stock table (built elsewhere).
Return ONLY that one warm line, plain text, no greeting, no signature.
{playbooks.outreach_playbook()[:2000]}"""
    user = f"Recipient company: {company}. {context_note}"
    try:
        line = backend.complete(system, user, max_tokens=100).strip().strip('"')
        return line or "I'm glad to share today's hot offer to you. Hope this will be helpful to you:"
    except Exception:
        return "I'm glad to share today's hot offer to you. Hope this will be helpful to you:"
