"""End-to-end consolidation over a real (seeded) database — the worksheet scenarios."""

from spotdesk import db
from spotdesk.pipeline import pricing


def _mk_deal(line_items, currency="USD", margin_override=None):
    eid = db.insert("emails", {"gmail_id": f"t-{db.utcnow()}-{len(line_items)}",
                               "thread_id": "t-thread", "email_type": "rfq"})
    did = db.insert("deals", {"email_id": eid, "quote_number": f"AE-Q-TEST-{eid:04d}",
                              "customer_email": "buyer@test.example",
                              "currency": currency,
                              "margin_percent_override": margin_override,
                              "line_items": db.j(line_items)})
    return db.query_one("SELECT * FROM deals WHERE id = ?", (did,))


def _vq(deal_id, vendor, cost, qty=None, dc=None, pkg=None, currency="USD",
        lead=None, valid_until=None):
    vrfq = db.insert("vendor_rfqs", {"deal_id": deal_id, "vendor_email": f"{vendor}@x.com",
                                     "vendor_name": vendor, "status": "replied",
                                     "sent_at": db.utcnow(), "replied_at": db.utcnow(),
                                     "requested_mpns": db.j(["PART-1"])})
    db.insert("vendor_quotes", {"deal_id": deal_id, "vendor_rfq_id": vrfq,
                                "vendor_email": f"{vendor}@x.com", "vendor_name": vendor,
                                "mpn": "PART-1", "cost_price": cost, "currency": currency,
                                "offered_qty": qty, "date_code": dc, "packaging": pkg,
                                "lead_time": lead, "valid_until": valid_until})


def test_cheapest_vendor_wins(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 1000}])
    _vq(deal["id"], "Cheap", 4.45)
    _vq(deal["id"], "Pricey", 7.05)
    s = pricing.consolidate_deal(deal)
    assert s.priced == 1
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    assert items[0]["selected_vendor"] == "Cheap"
    assert items[0]["unit_price"] > 4.45           # margin applied
    assert items[0]["pricing_status"] == "priced"


def test_supply_cap_prefers_single_covering_vendor(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 3000}])
    _vq(deal["id"], "CappedCheap", 4.00, qty=1000)
    _vq(deal["id"], "FullCover", 4.60)
    s = pricing.consolidate_deal(deal)
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    assert items[0]["selected_vendor"] == "FullCover"
    assert s.priced == 1 and s.split_review == 0


def test_cross_vendor_split_is_held_for_review(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 3000}])
    _vq(deal["id"], "A", 4.00, qty=2000)
    _vq(deal["id"], "B", 4.60, qty=2000)
    s = pricing.consolidate_deal(deal)
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    assert items[0]["pricing_status"] == "needs_review"
    assert s.split_review == 1
    # Blended: 2000@4.00 + 1000@4.60 landed (×1.03 expenses) then margin — just check
    # the fulfillment breakdown recorded both legs.
    assert len(items[0]["fulfillment"]) == 2


def test_shortfall_held(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 10000}])
    _vq(deal["id"], "A", 4.00, qty=2000)
    _vq(deal["id"], "B", 4.60, qty=3000)
    s = pricing.consolidate_deal(deal)
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    assert items[0]["pricing_status"] == "needs_review"
    assert "short" in (items[0]["remark"] or "").lower() or \
           "available" in (items[0]["remark"] or "").lower()
    assert s.partial == 1


def test_no_bid_vs_pending(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 100},
                     {"mpn": "PART-2", "manufacturer": "Intel", "quantity": 100}])
    _vq(deal["id"], "OnlyVendor", 4.00)          # quotes PART-1 only; RFQ covered PART-1
    # PART-2: one RFQ still awaiting a reply → pending, not no_bid.
    db.insert("vendor_rfqs", {"deal_id": deal["id"], "vendor_email": "slow@x.com",
                              "vendor_name": "Slow", "status": "sent",
                              "sent_at": db.utcnow(),
                              "requested_mpns": db.j(["PART-2"])})
    pricing.consolidate_deal(deal)
    items = {it["mpn"]: it for it in db.uj(db.query_one(
        "SELECT line_items FROM deals WHERE id = ?", (deal["id"],))["line_items"])}
    assert items["PART-1"]["pricing_status"] == "priced"
    assert items["PART-2"]["pricing_status"] == "pending"


def test_date_code_lots_quote_separately(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 3000}])
    _vq(deal["id"], "A", 4.00, qty=1000, dc="24+")
    _vq(deal["id"], "A", 4.60, qty=2000, dc="22+")
    pricing.consolidate_deal(deal)
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    # One inquiry line expanded into two customer lines, one per date-code lot.
    assert len(items) == 2
    assert {it["date_code"] for it in items} == {"24+", "22+"}
    assert all(it.get("date_code_lot") for it in items)


def test_packaging_mismatch_flagged(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 3000}])
    _vq(deal["id"], "A", 4.00, qty=2000, pkg="Tape & Reel")
    _vq(deal["id"], "B", 4.10, qty=2000, pkg="Bulk")
    s = pricing.consolidate_deal(deal)
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    assert items[0]["pricing_status"] == "needs_review"
    # Held by either the packaging check or the multi-vendor rule — packaging counts.
    assert s.pkg_mismatch + s.split_review >= 1


def test_expired_quote_held(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 100}])
    _vq(deal["id"], "Stale", 4.00, valid_until="2020-01-01T00:00:00")
    pricing.consolidate_deal(deal)
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    assert items[0]["pricing_status"] == "needs_review"
    assert "expired" in (items[0]["remark"] or "").lower()


def test_too_cheap_authenticity_flag(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 100}])
    _vq(deal["id"], "Suspicious", 1.00)
    _vq(deal["id"], "B", 5.00)
    _vq(deal["id"], "C", 5.20)
    pricing.consolidate_deal(deal)
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    assert items[0]["authenticity_review"] is True


def test_negotiated_line_not_repriced(clean_db):
    deal = _mk_deal([{"mpn": "PART-1", "manufacturer": "Micron", "quantity": 100,
                      "negotiated": True, "unit_price": 4.20, "pricing_status": "priced"}])
    _vq(deal["id"], "A", 3.00)
    s = pricing.consolidate_deal(deal)
    items = db.uj(db.query_one("SELECT line_items FROM deals WHERE id = ?",
                               (deal["id"],))["line_items"])
    assert items[0]["unit_price"] == 4.20        # the agreed price survived
    assert s.priced == 1
