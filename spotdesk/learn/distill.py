"""Weekly distill — the DISTILL edge: refresh the numbers, surface the surprises.

Refreshes vendor scores + daily KPI snapshot, then builds a short report of the week
(closes, win rate, margins, breaker states, biggest predicted-vs-actual surprises)
and puts it in the operator's Drafts addressed to the oversight team. Surprises are
what the human should consider promoting into a playbook edit — heuristics absorb
what the numbers can't yet capture.
"""

from __future__ import annotations

import html as html_mod
import logging
from datetime import datetime, timedelta, timezone

from .. import config, db
from ..hitl import drafts
from ..ops import breakers
from . import scorecard

log = logging.getLogger("spotdesk.distill")


def snapshot_kpis() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()

    def _n(sql, params=()):
        return (db.query_one(sql, params) or {}).get("n", 0) or 0

    row = {
        "date": today,
        "open_deals": _n("SELECT COUNT(*) AS n FROM deals WHERE outcome IS NULL"),
        "drafts_pending": _n("SELECT COUNT(*) AS n FROM outbound_actions WHERE status='drafted'"),
        "quotes_sent": _n("SELECT COUNT(*) AS n FROM outbound_actions "
                          "WHERE kind='quote' AND status IN ('sent','auto_sent') "
                          "AND sent_at >= ?", (today,)),
        "rfqs_sent": _n("SELECT COUNT(*) AS n FROM outbound_actions "
                        "WHERE kind='vendor_rfq' AND status IN ('sent','auto_sent') "
                        "AND sent_at >= ?", (today,)),
        "pos_placed": _n("SELECT COUNT(*) AS n FROM vendor_pos WHERE placed_at >= ?", (today,)),
        "deals_won": _n("SELECT COUNT(*) AS n FROM deals WHERE outcome='won' "
                        "AND closed_at >= ?", (today,)),
        "deals_lost": _n("SELECT COUNT(*) AS n FROM deals WHERE outcome='lost' "
                         "AND closed_at >= ?", (today,)),
        "cold_sends": _n("SELECT COUNT(*) AS n FROM outreach_sends WHERE sent_at >= ?", (today,)),
    }
    won = db.query_one("SELECT SUM(clearing_price_usd) AS s FROM deals "
                       "WHERE outcome='won' AND closed_at >= ?", (today,))
    row["revenue_won_usd"] = (won or {}).get("s") or 0
    db.execute("INSERT OR REPLACE INTO system_kpis (date, open_deals, drafts_pending, "
               "quotes_sent, rfqs_sent, pos_placed, deals_won, deals_lost, "
               "revenue_won_usd, cold_sends) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (row["date"], row["open_deals"], row["drafts_pending"], row["quotes_sent"],
                row["rfqs_sent"], row["pos_placed"], row["deals_won"], row["deals_lost"],
                row["revenue_won_usd"], row["cold_sends"]))
    return row


def weekly_report(gmail=None) -> str:
    scorecard.compute_vendor_scores()
    snapshot_kpis()
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    closed = db.query("SELECT quote_number, customer_company, outcome, we_quoted_usd, "
                      "clearing_price_usd, loss_reason FROM deals "
                      "WHERE closed_at >= ? ORDER BY closed_at DESC", (since,))
    won = [d for d in closed if d["outcome"] == "won"]
    lost = [d for d in closed if d["outcome"] in ("lost", "abandoned")]
    censored = [d for d in closed if d["outcome"] == "no_response"]
    decided = len(won) + len(lost)
    win_rate = (len(won) / decided * 100) if decided else None

    # Surprises: quotes that lost with a stated reason, or clearing far off our quote.
    surprises = []
    for d in closed:
        if d["outcome"] == "won" and d.get("clearing_price_usd") and d.get("we_quoted_usd"):
            try:
                drift = abs(d["clearing_price_usd"] - d["we_quoted_usd"]) / d["we_quoted_usd"]
                if drift > 0.10:
                    surprises.append(f"{d['quote_number']}: cleared "
                                     f"{drift * 100:.0f}% off our quote")
            except ZeroDivisionError:
                pass
        if d["outcome"] == "lost" and d.get("loss_reason"):
            surprises.append(f"{d['quote_number']}: lost — {d['loss_reason']}")

    brk = breakers.status()
    esc = html_mod.escape
    lines = [f"<h3>Desk week in review (since {since})</h3>",
             f"<p>Closed: <strong>{len(closed)}</strong> — won {len(won)}, lost {len(lost)}, "
             f"no-response (censored) {len(censored)}."
             + (f" Win rate on decided: <strong>{win_rate:.0f}%</strong>." if win_rate is not None else "")
             + "</p>"]
    if won:
        rev = sum(d.get("clearing_price_usd") or 0 for d in won)
        lines.append(f"<p>Revenue won: <strong>${rev:,.0f}</strong></p>")
    if surprises:
        lines.append("<p><strong>Surprises worth a playbook edit:</strong></p><ul>"
                     + "".join(f"<li>{esc(s)}</li>" for s in surprises[:8]) + "</ul>")
    lines.append("<p><strong>Breakers:</strong> "
                 + "; ".join(f"{k}: {'OK' if v['ok'] else 'HALT'} ({esc(v['reason'])})"
                             for k, v in brk.items()) + "</p>")
    top = db.query("SELECT name, company, email, score, quotes_won, rfqs_replied, rfqs_sent "
                   "FROM counterparties WHERE score IS NOT NULL "
                   "ORDER BY score DESC LIMIT 5")
    if top:
        lines.append("<p><strong>Top vendors by learned score:</strong></p><ul>"
                     + "".join(f"<li>{esc(str(v.get('name') or v.get('company') or v['email']))} — "
                               f"{v['score']} ({v['quotes_won']} wins, "
                               f"{v['rfqs_replied']}/{v['rfqs_sent']} replies)</li>"
                               for v in top) + "</ul>")
    body = "<div style='font-family:Arial,sans-serif;font-size:14px;color:#222;'>" \
           + "".join(lines) + "</div>"

    if gmail is not None:
        to = config.OVERSIGHT_CC[0] if config.OVERSIGHT_CC else config.TEST_EMAIL
        if to:
            drafts.dispatch(gmail, kind="report", to=to,
                            subject=f"Desk weekly distill — {datetime.now(timezone.utc).date()}",
                            body_html=body, note="weekly distill", force_draft=True)
    db.audit("weekly_distill", {"closed": len(closed), "surprises": len(surprises)})
    return body
