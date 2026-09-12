"""FX auto-fetch — daily USD→INR from free key-less APIs, with a safety buffer.

Used ONLY to normalize a non-USD vendor cost to USD before comparison; sales are USD.
effective = fetched × (1 + buffer%) so currency moves don't quietly erode margin.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from .. import config, db

log = logging.getLogger("spotdesk.fx")

_ENDPOINTS = [
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD",
     lambda d: (d.get("rates") or {}).get("INR")),
    ("frankfurter", "https://api.frankfurter.app/latest?from=USD&to=INR",
     lambda d: (d.get("rates") or {}).get("INR")),
]


def fetch_usd_inr(timeout: int = 10) -> float | None:
    for name, url, extract in _ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SpotDesk/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            rate = extract(data)
            if rate and float(rate) > 0:
                log.info("FX USD->INR = %s (%s)", rate, name)
                return float(rate)
        except Exception as e:
            log.warning("FX fetch failed from %s: %s", name, e)
    return None


def _latest() -> dict | None:
    return db.query_one(
        "SELECT * FROM fx_rates WHERE pair = 'USDINR' ORDER BY fetched_at DESC LIMIT 1")


def get_effective_rate() -> float:
    row = _latest()
    if row and row.get("effective_rate"):
        return float(row["effective_rate"])
    return config.USD_INR_FALLBACK


def ensure_fresh() -> None:
    """Cheap to call every monitor cycle — only hits the network when actually due."""
    if not config.FX_AUTO_FETCH:
        return
    row = _latest()
    due = row is None or not row.get("fetched_at")
    if not due:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(str(row["fetched_at"])).replace(tzinfo=timezone.utc)
                     ).total_seconds() / 3600
            due = age_h > config.FX_REFRESH_HOURS
        except (ValueError, TypeError):
            due = True
    if not due:
        return
    raw = fetch_usd_inr()
    if not raw:
        return
    buf = config.FX_BUFFER_PERCENT
    db.insert("fx_rates", {"pair": "USDINR", "base_rate": round(raw, 4),
                           "buffer_percent": buf,
                           "effective_rate": round(raw * (1 + buf / 100.0), 4),
                           "source": "api", "fetched_at": db.utcnow()})
