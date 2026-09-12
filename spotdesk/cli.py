"""`desk` — the operator's command line.

    python -m spotdesk.cli status                     agent + breaker + pipeline status
    python -m spotdesk.cli lookup <MPN>               RFQ-time market check
    python -m spotdesk.cli close <QUOTE#> won 12500 "reason"
                                                      label a deal (fires the trust loop)
    python -m spotdesk.cli release --deal <QUOTE#>    send all reviewed RFQ drafts on a deal
    python -m spotdesk.cli campaign --name X ...      cold-email engine (dry-run default)
    python -m spotdesk.cli followups [--send]         weekly threaded bumps
    python -m spotdesk.cli distill                    weekly report → Drafts
    python -m spotdesk.cli import-vendors file.csv    load the vendor panel
    python -m spotdesk.cli correct <email_id> <type> "reason"
                                                      teach the classifier
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config, db


def _gmail():
    from .gmail.client import GmailClient
    g = GmailClient()
    g.authenticate()
    return g


def cmd_status(_args) -> int:
    from .ops import breakers, health
    print(json.dumps({"health": health.check(), "breakers": breakers.status()},
                     indent=2, default=str))
    pipe = db.query("SELECT status, COUNT(*) AS n FROM deals WHERE outcome IS NULL "
                    "GROUP BY status ORDER BY n DESC")
    print("\npipeline:")
    for r in pipe:
        print(f"  {r['status']:<20} {r['n']}")
    return 0


def cmd_lookup(args) -> int:
    from .market import lookup
    sys.argv = ["lookup", args.mpn] + (["--json"] if args.json else [])
    return lookup.main()


def cmd_close(args) -> int:
    from .learn import outcomes
    deal = db.query_one("SELECT id FROM deals WHERE quote_number = ?", (args.quote,))
    if not deal:
        print(f"no deal {args.quote}")
        return 1
    res = outcomes.close_deal(deal["id"], outcome=args.outcome,
                              clearing_price_usd=args.price, reason=args.reason or "")
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_release(args) -> int:
    """Batch-release reviewed drafts: all vendor RFQs on a deal in one command."""
    from .hitl import drafts
    deal = db.query_one("SELECT id, quote_number FROM deals WHERE quote_number = ?",
                        (args.deal,))
    if not deal:
        print(f"no deal {args.deal}")
        return 1
    rows = db.query("SELECT id, kind, to_email FROM outbound_actions "
                    "WHERE deal_id = ? AND status = 'drafted'"
                    + (" AND kind = ?" if args.kind else ""),
                    (deal["id"], args.kind) if args.kind else (deal["id"],))
    if not rows:
        print("nothing drafted on this deal")
        return 0
    g = _gmail()
    import time
    ok = 0
    for r in rows:
        res = drafts.release(g, r["id"])
        print(f"  {r['kind']:<16} → {r['to_email']:<40} "
              f"{'sent' if res.get('ok') else res.get('reason')}")
        if res.get("ok"):
            ok += 1
            time.sleep(config.RFQ_SEND_SPACING_SECONDS)
    print(f"released {ok}/{len(rows)}")
    return 0


def cmd_campaign(args) -> int:
    from .outreach import campaign
    g = _gmail()
    if args.test:
        res = campaign.send_test(g, args.name, args.test, subject=args.subject or "")
        print(json.dumps(res, indent=2))
        return 0
    if not args.recipients:
        print("--recipients CSV required (headers: email, company[, name, type])")
        return 1
    recipients = campaign.load_recipients_csv(args.recipients)
    res = campaign.run(g, args.name, recipients, send=args.send,
                       limit=args.limit, subject=args.subject or "")
    print(json.dumps(res, indent=2))
    return 0 if not res.get("errors") else 1


def cmd_followups(args) -> int:
    from .outreach import followups
    res = followups.run(_gmail(), min_days_since_touch=args.days,
                        limit=args.limit, send=args.send)
    print(json.dumps(res, indent=2))
    return 0


def cmd_bounces(_args) -> int:
    from .outreach import campaign
    print(json.dumps(campaign.sweep_bounces(_gmail()), indent=2))
    return 0


def cmd_distill(_args) -> int:
    from .learn import distill
    distill.weekly_report(_gmail())
    print("weekly distill drafted to the oversight inbox")
    return 0


def cmd_import_vendors(args) -> int:
    """CSV headers: email, name, company, brands (';'-separated, ANY=broker),
    country, currency, priority."""
    import csv
    n = 0
    with open(args.file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            email = (row.get("email") or "").strip().lower()
            if "@" not in email:
                continue
            brands = [b.strip() for b in (row.get("brands") or "ANY").split(";") if b.strip()]
            prio = None
            try:
                prio = int(row.get("priority")) if (row.get("priority") or "").strip() else None
            except ValueError:
                pass
            db.execute(
                "INSERT INTO counterparties (email, name, company, domain, kind, brands, "
                "country, currency, priority) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET kind = CASE WHEN counterparties.kind='buyer' "
                "THEN 'both' ELSE counterparties.kind END, brands = excluded.brands, "
                "country = excluded.country, currency = excluded.currency, "
                "priority = excluded.priority",
                (email, (row.get("name") or "").strip() or None,
                 (row.get("company") or "").strip() or None, email.split("@")[-1],
                 "vendor", db.j(brands), (row.get("country") or "").strip() or None,
                 (row.get("currency") or "USD").strip().upper(), prio))
            n += 1
    print(f"imported/updated {n} vendors")
    db.audit("vendors_imported", {"n": n, "file": args.file}, actor="human")
    return 0


def cmd_correct(args) -> int:
    """Record a classification correction — future similar mail classifies right."""
    row = db.query_one("SELECT * FROM emails WHERE id = ?", (args.email_id,))
    if not row:
        print("no such email row")
        return 1
    db.insert("classification_feedback", {
        "email_id": row["id"], "subject": row.get("subject"),
        "body_snippet": (row.get("body_text") or "")[:500],
        "from_email": row.get("from_email"),
        "agent_classification": row.get("email_type"),
        "human_correction": args.correct_type, "reason": args.reason or ""})
    db.update("emails", {"email_type": args.correct_type}, "id = ?", (row["id"],))
    print(f"recorded: {row.get('email_type')} → {args.correct_type}")
    return 0


def main() -> int:
    db.init_schema()
    p = argparse.ArgumentParser(prog="desk", description="SpotDesk operator CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    sp = sub.add_parser("lookup")
    sp.add_argument("mpn")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("close")
    sp.add_argument("quote")
    sp.add_argument("outcome", choices=["won", "lost", "no_response", "abandoned"])
    sp.add_argument("price", nargs="?", type=float, default=None)
    sp.add_argument("reason", nargs="?", default="")

    sp = sub.add_parser("release")
    sp.add_argument("--deal", required=True)
    sp.add_argument("--kind", default=None,
                    help="restrict to one kind (e.g. vendor_rfq)")

    sp = sub.add_parser("campaign")
    sp.add_argument("--name", required=True)
    sp.add_argument("--recipients", help="CSV path")
    sp.add_argument("--subject", default="")
    sp.add_argument("--test", metavar="EMAIL", help="send ONE proof email and stop")
    sp.add_argument("--send", action="store_true")
    sp.add_argument("--limit", type=int, default=0)

    sp = sub.add_parser("followups")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--send", action="store_true")

    sub.add_parser("bounces")
    sub.add_parser("distill")

    sp = sub.add_parser("import-vendors")
    sp.add_argument("file")

    sp = sub.add_parser("correct")
    sp.add_argument("email_id", type=int)
    sp.add_argument("correct_type")
    sp.add_argument("reason", nargs="?", default="")

    args = p.parse_args()
    return {"status": cmd_status, "lookup": cmd_lookup, "close": cmd_close,
            "release": cmd_release, "campaign": cmd_campaign,
            "followups": cmd_followups, "bounces": cmd_bounces,
            "distill": cmd_distill, "import-vendors": cmd_import_vendors,
            "correct": cmd_correct}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
