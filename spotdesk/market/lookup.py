"""RFQ-time market check — the one call to make when an inquiry lands.

Fires every configured channel in parallel (distributor APIs + aggregator scrape +
the desk's own price memory) and folds the answers into a desk decision frame:

  broker_history  — Section 3 first: a broker who offered this part recently is the
                    buy reference; the channel exists, revive that thread.
  price_ceiling   — authorized-distributor list price sets the customer's upper bound
                    (nobody pays list+10% unless allocation).
  channels        — aggregator rows are supply nodes we don't have relationships with
                    yet: sourcing leads.
  shortage        — live stock/lead signal for the quote's remark line.

Every price observed is appended to price_history — a lookup is never wasted.

CLI:  python -m spotdesk.market.lookup STM32F405RGT6 [--json]
"""

from __future__ import annotations

import logging
import time

from .. import config, db
from . import digikey, history, mouser, oemstrade
from .base import Quote, run_parallel

log = logging.getLogger("spotdesk.market")


def identify_part(mpn: str) -> dict | None:
    """MPN → real manufacturer + description, grounded in a distributor database and
    cached forever in part_info (an empty row = 'looked up, not found' marker)."""
    key = (mpn or "").strip().upper()
    if not key:
        return None
    cached = db.query_one("SELECT * FROM part_info WHERE mpn = ?", (key,))
    if cached is not None:
        if not cached.get("manufacturer") and not cached.get("description"):
            return None                      # known not-found; don't re-query
        return {k: cached.get(k) for k in
                ("manufacturer", "description", "category", "datasheet_url", "source")}
    if not config.PART_LOOKUP_ENABLED:
        return None
    info = None
    try:
        info = mouser.identify(key)
    except Exception as e:
        log.warning("identify(%s) failed: %s", key, e)
        return None
    row = {"mpn": key, **{k: (info or {}).get(k) for k in
                          ("manufacturer", "description", "category", "datasheet_url")},
           "source": (info or {}).get("source") or "mouser"}
    try:
        db.insert("part_info", row)
    except Exception:
        pass                                  # concurrent insert — cache already there
    return info


def market_status(mpn: str) -> dict | None:
    if not config.PART_LOOKUP_ENABLED:
        return None
    try:
        return mouser.market_status(mpn)
    except Exception:
        return None


def run(mpn: str, include_aggregators: bool = True) -> dict:
    """The full parallel check. Returns the consolidated decision frame."""
    started = time.time()
    mpn = (mpn or "").strip()

    lookups = []
    if config.PART_LOOKUP_ENABLED and config.MOUSER_API_KEY:
        lookups.append(("mouser", mouser.lookup))
    if config.DIGIKEY_CREDS_PATH.exists():
        lookups.append(("digikey", digikey.lookup))
    api_quotes: list[Quote] = run_parallel(lookups, mpn,
                                           timeout_sec=config.LOOKUP_TIMEOUT_SECONDS)

    agg_quotes: list[Quote] = []
    if include_aggregators and config.OEMSTRADE_ENABLED:
        try:
            agg_quotes = oemstrade.lookup(mpn)
        except Exception as e:
            log.warning("oemstrade failed: %s", e)

    # Record every observed list price into the desk's price memory.
    for q in api_quotes + agg_quotes:
        if isinstance(q, Quote) and q.found and q.best_unit_price:
            history.record_price(mpn, q.best_unit_price, side="distributor_list",
                                 source=q.distributor, qty=q.stock,
                                 manufacturer=q.manufacturer)

    api_found = [q for q in api_quotes if isinstance(q, Quote) and q.found]
    ceiling_candidates = [q.best_unit_price for q in api_found if q.best_unit_price]
    price_ceiling = min(ceiling_candidates) if ceiling_candidates else None

    brokers_indexed = sorted({
        q.distributor.split(":", 1)[1] for q in agg_quotes
        if isinstance(q, Quote) and q.found and ":" in q.distributor})

    hist = history.broker_history(mpn)
    status = None
    for q in api_found:
        if q.distributor == "mouser":
            status = {"in_stock": q.stock or 0,
                      "lead_time": f"{q.lead_time_days} days" if q.lead_time_days else None,
                      "shortage": (q.stock or 0) == 0 and (q.lead_time_days or 0) >= 84}
            break

    return {
        "mpn": mpn,
        "elapsed_sec": round(time.time() - started, 2),
        "api_quotes": [q.to_dict() for q in api_found],
        "price_ceiling_usd": price_ceiling,
        "aggregator_channels": brokers_indexed,
        "aggregator_rows": [q.to_dict() for q in agg_quotes
                            if isinstance(q, Quote) and q.found][:25],
        "broker_history": {k: v for k, v in hist.items()
                           if k not in ("offers", "past_quotes")},
        "broker_offers": hist["offers"][:10],
        "median_price_usd": history.median_price(mpn),
        "market_status": status,
    }


def main() -> int:
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(description="RFQ-time market check")
    ap.add_argument("mpn")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-aggregators", action="store_true")
    args = ap.parse_args()
    from .. import db as _db
    _db.init_schema()
    res = run(args.mpn, include_aggregators=not args.no_aggregators)
    if args.json:
        print(_json.dumps(res, indent=2, default=str))
        return 0
    print(f"\nMARKET CHECK: {res['mpn']}  ({res['elapsed_sec']}s)")
    print("-" * 72)
    print(f"price ceiling (authorized list): "
          f"{'$%.4f' % res['price_ceiling_usd'] if res['price_ceiling_usd'] else '—'}")
    print(f"desk median (180d):              "
          f"{'$%.4f' % res['median_price_usd'] if res['median_price_usd'] else '—'}")
    bh = res["broker_history"]
    print(f"broker history: {bh['count']} touches from {bh['unique_brokers']} channels "
          f"(latest {bh['latest_seen'] or '—'})")
    if res["aggregator_channels"]:
        print(f"channels indexed: {', '.join(res['aggregator_channels'][:12])}")
    if res["market_status"]:
        print(f"live market: {res['market_status']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
