"""Everything else pure-logic: brands, BOM merge/HTML, gates, breakers, offers,
outcomes → trust flywheel."""

from spotdesk import config, db
from spotdesk.knowledge.line_card import brand_match, canonical, line_card
from spotdesk.ops import breakers
from spotdesk.outreach import offers
from spotdesk.pipeline import bom, quoting


# ---- brand aliasing ------------------------------------------------

def test_canonical_brand_aliases():
    assert canonical("ST Microelectronics") == "STM"
    assert canonical("Texas Instruments") == canonical("TI")
    assert canonical("SMC Diode Solutions") == "SMC"
    assert canonical("Würth".upper()) != ""     # doesn't crash on unicode


def test_brand_match_normalized():
    assert brand_match("STMicroelectronics", ["STM"])
    assert brand_match("Fairchild", ["ON Semiconductor"])
    assert not brand_match("Mornsun", ["STM"])
    assert not brand_match("", ["STM"])


def test_line_card_loads():
    lc = line_card()
    assert len(lc.all_brands()) > 10
    assert lc.summary()


# ---- BOM cleaning + merge -----------------------------------------

def test_html_table_cleaner_pads_jagged_rows():
    html = """<style>body{color:red}</style>
    <table><tr><th>MPN</th><th>Qty</th><th>Make</th></tr>
    <tr><td>1N4007</td><td>2200</td></tr>
    <tr><td>BAT54C</td><td>3000</td><td>SMC</td></tr></table>"""
    out = bom.html_to_clean_text(html)
    assert "1N4007" in out and "BAT54C" in out
    assert "color:red" not in out          # style content skipped, not emitted


def test_composite_merge_keeps_distinct_rows():
    rows = [
        {"mpn": "X1", "description": "d1", "package": "SOT-23", "quantity": 10},
        {"mpn": "X1", "description": "d1", "package": "SOT-23",
         "manufacturer": "STM"},                       # exact dup — merges, fills mfr
        {"mpn": "X1", "description": "d1", "package": "QFN"},   # distinct package — kept
        {"mpn": None, "description": "LCD 16x2", "package": None},  # desc-only — kept
        {"mpn": "", "description": "", "package": ""},              # empty — dropped
    ]
    merged = bom.merge_items(rows)
    assert len(merged) == 3
    assert merged[0]["manufacturer"] == "STM"


def test_best_body_prefers_html_tables():
    text = "Hi, please quote the attached."
    html = "<table><tr><td>STM32F405RGT6</td><td>500</td></tr></table>"
    out = bom.best_body(text, html)
    assert "STM32F405RGT6" in out


# ---- confidence gate ----------------------------------------------

def _deal(items, currency="USD", assumed=False):
    return {"id": 999, "quote_number": "AE-Q-TEST-9999", "currency": currency,
            "currency_assumed": 1 if assumed else 0, "customer_email": "b@x.com",
            "customer_name": "B", "validity_days": 30}


def test_gate_clean_when_all_priced(clean_db):
    items = [{"mpn": "A", "quantity": 10, "unit_price": 5.0,
              "pricing_status": "priced", "margin_percent": 15.0}]
    assert quoting.gate_reasons(_deal(items), items) == []


def test_gate_holds_on_each_condition(clean_db):
    base = {"mpn": "A", "quantity": 10, "unit_price": 5.0,
            "pricing_status": "priced", "margin_percent": 15.0}
    assert any("not confidently priced" in r for r in quoting.gate_reasons(
        _deal([{**base, "pricing_status": "pending", "unit_price": None}]),
        [{**base, "pricing_status": "pending", "unit_price": None}]))
    assert any("non-USD" in r for r in quoting.gate_reasons(
        _deal([base], currency="SGD"), [base]))
    assert any("assumed" in r for r in quoting.gate_reasons(
        _deal([base], assumed=True), [base]))
    assert any("End-User" in r for r in quoting.gate_reasons(
        _deal([{**base, "eud_required": True}]), [{**base, "eud_required": True}]))
    assert any("authenticity" in r for r in quoting.gate_reasons(
        _deal([{**base, "authenticity_review": True}]),
        [{**base, "authenticity_review": True}]))
    thin = {**base, "margin_percent": 2.0}
    assert any("margin floor" in r for r in quoting.gate_reasons(_deal([thin]), [thin]))


def test_quote_html_renders_states(clean_db):
    items = [
        {"mpn": "A1", "quantity": 10, "unit_price": 5.0, "pricing_status": "priced",
         "newly_priced": True},
        {"mpn": "B2", "quantity": 5, "unit_price": None, "pricing_status": "pending"},
        {"mpn": "C3", "quantity": 5, "unit_price": None, "pricing_status": "no_bid"},
    ]
    html = quoting.build_quote_html(_deal(items), items, is_update=True)
    assert "No Bid" in html and "Pending" in html and "New" in html
    assert config.COMPANY_ENTITY in html


def test_internal_remark_scrubbed_from_quote(clean_db):
    items = [{"mpn": "A1", "quantity": 10, "unit_price": 5.0,
              "pricing_status": "priced",
              "remark": "please match or beat our target buy price"}]
    html = quoting.build_quote_html(_deal(items), items)
    assert "target buy" not in html.lower()


# ---- breakers ------------------------------------------------------

def test_anomaly_bands():
    ok, _ = breakers.check_anomaly(5.0, 5.0)
    assert ok
    bad_high, _ = breakers.check_anomaly(60.0, 5.0)
    assert not bad_high
    bad_low, _ = breakers.check_anomaly(0.4, 5.0)
    assert not bad_low
    none_ref, _ = breakers.check_anomaly(5.0, None)
    assert none_ref


def test_cold_ramp_scales_with_history(clean_db):
    assert breakers.cold_daily_cap() == 300      # no history → conservative


# ---- offer pool ----------------------------------------------------

def test_offer_combos_and_jitter(tmp_path, monkeypatch):
    monkeypatch.setattr(offers, "POOL_PATH", tmp_path / "pool.json")
    pool = {"skus": {f"k{i}": {"mpn": f"MPN-{i}", "mfg": "Samsung", "family": "DDR5",
                               "dc": "25+", "base_qty": 6000, "types": ["server"]}
                     for i in range(10)},
            "used_combos": {}}
    offers.save_pool(pool)
    a = offers.suggest_combo("CompanyA", "server")
    b = offers.suggest_combo("CompanyB", "server", avoid_overlap_with=["CompanyA"])
    assert len(a) == config.SKUS_PER_COMPANY
    assert a != b                                  # anti-forwarding: different picks
    q1 = offers._jitter_qty(6000, "CompanyA", "k1")
    q2 = offers._jitter_qty(6000, "CompanyA", "k1")
    assert q1 == q2                                # deterministic per company+sku
    assert 6000 * 0.6 <= q1 <= 6000 * 1.4
    assert q1 % 1000 != 0 or q1 % 100 == 0         # "ugly" rounding applied
    subj = offers.make_subject(a)
    assert "Spot Stock" in subj and "—" not in subj


# ---- outcomes → trust flywheel (the connected arrow) ---------------

def test_close_deal_moves_trust(clean_db):
    db.insert("counterparties", {"email": "buyer@x.com", "kind": "buyer",
                                 "trust_score": 50})
    eid = db.insert("emails", {"gmail_id": "t-out-1", "email_type": "rfq"})
    did = db.insert("deals", {"email_id": eid, "quote_number": "AE-Q-TEST-7777",
                              "customer_email": "buyer@x.com", "status": "sent",
                              "we_quoted_usd": 1000.0, "sent_at": db.utcnow(),
                              "line_items": db.j([])})
    from spotdesk.learn import outcomes
    res = outcomes.close_deal(did, outcome="won", clearing_price_usd=1000.0,
                              reason="test")
    assert res["ok"]
    cp = db.query_one("SELECT * FROM counterparties WHERE email = 'buyer@x.com'")
    assert cp["trust_score"] == 50 + config.TRUST_BOOST_PER_WON   # the arrow fires
    assert cp["n_deals_won"] == 1
    # double-close is refused
    assert not outcomes.close_deal(did, outcome="lost")["ok"]


def test_no_response_is_censored_not_lost(clean_db):
    db.insert("counterparties", {"email": "ghost@x.com", "kind": "buyer",
                                 "trust_score": 50})
    eid = db.insert("emails", {"gmail_id": "t-out-2", "email_type": "rfq"})
    did = db.insert("deals", {"email_id": eid, "quote_number": "AE-Q-TEST-8888",
                              "customer_email": "ghost@x.com", "status": "sent",
                              "sent_at": "2020-01-01 00:00:00", "line_items": db.j([])})
    from spotdesk.learn import outcomes
    n = outcomes.expire_silent_deals()
    assert n == 1
    deal = db.query_one("SELECT * FROM deals WHERE id = ?", (did,))
    assert deal["outcome"] == "no_response"
    cp = db.query_one("SELECT trust_score, n_deals_lost FROM counterparties "
                      "WHERE email = 'ghost@x.com'")
    assert cp["trust_score"] == 50            # neutral — censored, not punished
    assert cp["n_deals_lost"] == 0
