"""OEMsTrade scrape — server-side-rendered aggregator indexing 25+ distributors,
including the pure broker channel (Win Source, Sierra IC, Quest, Bristol, DST, LIBRA,
EBV, Verical…). No API access needed. Two jobs:

  * cross-check on official API prices;
  * CHANNEL DISCOVERY — every broker row here is a potential supply node the desk
    doesn't have a relationship with yet.
"""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from .base import PriceBreak, Quote

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}


def lookup(mpn: str) -> list[Quote]:
    """One Quote per distributor row, namespaced 'oemstrade:<Distributor>' so official
    API hits and indexed hits for the same distributor stay distinguishable."""
    url = f"https://www.oemstrade.com/search/{mpn}"
    try:
        r = requests.get(url, headers=_UA, timeout=15)
    except requests.RequestException as e:
        return [Quote(distributor="oemstrade", mpn=mpn, error=f"network: {e}", product_url=url)]
    if r.status_code != 200:
        return [Quote(distributor="oemstrade", mpn=mpn, error=f"HTTP {r.status_code}", product_url=url)]

    soup = BeautifulSoup(r.text, "html.parser")
    quotes: list[Quote] = []
    current_distributor: str | None = None

    # Walk h2 + tr.row in document order — the h2 preceding each row IS the distributor.
    # (The .td-distributor-name cell is misnamed: it actually holds the MANUFACTURER.)
    for el in soup.find_all(["h2", "tr"]):
        if el.name == "h2":
            txt = el.get_text(" ", strip=True)
            for suffix in (" ECIA", " Authorized"):
                if suffix in txt:
                    txt = txt.split(suffix)[0]
            current_distributor = txt.strip() or None
            continue
        if "row" not in (el.get("class") or []) or not current_distributor:
            continue
        td_pn = el.select_one(".td-part-number")
        if not td_pn:
            continue
        pn_text = td_pn.get_text(" ", strip=True).split(" D#:")[0].strip()
        if pn_text.upper() != mpn.upper() and not pn_text.upper().startswith(mpn.upper()):
            continue

        td_mfr = el.select_one(".td-distributor-name")
        td_stock = el.select_one(".td-stock")
        td_price = el.select_one(".td-price")
        td_desc = el.select_one(".td-desc")

        stock = None
        if td_stock:
            m = re.search(r"\d+", td_stock.get_text(" ", strip=True).replace(",", ""))
            if m:
                stock = int(m.group())

        breaks: list[PriceBreak] = []
        if td_price:
            # "1 $13.9600 10 $11.4700 25 $10.8500 ..."
            for m in re.finditer(r"(\d+(?:,\d{3})*)\s*\$\s*([\d.]+)",
                                 td_price.get_text(" ", strip=True)):
                try:
                    breaks.append(PriceBreak(qty=int(m.group(1).replace(",", "")),
                                             unit_price_usd=float(m.group(2))))
                except (ValueError, TypeError):
                    pass

        quotes.append(Quote(
            distributor=f"oemstrade:{current_distributor}", mpn=mpn, found=True,
            manufacturer=td_mfr.get_text(" ", strip=True) if td_mfr else None,
            distributor_pn=pn_text, stock=stock,
            description=td_desc.get_text(" ", strip=True) if td_desc else None,
            price_breaks=breaks, product_url=url,
        ))

    if not quotes:
        return [Quote(distributor="oemstrade", mpn=mpn, found=False,
                      error="no matching rows", product_url=url)]
    return quotes
