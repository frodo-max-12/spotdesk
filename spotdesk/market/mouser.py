"""Mouser Search API — the workhorse channel (free key, plain POST, no OAuth).

Serves three jobs:
  lookup(mpn)        → normalized Quote (stock + price breaks) for the price ceiling
  identify(mpn)      → real manufacturer + description (grounds bare part numbers so
                       the LLM never guesses a make — mis-identification is worse
                       than "not found", so only EXACT matches count)
  market_status(mpn) → {in_stock, lead_time, shortage} for the shortage signal
"""

from __future__ import annotations

import json
import re
import urllib.request

from .. import config
from .base import PriceBreak, Quote, norm_mpn

_URL = "https://api.mouser.com/api/v1/search/partnumber?apiKey={key}"


def _post(mpn: str, timeout: int = 12) -> dict:
    body = json.dumps({"SearchByPartRequest": {"mouserPartNumber": mpn}}).encode()
    req = urllib.request.Request(_URL.format(key=config.MOUSER_API_KEY), data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _exact_part(data: dict, mpn: str) -> dict | None:
    parts = ((data or {}).get("SearchResults") or {}).get("Parts") or []
    target = norm_mpn(mpn)
    return next((p for p in parts
                 if norm_mpn(p.get("ManufacturerPartNumber")) == target), None)


def lookup(mpn: str) -> Quote:
    if not config.MOUSER_API_KEY:
        return Quote(distributor="mouser", mpn=mpn, error="no ~/.mouser_api_key / MOUSER_API_KEY")
    try:
        data = _post(mpn)
    except Exception as e:
        return Quote(distributor="mouser", mpn=mpn, error=f"network: {e}")
    part = _exact_part(data, mpn)
    if not part:
        return Quote(distributor="mouser", mpn=mpn, found=False, error="not found")

    breaks = []
    for pb in part.get("PriceBreaks") or []:
        try:
            price = float(re.sub(r"[^\d.]", "", str(pb.get("Price", ""))) or 0)
            qty = int(pb.get("Quantity") or 0)
            if price > 0 and qty > 0:
                breaks.append(PriceBreak(qty=qty, unit_price_usd=price))
        except (ValueError, TypeError):
            continue

    avail = (part.get("Availability") or "").strip()
    m = re.search(r"[\d,]+", avail)
    stock = int(m.group(0).replace(",", "")) if m else 0
    lead = (part.get("LeadTime") or "").strip() or None
    lead_days = None
    lm = re.search(r"(\d+)\s*(day|week)", (lead or "").lower())
    if lm:
        lead_days = int(lm.group(1)) * (7 if lm.group(2) == "week" else 1)

    return Quote(
        distributor="mouser", mpn=mpn, found=True,
        manufacturer=(part.get("Manufacturer") or "").strip() or None,
        description=(part.get("Description") or "").strip() or None,
        distributor_pn=(part.get("MouserPartNumber") or "").strip() or None,
        stock=stock,
        moq=int(part.get("Min") or 0) or None,
        lead_time_days=lead_days,
        price_breaks=breaks,
        datasheet_url=(part.get("DataSheetUrl") or "").strip() or None,
        product_url=(part.get("ProductDetailUrl") or "").strip() or None,
    )


def identify(mpn: str) -> dict | None:
    """{manufacturer, description, category, datasheet_url, source} or None.
    EXACT MPN matches only."""
    if not config.MOUSER_API_KEY:
        return None
    try:
        part = _exact_part(_post(mpn), mpn)
    except Exception:
        return None
    if not part:
        return None
    mfr = (part.get("Manufacturer") or "").strip()
    desc = (part.get("Description") or "").strip()
    if not mfr and not desc:
        return None
    return {"manufacturer": mfr or None, "description": desc or None,
            "category": (part.get("Category") or "").strip() or None,
            "datasheet_url": (part.get("DataSheetUrl") or "").strip() or None,
            "source": "mouser"}


def market_status(mpn: str) -> dict | None:
    """{in_stock, lead_time, shortage}. shortage = zero distributor stock OR a factory
    lead in allocation territory (≥ ~12 weeks). UNCACHED — stock is time-sensitive."""
    if not config.MOUSER_API_KEY:
        return None
    try:
        part = _exact_part(_post(mpn), mpn)
    except Exception:
        return None
    if not part:
        return None
    avail = (part.get("Availability") or "").strip()
    m = re.search(r"[\d,]+", avail)
    in_stock = int(m.group(0).replace(",", "")) if m else 0
    lead = (part.get("LeadTime") or "").strip() or None
    long_lead = False
    lm = re.search(r"(\d+)\s*(day|week)", (lead or "").lower())
    if lm:
        days = int(lm.group(1)) * (7 if lm.group(2) == "week" else 1)
        long_lead = days >= 84
    return {"in_stock": in_stock, "lead_time": lead,
            "shortage": (in_stock == 0) or long_lead}
