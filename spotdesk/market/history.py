"""The desk's own price memory — price_history + market_offers queries.

Section-3-first doctrine: if a broker offered this part in the last 90 days, that is
the buy reference. Even without a price, the qty signal proves the channel exists —
reply to that thread rather than blasting cold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import db


def record_price(mpn: str, price_usd: float, *, side: str, source: str,
                 qty: int | None = None, manufacturer: str | None = None,
                 date_code: str | None = None) -> None:
    if not mpn or not price_usd or price_usd <= 0:
        return
    db.insert("price_history", {
        "mpn": mpn.strip().upper(), "manufacturer": manufacturer,
        "price_usd": round(float(price_usd), 6), "qty": qty,
        "side": side, "source": source, "date_code": date_code,
    })


def median_price(mpn: str, days: int = 180) -> float | None:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT price_usd FROM price_history WHERE mpn = ? AND observed_at >= ? "
        "ORDER BY price_usd", ((mpn or "").strip().upper(), since))
    if not rows:
        return None
    prices = [r["price_usd"] for r in rows]
    m = len(prices) // 2
    return prices[m] if len(prices) % 2 else (prices[m - 1] + prices[m]) / 2.0


def broker_history(mpn: str, days: int = 365) -> dict:
    """Past broker touches on this MPN: offers + quotes we've seen, with summary stats."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    key = (mpn or "").strip().upper()
    offers = db.query(
        "SELECT vendor_name, vendor_email, unit_price, offered_qty, date_code, lead_time, "
        "received_at FROM market_offers WHERE UPPER(mpn) = ? AND received_at >= ? "
        "ORDER BY received_at DESC LIMIT 50", (key, since))
    past_quotes = db.query(
        "SELECT vendor_name, cost_price, offered_qty, date_code, lead_time, created_at "
        "FROM vendor_quotes WHERE UPPER(mpn) = ? AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 50", (key, since))
    brokers = {(o.get("vendor_name") or o.get("vendor_email") or "?") for o in offers} | \
              {(q.get("vendor_name") or "?") for q in past_quotes}
    prices = [o["unit_price"] for o in offers if o.get("unit_price")] + \
             [q["cost_price"] for q in past_quotes if q.get("cost_price")]
    latest = max([o["received_at"] for o in offers] +
                 [q["created_at"] for q in past_quotes], default=None)
    return {
        "offers": offers, "past_quotes": past_quotes,
        "count": len(offers) + len(past_quotes),
        "unique_brokers": len(brokers),
        "min_price_usd": min(prices) if prices else None,
        "max_price_usd": max(prices) if prices else None,
        "latest_seen": latest,
    }


def recent_market_offers(days: int = 90, limit: int = 400) -> list[dict]:
    """Recency-capped slice of the standing offer book for the LLM matcher. Retrieval
    only — deliberately NOT filtered by MPN (an equality filter made this feature dead
    code once: equivalents and right-vendor-wrong-line matches were invisible)."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT vendor_name, vendor_email, mpn, manufacturer, unit_price, currency, "
        "date_code, lead_time, offered_qty, received_at FROM market_offers "
        "WHERE active = 1 AND received_at >= ? ORDER BY received_at DESC LIMIT ?",
        (since, limit))
    out = []
    for o in rows:
        age = None
        try:
            age = (now - datetime.fromisoformat(str(o["received_at"])).replace(
                tzinfo=timezone.utc)).days
        except (ValueError, TypeError):
            pass
        out.append({"vendor": o["vendor_name"], "vendor_email": o["vendor_email"],
                    "mpn": o["mpn"], "manufacturer": o["manufacturer"],
                    "price": o["unit_price"], "currency": o["currency"],
                    "date_code": o["date_code"], "lead_time": o["lead_time"],
                    "qty": o["offered_qty"], "age_days": age})
    return out
