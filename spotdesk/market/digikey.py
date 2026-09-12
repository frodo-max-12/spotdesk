"""DigiKey Product Information API v4 — OAuth client_credentials, token cached ~10 min.

Creds file (~/.digikey_creds.json): {"client_id": "...", "client_secret": "...",
"use_sandbox": false}. Sandbox works immediately after registration; production needs
the app added to an organization in the DigiKey developer portal.
"""

from __future__ import annotations

import threading
import time

import requests

from .. import config
from .base import PriceBreak, Quote, read_json_creds

_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def _base_url(creds: dict) -> str:
    return "https://sandbox-api.digikey.com" if creds.get("use_sandbox") else "https://api.digikey.com"


def _get_token(creds: dict) -> str:
    base = _base_url(creds)
    with _token_lock:
        cached = _token_cache.get(base)
        if cached and cached[1] - 30 > time.time():
            return cached[0]
        r = requests.post(
            f"{base}/v1/oauth2/token",
            data={"client_id": creds["client_id"], "client_secret": creds["client_secret"],
                  "grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        r.raise_for_status()
        body = r.json()
        _token_cache[base] = (body["access_token"], time.time() + float(body.get("expires_in", 600)))
        return body["access_token"]


def lookup(mpn: str) -> Quote:
    creds = read_json_creds(config.DIGIKEY_CREDS_PATH)
    if not creds or not creds.get("client_id") or not creds.get("client_secret"):
        return Quote(distributor="digikey", mpn=mpn,
                     error=f"no credentials ({config.DIGIKEY_CREDS_PATH})")
    try:
        token = _get_token(creds)
    except Exception as e:
        return Quote(distributor="digikey", mpn=mpn, error=f"auth failed: {e}")

    base = _base_url(creds)
    try:
        r = requests.post(
            f"{base}/products/v4/search/keyword",
            json={"Keywords": mpn, "Limit": 5, "Offset": 0},
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": creds["client_id"],
                "X-DIGIKEY-Locale-Site": "US", "X-DIGIKEY-Locale-Language": "en",
                "X-DIGIKEY-Locale-Currency": "USD",
                "Content-Type": "application/json", "Accept": "application/json",
            }, timeout=12)
    except requests.RequestException as e:
        return Quote(distributor="digikey", mpn=mpn, error=f"network: {e}")
    if r.status_code != 200:
        return Quote(distributor="digikey", mpn=mpn, error=f"HTTP {r.status_code}: {r.text[:160]}")

    products = r.json().get("Products") or []
    if not products:
        return Quote(distributor="digikey", mpn=mpn, found=False, error="not found")
    exact = [p for p in products
             if (p.get("ManufacturerProductNumber") or "").upper() == mpn.upper()]
    product = exact[0] if exact else products[0]

    # v4 nests packaging variations (cut tape, reel…) — take the cheapest one.
    variations = product.get("ProductVariations") or []
    variation = min(
        variations,
        key=lambda v: (v.get("StandardPricing") or [{}])[0].get("UnitPrice", float("inf"))
        if v.get("StandardPricing") else float("inf"),
    ) if variations else {}

    breaks = [PriceBreak(qty=int(pb["BreakQuantity"]), unit_price_usd=float(pb["UnitPrice"]))
              for pb in (variation.get("StandardPricing") or [])
              if pb.get("UnitPrice") is not None and pb.get("BreakQuantity")]

    qty_avail = product.get("QuantityAvailable") or variation.get("QuantityAvailableforPackageType")
    mfr = product.get("Manufacturer") or {}
    lead_weeks = product.get("ManufacturerLeadWeeks")

    return Quote(
        distributor="digikey", mpn=mpn, found=True,
        manufacturer=mfr.get("Name") if isinstance(mfr, dict) else mfr,
        description=(product.get("Description") or {}).get("ProductDescription")
        if isinstance(product.get("Description"), dict) else product.get("Description"),
        distributor_pn=variation.get("DigiKeyProductNumber"),
        stock=int(qty_avail) if qty_avail else None,
        moq=int(variation["MinimumOrderQuantity"]) if variation.get("MinimumOrderQuantity") else None,
        package=(variation.get("PackageType") or {}).get("Name"),
        lead_time_days=int(lead_weeks) * 7 if lead_weeks else None,
        price_breaks=breaks,
        datasheet_url=product.get("DatasheetUrl"),
        product_url=product.get("ProductUrl"),
    )
