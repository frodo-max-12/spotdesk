"""Seed a demo desk — realistic pipeline state with NO external calls.

    python scripts/seed_demo.py

Gives the dashboard something to show and gives new operators a safe playground:
a vendor panel, an in-flight deal with competing vendor quotes, a quoted deal, a won
deal with its PO + shipment, and pending draft rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spotdesk import db  # noqa: E402


def seed():
    db.init_schema()

    vendors = [
        ("elaine@nxelectronics.example", "Elaine", "NX Electronics", "Hong Kong",
         ["MICRON", "SKHYNIX"], 70),
        ("tavis@flykingtech.example", "Tavis", "Flyking Technology", "China",
         ["BROADCOM", "INTEL", "MICRON"], 65),
        ("daisy@quiksol.example", "Daisy", "Quiksol International", "China",
         ["INTEL"], 55),
        ("sales@greatchips.example", "Alice", "Greatchips Electronics", "Hong Kong",
         ["ANY"], 60),
        ("ruby@vitalglobal.example", "Ruby", "Vital Electronics", "UK",
         ["ANY"], 70),
    ]
    for email, name, company, country, brands, trust in vendors:
        db.execute(
            "INSERT OR IGNORE INTO counterparties (email, name, company, domain, kind, "
            "brands, country, currency, trust_score) VALUES (?,?,?,?,?,?,?,?,?)",
            (email, name, company, email.split("@")[-1], "vendor",
             db.j(brands), country, "USD", trust))

    db.execute("INSERT OR IGNORE INTO counterparties (email, name, company, domain, kind, "
               "trust_score, n_inquiries) VALUES (?,?,?,?,?,?,?)",
               ("buyer@chip1.example", "Efkan", "Chip 1 Exchange", "chip1.example",
                "buyer", 70, 3))

    # Deal 1 — sourcing in flight, three competing quotes on a DDR3 part.
    e1 = db.insert("emails", {"gmail_id": "demo-rfq-1", "thread_id": "demo-thread-1",
                              "from_email": "buyer@chip1.example",
                              "subject": "RFQ - MT41K128M16JT-125AAT:K 6000pcs",
                              "email_type": "rfq", "status": "processing"})
    d1 = db.insert("deals", {
        "email_id": e1, "quote_number": "AE-Q-20260831-0001",
        "customer_name": "Efkan", "customer_email": "buyer@chip1.example",
        "customer_company": "Chip 1 Exchange", "currency": "USD",
        "status": "sourcing_vendors",
        "line_items": db.j([{"mpn": "MT41K128M16JT-125AAT:K", "manufacturer": "Micron",
                             "quantity": 6000, "description": "DDR3-1600 2Gb",
                             "unit_price": None}])})
    for vendor_email, vendor_name, cost, qty, dc in [
            ("sales@greatchips.example", "Greatchips Electronics", 4.45, 6000, "22+"),
            ("elaine@nxelectronics.example", "NX Electronics", 7.05, 6000, "21+"),
            ("tavis@flykingtech.example", "Flyking Technology", 5.40, 4000, "22+")]:
        vrfq = db.insert("vendor_rfqs", {"deal_id": d1, "vendor_email": vendor_email,
                                         "vendor_name": vendor_name,
                                         "requested_mpns": db.j(["MT41K128M16JT-125AAT:K"]),
                                         "status": "replied", "sent_at": db.utcnow(),
                                         "replied_at": db.utcnow()})
        db.insert("vendor_quotes", {"deal_id": d1, "vendor_rfq_id": vrfq,
                                    "vendor_email": vendor_email, "vendor_name": vendor_name,
                                    "mpn": "MT41K128M16JT-125AAT:K", "cost_price": cost,
                                    "currency": "USD", "offered_qty": qty,
                                    "date_code": dc, "lead_time": "3-5 days",
                                    "packaging": "Reel"})

    # Deal 2 — quoted, waiting on the customer.
    e2 = db.insert("emails", {"gmail_id": "demo-rfq-2", "thread_id": "demo-thread-2",
                              "from_email": "buyer@chip1.example",
                              "subject": "Inquiry - Intel FH8070304243808",
                              "email_type": "rfq", "status": "processing"})
    db.insert("deals", {
        "email_id": e2, "quote_number": "AE-Q-20260830-0002",
        "customer_name": "Efkan", "customer_email": "buyer@chip1.example",
        "customer_company": "Chip 1 Exchange", "currency": "USD", "status": "sent",
        "sent_at": db.utcnow(), "we_quoted_usd": 26000.0, "total_amount": 26000.0,
        "line_items": db.j([{"mpn": "FH8070304243808", "manufacturer": "Intel",
                             "quantity": 100, "unit_price": 260.0,
                             "pricing_status": "priced", "margin_percent": 15.0,
                             "selected_vendor": "Quiksol International"}])})

    # Deal 3 — won, PO placed, shipment in transit.
    e3 = db.insert("emails", {"gmail_id": "demo-rfq-3", "thread_id": "demo-thread-3",
                              "from_email": "buyer@chip1.example",
                              "subject": "RFQ - Samsung DDR5", "email_type": "rfq",
                              "status": "done"})
    d3 = db.insert("deals", {
        "email_id": e3, "quote_number": "AE-Q-20260820-0003",
        "customer_name": "Efkan", "customer_email": "buyer@chip1.example",
        "customer_company": "Chip 1 Exchange", "currency": "USD", "status": "po_placed",
        "outcome": "won", "we_quoted_usd": 15400.0, "clearing_price_usd": 15400.0,
        "closed_at": db.utcnow(), "sent_at": db.utcnow(),
        "line_items": db.j([{"mpn": "M321R8GA0EB2", "manufacturer": "Samsung",
                             "quantity": 200, "unit_price": 77.0,
                             "pricing_status": "priced"}])})
    vpo = db.insert("vendor_pos", {"deal_id": d3, "vendor_email": "ruby@vitalglobal.example",
                                   "vendor_name": "Vital Electronics",
                                   "po_number": "AE-PO-20260821-0003-1",
                                   "total_cost": 13000.0, "currency": "USD",
                                   "status": "shipped", "placed_at": db.utcnow(),
                                   "line_items": db.j([{"mpn": "M321R8GA0EB2",
                                                        "quantity": 200,
                                                        "unit_cost": 65.0}])})
    db.insert("shipments", {"vendor_po_id": vpo, "deal_id": d3, "status": "in_transit",
                            "carrier": "DHL", "awb": "JD0123456789",
                            "origin": "Hong Kong", "eta": "2026-09-04"})

    # A pending draft so the release flow is visible.
    db.insert("outbound_actions", {
        "kind": "quote", "deal_id": d1, "to_email": "buyer@chip1.example",
        "subject": "Re: RFQ - MT41K128M16JT-125AAT:K 6000pcs",
        "body_html": "<p>(demo draft)</p>", "thread_id": "demo-thread-1",
        "gmail_draft_id": "demo-draft-1", "status": "drafted",
        "note": "HOLD: demo — 1 line(s) not confidently priced"})

    db.insert("price_history", {"mpn": "MT41K128M16JT-125AAT:K", "price_usd": 5.20,
                                "side": "vendor_offer", "source": "demo"})
    print("seeded demo desk → data/desk.db")


if __name__ == "__main__":
    seed()
