"""Pricing core: margin engine, margin guard, allocation, plan validation, parsers."""

import pytest

from spotdesk.pipeline import pricing
from spotdesk.pipeline.pricing import (Lot, compute_resale, deterministic_allocation,
                                       is_internal_remark, landed_floor, lead_days,
                                       norm_pkg, parse_validity, to_float, to_int,
                                       validate_llm_plan)


# ---- margin engine -------------------------------------------------

def test_resale_usd_basic():
    r = compute_resale(1.00)
    assert r["resale"] > 1.00
    # expenses 3% then margin 15% → 1.03 × 1.15 = 1.1845
    assert r["resale"] == pytest.approx(1.1845, abs=1e-4)
    assert r["margin_percent"] == 15.0


def test_resale_margin_override():
    r = compute_resale(2.00, margin_percent_override=50)
    assert r["margin_percent"] == 50.0
    assert r["resale"] == pytest.approx(2.00 * 1.03 * 1.5, abs=1e-3)


def test_margin_guard_lifts_below_floor():
    # A 0% override would price below the landed floor → guard lifts to the floor.
    r = compute_resale(10.0, margin_percent_override=0)
    assert r["resale"] >= landed_floor(10.0) - 1e-9
    assert r["guard_note"]


def test_resale_inr_conversion():
    r = compute_resale(83.0, cost_currency="INR", fx_rate=83.0)
    assert r["usd_cost"] == pytest.approx(1.0)
    assert r["resale"] == pytest.approx(1.1845, abs=1e-3)


def test_resale_blocked_cases():
    assert compute_resale(1.0, sale_currency="SGD")["blocked"] == "currency"
    assert compute_resale(1.0, cost_currency="INR", fx_rate=None)["blocked"] == "no_fx"
    assert compute_resale(1.0, cost_currency="EUR")["blocked"] == "currency"
    assert compute_resale(0) is None
    assert compute_resale("garbage") is None
    assert compute_resale(-5) is None


# ---- allocation ----------------------------------------------------

def _lot(landed, cap=None, vendor="V", vq_id=1):
    return Lot(landed=landed, calc={"margin_percent": 15.0, "landed_cost": landed},
               quote={"id": vq_id, "offered_qty": cap, "vendor_name": vendor,
                      "vendor_email": f"{vendor.lower()}@x.com"})


def test_alloc_prefers_single_vendor_covering_full_qty():
    # Cheapest is capped at 4000; the next vendor covers all 6000 → single clean PO.
    lots = [_lot(4.45, cap=4000, vendor="A"), _lot(5.40, cap=None, vendor="B")]
    alloc = deterministic_allocation(lots, 6000)
    assert len(alloc) == 1
    assert alloc[0][0] == 6000
    assert alloc[0][1].quote["vendor_name"] == "B"


def test_alloc_splits_only_as_last_resort():
    lots = [_lot(4.45, cap=4000, vendor="A"), _lot(5.40, cap=3000, vendor="B")]
    alloc = deterministic_allocation(lots, 6000)
    assert [(q, l.quote["vendor_name"]) for q, l in alloc] == [(4000, "A"), (2000, "B")]


def test_alloc_unknown_qty_single_cheapest():
    lots = [_lot(4.45, vendor="A"), _lot(5.40, vendor="B")]
    alloc = deterministic_allocation(lots, 0)
    assert len(alloc) == 1 and alloc[0][0] is None
    assert alloc[0][1].quote["vendor_name"] == "A"


def test_llm_plan_validation_gates():
    lots = [_lot(4.45, cap=4000, vendor="A", vq_id=1),
            _lot(5.40, cap=3000, vendor="B", vq_id=2)]
    ok = validate_llm_plan({"allocation": [{"lot": 0, "qty": 4000},
                                          {"lot": 1, "qty": 2000}]}, lots, 6000)
    assert ok and sum(q for q, _ in ok) == 6000
    # over-allocation beyond the requirement → rejected
    assert validate_llm_plan({"allocation": [{"lot": 0, "qty": 4000},
                                             {"lot": 1, "qty": 3000}]}, lots, 6000) is None
    # exceeding a lot's cap → rejected
    assert validate_llm_plan({"allocation": [{"lot": 0, "qty": 5000}]}, lots, 6000) is None
    # invented lot index → rejected
    assert validate_llm_plan({"allocation": [{"lot": 7, "qty": 100}]}, lots, 6000) is None
    # repeated lot → rejected
    assert validate_llm_plan({"allocation": [{"lot": 0, "qty": 100},
                                             {"lot": 0, "qty": 100}]}, lots, 6000) is None
    # zero/negative qty → rejected
    assert validate_llm_plan({"allocation": [{"lot": 0, "qty": 0}]}, lots, 6000) is None


# ---- parsers -------------------------------------------------------

def test_lead_days_units_normalized():
    assert lead_days("2 Days") == 2
    assert lead_days("1 Week") == 7
    assert lead_days("2 Days") < lead_days("1 Week")   # the tie-break bug, fixed
    assert lead_days("4-6 weeks") == 35
    assert lead_days("ex-stock") == 0
    assert lead_days("2 months") == 60
    assert lead_days(None) is None
    assert lead_days("call us") is None


def test_norm_pkg():
    assert norm_pkg("Tape & Reel") == "reel"
    assert norm_pkg("T&R") == "reel"
    assert norm_pkg("Cut Tape") == "cut_tape"
    assert norm_pkg("TRAY") == "tray"
    assert norm_pkg("bulk pack") == "bulk"
    assert norm_pkg("") is None
    assert norm_pkg("weird") is None


def test_parse_validity():
    assert parse_validity("valid till 2026-07-10").startswith("2026-07-10")
    assert parse_validity("48 hours") is not None
    assert parse_validity("subject to prior sale") is None
    assert parse_validity("") is None


def test_internal_remark_never_ships():
    assert is_internal_remark("please match or beat our target buy price")
    assert is_internal_remark("margin now 12%")
    assert not is_internal_remark("Alternate part, verify footprint")
    assert not is_internal_remark("")


def test_numeric_coercion():
    assert to_float("1,234.5") == 1234.5
    assert to_float(None) is None
    assert to_int("8,000 ") == 8000
    assert to_int("") is None
