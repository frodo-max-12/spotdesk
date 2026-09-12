"""Pricing — the Quote Analyst role: consolidation + margin engine + margin guard.

Consolidation mirrors how a spot desk builds a quote by hand:
  * every vendor cost is normalized to USD (FX for INR vendors) and compared;
  * the cheapest wins; on a tie the shorter lead time wins ("2 Days" beats "1 Week" —
    units normalized, not string-sorted);
  * an AI buyer may propose the allocation (which lots, how much from each) from
    grounded facts — but the CODE validates the plan and computes every price; an
    unsafe plan falls back to deterministic rules;
  * a vendor's supply cap splits the line across vendors only as a last resort, and a
    cross-supplier split is always held for human review;
  * same part offered in different DATE CODES quotes as separate lines (the customer
    prices and buys by date code — never blend them);
  * safety flags on every line: packaging homogeneity, quote expiry, EUD dual-use,
    too-cheap authenticity, price-anomaly vs the desk median.

The margin engine turns cost → resale; the MARGIN GUARD enforces the one bug a desk
cannot afford — quoting below true landed cost:

    landed_floor = usd_cost × (1 + expenses% + finance% + safety%)
    resale       = usd_cost × (1 + expenses%) × (1 + margin%)   and resale ≥ floor
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import config, db
from ..knowledge import compliance
from ..knowledge.line_card import brand_match
from ..llm import brain
from ..market import fx as fx_mod
from ..market import history, lookup as market_lookup

log = logging.getLogger("spotdesk.pricing")


# ============================================================
# Small parsers shared across the pipeline
# ============================================================

def to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def to_int(v) -> int | None:
    try:
        if v in (None, ""):
            return None
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def lead_days(text) -> float | None:
    """'8 weeks' / '4-6 wks' / '10 days' / 'ex-stock' → days, or None."""
    if not text:
        return None
    t = str(text).strip().lower()
    if any(w in t for w in ("stock", "ready", "immediate", "available")):
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?(week|wk|day|month|mon)", t)
    if not m:
        return None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    val = (lo + hi) / 2.0
    unit = m.group(3)
    if unit.startswith("day"):
        return val
    if unit.startswith("mon"):
        return val * 30.0
    return val * 7.0


def norm_pkg(p) -> str | None:
    """Canonical packaging token so split legs compare for homogeneity. Unknown/blank
    stays None (never a false mismatch)."""
    if not p or not str(p).strip():
        return None
    s = str(p).strip().lower()
    if "cut" in s and "tape" in s:
        return "cut_tape"
    if any(w in s for w in ("reel", "t&r", "t & r", "tape and reel", "tape&reel", "emboss")):
        return "reel"
    if "tray" in s:
        return "tray"
    if "tube" in s:
        return "tube"
    if any(w in s for w in ("bulk", "loose", "bag")):
        return "bulk"
    if "ammo" in s:
        return "ammo"
    if s in ("tape", "tape only"):
        return "reel"
    return None


def parse_validity(text, base_dt: datetime | None = None) -> str | None:
    """Vendor validity phrase → ISO expiry. Relative ('48 hours', '7 days') and
    absolute ('till 2026-07-10') forms; None if open-ended."""
    if not text or not str(text).strip():
        return None
    s = str(text).strip().lower()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            23, 59).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hour|hr|day|week|month)", s)
    if m:
        from datetime import timedelta
        base = base_dt or datetime.now(timezone.utc).replace(tzinfo=None)
        if getattr(base, "tzinfo", None):
            base = base.replace(tzinfo=None)
        hours = {"hour": 1, "hr": 1, "day": 24, "week": 168, "month": 720}[m.group(2)]
        return (base + timedelta(hours=float(m.group(1)) * hours)).isoformat()
    return None


# Internal wording that must NEVER surface on a customer quote (a vendor replying on a
# negotiation re-RFQ often quotes our own "target buy … match or beat" line back at us).
_INTERNAL_REMARK_MARKERS = (
    "target buy", "buy cost", "match or beat", "please match", "margin now", "margin =",
    "quoted at target", "re-sourcing", "re-source", "our target", "target cost",
    "target price was", "target buy price",
)


def is_internal_remark(text) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _INTERNAL_REMARK_MARKERS)


# ============================================================
# Margin engine + margin guard
# ============================================================

def resolve_margin_percent(category: str | None = None, brand: str | None = None) -> tuple[float, str]:
    overrides = config.margin_overrides()
    if category and category.strip().lower() in overrides:
        return overrides[category.strip().lower()], f"category:{category}"
    if brand and brand.strip().lower() in overrides:
        return overrides[brand.strip().lower()], f"brand:{brand}"
    return config.DEFAULT_MARGIN_PERCENT, "default"


def landed_floor(usd_cost: float) -> float:
    """The never-quote-below-this line: cost + freight + finance + safety fudge."""
    return usd_cost * (1 + (config.DEFAULT_EXPENSES_PERCENT
                            + config.FINANCE_BUFFER_PCT
                            + config.SAFETY_FUDGE_PCT) / 100.0)


def compute_resale(cost, *, cost_currency: str = "USD", sale_currency: str = "USD",
                   fx_rate: float | None = None, brand: str | None = None,
                   category: str | None = None,
                   margin_percent_override: float | None = None) -> dict | None:
    """Vendor COST → customer RESALE in USD (ex-tax).

    Returns None for an invalid cost, {"blocked": ...} when it can't price safely
    (non-USD sale, or a non-USD cost with no FX) — the caller marks the line
    needs_review, never auto-quotes — else a full breakdown with `resale`, floored at
    the margin guard's landed floor."""
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(cost) or cost <= 0:
        return None
    cost_currency = (cost_currency or "USD").upper()
    sale_currency = (sale_currency or "USD").upper()

    if sale_currency != "USD":
        return {"blocked": "currency",
                "reason": f"{sale_currency} sale — auto-pricing is USD only"}

    if cost_currency == "USD":
        usd_cost = cost
    elif cost_currency == "INR":
        if not fx_rate:
            return {"blocked": "no_fx", "reason": "INR→USD rate unavailable"}
        usd_cost = cost / float(fx_rate)      # fx_rate is USD→INR
    else:
        return {"blocked": "currency",
                "reason": f"{cost_currency} vendor cost — no configured conversion"}

    if margin_percent_override is not None:
        try:
            margin_pct, margin_reason = float(margin_percent_override), "deal-override"
        except (TypeError, ValueError):
            margin_pct, margin_reason = resolve_margin_percent(category, brand)
    else:
        margin_pct, margin_reason = resolve_margin_percent(category, brand)

    expenses_pct = config.DEFAULT_EXPENSES_PERCENT
    landed = usd_cost * (1 + expenses_pct / 100.0)
    resale = landed * (1 + margin_pct / 100.0)

    # MARGIN GUARD — the floor wins over the margin math. If a tiny margin override
    # would put resale under the landed floor, lift to the floor and say so.
    floor = landed_floor(usd_cost)
    guard_note = None
    if resale < floor:
        guard_note = (f"margin guard lifted resale from {resale:.4f} to floor {floor:.4f}")
        resale = floor
        margin_pct = round((resale / landed - 1.0) * 100.0, 2)

    return {"mode": "usd" if cost_currency == "USD" else "usd_import",
            "currency": "USD", "cost": round(cost, 4), "cost_currency": cost_currency,
            "fx_rate": (float(fx_rate) if cost_currency != "USD" and fx_rate else None),
            "usd_cost": round(usd_cost, 4), "expenses_percent": expenses_pct,
            "landed_cost": round(landed, 4), "landed_floor": round(floor, 4),
            "margin_percent": margin_pct, "margin_reason": margin_reason,
            "guard_note": guard_note, "resale": round(resale, 4)}


# ============================================================
# Allocation
# ============================================================

@dataclass
class Lot:
    """One priced vendor candidate for a line."""
    landed: float
    calc: dict
    quote: dict                      # the vendor_quotes row (as dict)
    trust: int | None = None

    @property
    def cap(self) -> int | None:
        oq = self.quote.get("offered_qty")
        return oq if (oq and oq > 0) else None


def deterministic_allocation(lots: list[Lot], req_qty: int) -> list[tuple[int | None, Lot]]:
    """Prefer the cheapest SINGLE vendor covering the full quantity; split across
    vendors (cheapest-first) only as a last resort. `lots` must be cheapest-first."""
    if req_qty <= 0:
        return [(None, lots[0])]
    for lot in lots:
        if lot.cap is None or lot.cap >= req_qty:
            return [(req_qty, lot)]
    allocations: list[tuple[int | None, Lot]] = []
    remaining = req_qty
    for lot in lots:
        if remaining <= 0:
            break
        take = remaining if lot.cap is None else min(remaining, lot.cap)
        if take <= 0:
            continue
        allocations.append((take, lot))
        remaining -= take
    return allocations


def validate_llm_plan(plan: dict, lots: list[Lot], req_qty: int) -> list[tuple[int, Lot]] | None:
    """Turn an AI sourcing plan into allocations IFF it is legal, else None →
    deterministic fallback. Gates (each on a fact, not a judgment): every lot index
    exists, none repeated, qty > 0 and ≤ that lot's cap, total ≤ requirement."""
    alloc = plan.get("allocation") if isinstance(plan, dict) else None
    if not isinstance(alloc, list) or not alloc:
        return None
    out: list[tuple[int, Lot]] = []
    seen: set[int] = set()
    total = 0
    for a in alloc:
        if not isinstance(a, dict):
            return None
        li, q = a.get("lot"), a.get("qty")
        if not isinstance(li, int) or li < 0 or li >= len(lots) or li in seen:
            return None
        try:
            q = int(q)
        except (TypeError, ValueError):
            return None
        if q <= 0:
            return None
        lot = lots[li]
        if lot.cap is not None and q > lot.cap:
            return None
        seen.add(li)
        out.append((q, lot))
        total += q
    if req_qty > 0 and total > req_qty:
        return None                              # never quote more than asked
    out.sort(key=lambda x: x[1].landed)          # cheapest leg first → primary lot
    return out


# ============================================================
# Lead-time composition (customer sees EXW <our hub>, inbound transit included)
# ============================================================

def _transit_days(country: str | None) -> float:
    if not country or "singapore" in str(country).lower():
        return 0.0
    row = db.query_one("SELECT last_email_date FROM ingest_state WHERE source = ?",
                       (f"transit:{country.strip().lower()}",))
    if row and row.get("last_email_date"):
        try:
            return float(row["last_email_date"])
        except (TypeError, ValueError):
            pass
    days = None
    try:
        days = brain.estimate_transit_days(country)
    except Exception as e:
        log.warning("transit estimate failed for %s: %s", country, e)
    if days is None:
        days = config.TRANSIT_WEEKS_FALLBACK * 7.0
    db.execute("INSERT INTO ingest_state (source, last_email_date) VALUES (?, ?) "
               "ON CONFLICT(source) DO UPDATE SET last_email_date = excluded.last_email_date",
               (f"transit:{country.strip().lower()}", str(days)))
    return float(days)


def customer_lead(vendor_lead: str | None, vendor_country: str | None) -> str | None:
    """Vendor lead + inbound transit → the lead time the customer will actually
    experience ('1 week EXW China' becomes a realistic EXW-Singapore lead)."""
    base = lead_days(vendor_lead)
    transit = _transit_days(vendor_country)
    if base is None:
        if transit <= 0:
            return vendor_lead
        total = transit
    else:
        total = base + transit
    if total >= 7:
        wk = max(1, int(round(total / 7.0)))
        return f"{wk} week{'s' if wk != 1 else ''}"
    return f"{int(round(total))} days"


def combined_lead(leads: list) -> str | None:
    """For a split line the effective lead is the WORST leg."""
    vals = [l for l in leads if l and str(l).strip()]
    if not vals:
        return None
    return max(vals, key=lambda s: (lead_days(s) or 0)) if len(vals) > 1 else vals[0]


# ============================================================
# Consolidation — the main act
# ============================================================

def _match_remark(vq: dict, item: dict) -> str:
    notes = (vq.get("notes") or "").strip()
    if is_internal_remark(notes):
        notes = ""
    low = notes.lower()
    if any(w in low for w in ("alternate", "alternative", "equivalent", "cross", "eol", "verify")):
        return notes[:120]
    req = (item.get("manufacturer") or "").strip()
    off = (vq.get("manufacturer") or "").strip()
    if req and off and not brand_match(req, [off]):
        return f"Quoted {off} p/n"
    return notes[:120] if notes else "Exact P/N"


def _vendor_trust(email: str | None) -> int | None:
    if not email:
        return None
    row = db.query_one("SELECT trust_score FROM counterparties WHERE email = ?",
                       (email.lower(),))
    return row.get("trust_score") if row else None


@dataclass
class ConsolidationSummary:
    priced: int = 0
    unpriced: int = 0
    no_bid: int = 0
    partial: int = 0
    pkg_mismatch: int = 0
    expired: int = 0
    eud_flagged: int = 0
    split_review: int = 0
    anomaly: int = 0
    currency_skipped: int = 0
    total_amount: float = 0.0
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("priced", "unpriced", "no_bid", "partial", "pkg_mismatch", "expired",
                 "eud_flagged", "split_review", "anomaly", "currency_skipped",
                 "total_amount")}


def consolidate_deal(deal: dict, use_llm_planner: bool | None = None) -> ConsolidationSummary:
    """Consolidate every vendor quote on a deal into priced line items; writes the
    updated line_items + decision snapshot back to the deals row."""
    deal_id = deal["id"]
    quote_ccy = (deal.get("currency") or "USD").upper()
    margin_override = deal.get("margin_percent_override")
    use_llm = config.CONSOLIDATION_LLM_PLANNING if use_llm_planner is None else use_llm_planner

    vquotes = db.query("SELECT * FROM vendor_quotes WHERE deal_id = ?", (deal_id,))
    by_mpn: dict[str, list[dict]] = {}
    for vq in vquotes:
        by_mpn.setdefault((vq.get("mpn") or "").strip().upper(), []).append(vq)
    db.execute("UPDATE vendor_quotes SET is_selected = 0 WHERE deal_id = ?", (deal_id,))

    # MPNs still awaiting a vendor: pending (a price may come), not no_bid.
    awaited: set[str] = set()
    for r in db.query("SELECT status, requested_mpns FROM vendor_rfqs WHERE deal_id = ?",
                      (deal_id,)):
        if r["status"] not in ("replied", "no_bid"):
            for m in db.uj(r.get("requested_mpns"), []) or []:
                awaited.add((m or "").strip().upper())

    fx = fx_mod.get_effective_rate()
    items = [dict(it) for it in (db.uj(deal.get("line_items"), []) or [])]
    prior_priced = {(it.get("mpn") or "").strip().upper()
                    for it in items if it.get("unit_price") is not None}
    is_followup = bool(prior_priced)

    country_by_email = {r["email"]: r.get("country") for r in db.query(
        "SELECT email, country FROM counterparties WHERE kind IN ('vendor','both')")}

    out_items: list[dict] = []
    s = ConsolidationSummary()
    selected_ids: list[int] = []
    snapshot_lines: list[dict] = []

    for it in items:
        key = (it.get("mpn") or "").strip().upper()

        # A negotiated line (margin trimmed to a customer target) stays as agreed.
        if it.get("negotiated") and it.get("unit_price") is not None:
            s.priced += 1
            out_items.append(it)
            continue

        req_qty = to_int(it.get("quantity")) or 0
        candidates = [c for c in by_mpn.get(key, []) if c.get("cost_price")]

        # Manual pin: a human chose a vendor for this line — honour it.
        pinned_id = it.get("pinned_vendor_quote_id")
        if pinned_id is not None:
            pinned = [c for c in candidates if c["id"] == pinned_id]
            if pinned:
                candidates = pinned
                it["vendor_pinned"] = True
            else:
                it.pop("pinned_vendor_quote_id", None)
                it["vendor_pinned"] = False

        if not candidates:
            if key in awaited:
                it.update(unit_price=None, cost_price=None, selected_vendor=None,
                          fulfillment=None, pricing_status="pending",
                          remark="Pending", newly_priced=False)
            else:
                it.update(unit_price=None, cost_price=None, selected_vendor=None,
                          fulfillment=None, pricing_status="no_bid",
                          remark="NO BID", newly_priced=False)
                s.no_bid += 1
            s.unpriced += 1
            out_items.append(it)
            continue

        # Price every candidate to a USD landed cost so mixed currencies compare fairly.
        lots: list[Lot] = []
        blocked_reason = None
        for c in candidates:
            c_ccy = (c.get("currency") or quote_ccy).upper()
            calc = compute_resale(c["cost_price"], cost_currency=c_ccy,
                                  sale_currency=quote_ccy,
                                  fx_rate=fx if c_ccy == "INR" else None,
                                  brand=it.get("manufacturer"),
                                  margin_percent_override=margin_override)
            if not calc or calc.get("blocked"):
                blocked_reason = (calc or {}).get("reason") or "invalid vendor cost"
                continue
            lots.append(Lot(landed=calc["landed_cost"], calc=calc, quote=c,
                            trust=_vendor_trust(c.get("vendor_email"))))
        if not lots:
            it.update(unit_price=None, pricing_status="needs_review",
                      remark="needs review",
                      pricing_note=blocked_reason or "vendor currency needs FX/review")
            s.currency_skipped += 1
            s.unpriced += 1
            out_items.append(it)
            continue

        # Cheapest first; tie broken by NORMALIZED lead time ("2 Days" beats "1 Week").
        def _lead_key(lot: Lot):
            d = lead_days(lot.quote.get("lead_time"))
            return d if d is not None else 10 ** 9
        lots.sort(key=lambda l: (round(l.landed, 4), _lead_key(l)))

        # Too-cheap = counterfeit flag (needs ≥3 quotes for a meaningful median).
        auth_flag = False
        if len(lots) >= 3:
            landeds = sorted(l.landed for l in lots)
            m = len(landeds) // 2
            panel_median = landeds[m] if len(landeds) % 2 else (landeds[m - 1] + landeds[m]) / 2
            if panel_median > 0 and lots[0].landed < config.AUTHENTICITY_MEDIAN_RATIO * panel_median:
                auth_flag = True

        # Price-anomaly breaker vs the desk's OWN price memory — a wildly-off price is
        # more likely a parsing artifact than a deal; hold the line.
        from ..ops import breakers
        desk_median = history.median_price(key)
        anomaly_ok, anomaly_reason = breakers.check_anomaly(lots[0].landed, desk_median)

        # --- Allocation: AI buyer proposes, code validates, deterministic fallback ---
        allocations = None
        plan_hold = False
        plan_shortage = None
        if (use_llm and len(lots) > 1 and req_qty > 0
                and not it.get("customer_target_price")):
            try:
                mkt = market_lookup.market_status(key)
                plan = brain.plan_sourcing(
                    it.get("mpn") or "", it.get("description") or "",
                    it.get("manufacturer") or "", req_qty,
                    [{"lot": i, "cost_usd": round(l.landed, 4),
                      "offered_qty": l.cap, "date_code": l.quote.get("date_code"),
                      "packaging": l.quote.get("packaging"),
                      "lead_time": l.quote.get("lead_time"),
                      "vendor": l.quote.get("vendor_name"), "trust": l.trust}
                     for i, l in enumerate(lots)], mkt)
                validated = validate_llm_plan(plan, lots, req_qty) if plan else None
                if validated:
                    allocations = validated
                    plan_hold = bool(plan.get("hold_for_review"))
                    plan_shortage = bool(plan.get("shortage"))
                elif plan is not None:
                    log.warning("[deal %s] AI plan for %s failed validation — deterministic fallback",
                                deal_id, key)
            except Exception as e:
                log.warning("[deal %s] AI planning error for %s: %s", deal_id, key, e)
        if allocations is None:
            allocations = deterministic_allocation(lots, req_qty)

        allocated_qty = sum(q for q, _ in allocations if q)
        shortfall = (req_qty - allocated_qty) if (req_qty > 0 and allocated_qty < req_qty) else 0
        covered = None if req_qty <= 0 else (req_qty - shortfall)
        multi_vendor = len({(l.quote.get("vendor_email") or l.quote.get("vendor_name"))
                            for _, l in allocations}) > 1

        eud_flag, eud_reason = compliance.check_eud(it.get("mpn"), it.get("manufacturer"),
                                                    it.get("description"))
        if eud_flag:
            s.eud_flagged += 1

        snapshot_lines.append({
            "mpn": it.get("mpn"), "req_qty": req_qty,
            "lots": [{"vendor": l.quote.get("vendor_name"), "landed": round(l.landed, 4),
                      "cap": l.cap, "dc": l.quote.get("date_code"), "trust": l.trust}
                     for l in lots],
            "desk_median": desk_median, "auth_flag": auth_flag,
        })

        # --- DATE-CODE SPLIT: different date codes quote as separate customer lines ---
        distinct_dcs = {(l.quote.get("date_code") or "").strip()
                        for _, l in allocations if (l.quote.get("date_code") or "").strip()}
        if len(allocations) > 1 and len(distinct_dcs) > 1 and not it.get("customer_target_price"):
            n_lots = len(allocations)
            for li, (q, lot) in enumerate(allocations, 1):
                selected_ids.append(lot.quote["id"])
                lot_margin = lot.calc.get("margin_percent") or 0.0
                lot_remark = f"Lot {li} of {n_lots} — date code {lot.quote.get('date_code') or 'n/a'}"
                if auth_flag:
                    lot_remark += " · cost far below market — verify CoC / stock-label"
                row = dict(it)
                row.update(
                    quantity=(int(q) if q else req_qty),
                    cost_price=lot.calc.get("usd_cost"),
                    cost_currency=lot.quote.get("currency") or quote_ccy,
                    selected_vendor=lot.quote.get("vendor_name"),
                    fulfillment=[{"vendor": lot.quote.get("vendor_name"),
                                  "qty": (int(q) if q else None),
                                  "landed_cost": round(lot.landed, 4)}],
                    margin_percent=lot_margin,
                    unit_price=round(lot.landed * (1 + lot_margin / 100.0), 4),
                    landed_cost=round(lot.landed, 4),
                    lead_time=customer_lead(lot.quote.get("lead_time"),
                                            country_by_email.get(lot.quote.get("vendor_email"))),
                    moq=lot.quote.get("moq") or it.get("moq"),
                    spq=lot.quote.get("spq") or it.get("spq"),
                    packaging=norm_pkg(lot.quote.get("packaging")),
                    date_code=lot.quote.get("date_code"),
                    quote_valid_until=lot.quote.get("valid_until"),
                    eud_required=bool(eud_flag), eud_reason=eud_reason,
                    authenticity_review=bool(auth_flag),
                    pricing_status="priced", remark=lot_remark,
                    newly_priced=False, date_code_lot=True,
                )
                out_items.append(row)
                s.priced += 1
            tail = out_items[-1]
            if shortfall > 0:
                note = (f"Only {int(covered):,} of {int(req_qty):,} available across "
                        f"date codes ({int(shortfall):,} short) — confirm the balance")
                tail.update(pricing_status="needs_review", remark=tail["remark"] + " · " + note)
                s.partial += 1
            elif multi_vendor or plan_hold:
                note = ("Multiple suppliers needed — confirm sourcing before order"
                        if multi_vendor else "Held for buyer review before quoting")
                tail.update(pricing_status="needs_review", remark=tail["remark"] + " · " + note)
                s.split_review += 1
            continue

        # --- Blended single line (single vendor is the degenerate case) ---
        if covered and covered > 0:
            blended = sum((q or 0) * l.landed for q, l in allocations) / covered
        else:
            blended = allocations[0][1].landed
        primary = allocations[0][1]
        margin_pct = primary.calc.get("margin_percent") or 0.0
        resale = round(blended * (1 + margin_pct / 100.0), 4)

        # Negotiation best-price: flex margin within [floor, normal] toward the target.
        ctp = to_float(it.get("customer_target_price"))
        if ctp and blended > 0:
            floor_m = config.NEGOTIATION_MIN_MARGIN_PERCENT
            margin_to_hit = (ctp / blended - 1.0) * 100.0
            margin_pct = round(max(float(floor_m), min(margin_to_hit, margin_pct)), 2)
            resale = round(blended * (1 + margin_pct / 100.0), 4)
            it["negotiated"] = True

        for _, lot in allocations:
            selected_ids.append(lot.quote["id"])

        is_split = len(allocations) > 1
        fulfillment = [{"vendor": l.quote.get("vendor_name"), "qty": (int(q) if q else None),
                        "landed_cost": round(l.landed, 4)} for q, l in allocations]
        remark = ("Consolidated from %d sources" % len(allocations)) if is_split \
            else _match_remark(primary.quote, it)

        raw_lead = combined_lead([l.quote.get("lead_time") for _, l in allocations]) \
            or it.get("lead_time")
        lead = customer_lead(raw_lead,
                             country_by_email.get(primary.quote.get("vendor_email"))) or raw_lead

        pkgs = {norm_pkg(l.quote.get("packaging")) for _, l in allocations
                if norm_pkg(l.quote.get("packaging"))}
        pkg_mismatch = is_split and len(pkgs) > 1

        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        valids = [l.quote.get("valid_until") for _, l in allocations if l.quote.get("valid_until")]
        earliest_valid = min(valids) if valids else None
        expired = bool(earliest_valid and str(earliest_valid) < now_iso)

        if auth_flag:
            remark = (remark + " · " if remark and remark != "Exact P/N" else "") + \
                     "cost far below market — verify CoC / stock-label"
        if plan_shortage:
            remark = (remark + " · " if remark and remark != "Exact P/N" else "") + \
                     "Market shortage — extended lead time"

        it.update(
            cost_price=primary.calc.get("usd_cost"),
            cost_currency=primary.quote.get("currency") or quote_ccy,
            selected_vendor=primary.quote.get("vendor_name"),
            fulfillment=fulfillment,
            margin_percent=margin_pct,
            unit_price=resale,
            landed_cost=round(blended, 4),
            fx_rate=primary.calc.get("fx_rate"),
            lead_time=lead,
            moq=primary.quote.get("moq") or it.get("moq"),
            spq=primary.quote.get("spq") or it.get("spq"),
            packaging=norm_pkg(primary.quote.get("packaging")) or (next(iter(pkgs)) if pkgs else None),
            date_code=primary.quote.get("date_code") or it.get("date_code"),
            quote_valid_until=earliest_valid,
            eud_required=bool(eud_flag), eud_reason=eud_reason,
            authenticity_review=bool(auth_flag),
            remark=remark,
        )

        # Status precedence: anomaly → shortfall → packaging → expiry → split → priced.
        if not anomaly_ok:
            it.update(pricing_status="needs_review",
                      pricing_note=f"price anomaly: {anomaly_reason}",
                      remark=f"{remark} | price anomaly — verify")
            s.anomaly += 1
            s.unpriced += 1
        elif shortfall > 0:
            note = (f"Only {int(covered):,} of {int(req_qty):,} available "
                    f"({int(shortfall):,} short) — confirm the balance")
            it.update(pricing_status="needs_review", pricing_note=note, remark=note)
            s.partial += 1
            s.unpriced += 1
        elif pkg_mismatch:
            note = ("Packaging mismatch across vendors ("
                    + " vs ".join(sorted(pkgs)) + ") — confirm before order")
            it.update(pricing_status="needs_review", pricing_note=note,
                      remark=f"{remark} | {note}")
            s.pkg_mismatch += 1
            s.unpriced += 1
        elif expired:
            note = "Vendor quote expired — re-source before quoting"
            it.update(pricing_status="needs_review", pricing_note=note, remark=note)
            s.expired += 1
            s.unpriced += 1
        elif multi_vendor or plan_hold:
            note = ("Multiple suppliers needed — confirm sourcing before order"
                    if multi_vendor else "Held for buyer review before quoting")
            it.update(pricing_status="needs_review", pricing_note=note, remark=note)
            s.split_review += 1
            s.unpriced += 1
        else:
            it.update(pricing_status="priced",
                      newly_priced=is_followup and key not in prior_priced)
            s.priced += 1
        out_items.append(it)

    # Persist selections + updated lines + decision-time snapshot (the CAPTURE edge:
    # every competing lot, trust, and market median at the moment we priced).
    if selected_ids:
        with db.connect() as conn:
            conn.executemany("UPDATE vendor_quotes SET is_selected = 1 WHERE id = ?",
                             [(i,) for i in selected_ids])
    total = sum((it.get("unit_price") or 0) * (it.get("quantity") or 0)
                for it in out_items if it.get("unit_price"))
    s.total_amount = round(total, 2)
    db.update("deals",
              {"line_items": db.j(out_items), "total_amount": s.total_amount,
               "decision_snapshot": db.j({"lines": snapshot_lines, "fx": fx,
                                          "at": db.utcnow()}),
               "updated_at": db.utcnow()},
              "id = ?", (deal_id,))
    for it in out_items:
        if it.get("unit_price") and it.get("mpn"):
            history.record_price(it["mpn"], it["unit_price"], side="our_quote",
                                 source=deal.get("quote_number") or f"deal:{deal_id}",
                                 qty=it.get("quantity"), manufacturer=it.get("manufacturer"))
    log.info("[%s] consolidation: %s", deal.get("quote_number"), s.as_dict())
    db.audit("consolidated", {"deal": deal.get("quote_number"), **s.as_dict()})
    return s
