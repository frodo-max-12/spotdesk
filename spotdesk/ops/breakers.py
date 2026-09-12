"""Circuit breakers — guardrails that stop the loop doing something catastrophic
when something else has already gone wrong. All return (ok: bool, reason: str);
senders and the pricing gate call them before any send-or-commit action.

Active breakers:
  check_send_rate       — MAX_OUTBOUND_PER_DAY across all agent-sent mail
  check_cold_send_rate  — cold-outreach ramp by lifetime volume + daily cap
  check_bounce_rate     — rolling bounce % over threshold → halt cold sends
  check_anomaly         — price far outside the SKU's median band → parsing artifact, halt
  check_capital_exposure— per-SKU / per-counterparty open-commitment caps
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import config, db


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def check_send_rate() -> tuple[bool, str]:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM outbound_actions "
        "WHERE status IN ('sent','auto_sent') AND sent_at >= ?", (_today(),))
    n = (row or {}).get("n", 0) or 0
    if n >= config.MAX_OUTBOUND_PER_DAY:
        return False, f"daily send cap reached: {n}/{config.MAX_OUTBOUND_PER_DAY}"
    return True, f"send budget {n}/{config.MAX_OUTBOUND_PER_DAY}"


def _lifetime_cold_sends() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM outreach_sends")
    return (row or {}).get("n", 0) or 0


def cold_daily_cap() -> int:
    """Volume ramp: newer sender accounts get flagged for cold-style volume, so the
    per-day cap scales with lifetime history."""
    lifetime = _lifetime_cold_sends()
    for threshold, cap in config.COLD_RAMP:
        if lifetime < threshold:
            return min(cap, config.MAX_COLD_SENDS_PER_DAY) if config.MAX_COLD_SENDS_PER_DAY else cap
    return config.MAX_COLD_SENDS_PER_DAY


def check_cold_send_rate() -> tuple[bool, str]:
    cap = cold_daily_cap()
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM outreach_sends WHERE sent_at >= ?", (_today(),))
    n = (row or {}).get("n", 0) or 0
    if n >= cap:
        return False, f"cold daily cap reached: {n}/{cap} (lifetime {_lifetime_cold_sends()})"
    return True, f"cold budget {n}/{cap}"


def check_bounce_rate(window_days: int = 7) -> tuple[bool, str]:
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    sent = (db.query_one("SELECT COUNT(*) AS n FROM outreach_sends WHERE sent_at >= ?",
                         (since,)) or {}).get("n", 0) or 0
    if sent < 20:
        return True, "not enough sends to measure"
    bounced = (db.query_one(
        "SELECT COUNT(*) AS n FROM outreach_sends WHERE sent_at >= ? AND status = 'bounced'",
        (since,)) or {}).get("n", 0) or 0
    rate = bounced / sent * 100
    if rate > config.BOUNCE_RATE_HALT_PCT:
        return False, f"bounce rate {rate:.1f}% > halt threshold {config.BOUNCE_RATE_HALT_PCT}%"
    return True, f"bounce rate {rate:.1f}% ({bounced}/{sent})"


def check_anomaly(price_usd: float, median_for_sku: float | None) -> tuple[bool, str]:
    """A price wildly off the SKU's median is far more likely a parsing artifact than a
    deal of the century — halt that line for review instead of quoting it."""
    if not median_for_sku or median_for_sku <= 0:
        return True, "no median reference"
    mult = config.ANOMALY_PRICE_MULTIPLIER
    if price_usd > median_for_sku * mult:
        return False, f"price ${price_usd:,.2f} > {mult}x median ${median_for_sku:,.2f}"
    if price_usd < median_for_sku / mult:
        return False, f"price ${price_usd:,.2f} < median/{mult} — likely parsing error"
    return True, "price within band"


def check_capital_exposure(counterparty_email: str, proposed_usd: float) -> tuple[bool, str]:
    """Block a commit that would push open exposure past the per-counterparty cap
    (open exposure = quoted minus closed)."""
    row = db.query_one(
        "SELECT (COALESCE(total_quoted_usd,0) - COALESCE(total_closed_usd,0)) AS exposure "
        "FROM counterparties WHERE email = ?", ((counterparty_email or "").lower(),))
    exposure = (row or {}).get("exposure", 0) or 0
    cap = config.MAX_CAPITAL_PER_COUNTERPARTY_USD
    if exposure + proposed_usd > cap:
        return False, (f"per-counterparty exposure cap: {counterparty_email} "
                       f"${exposure:,.0f} + ${proposed_usd:,.0f} > ${cap:,.0f}")
    return True, "exposure ok"


def status() -> dict:
    """Every breaker's current state (dashboard card)."""
    out = {}
    for name, fn in (("send_rate", check_send_rate),
                     ("cold_send_rate", check_cold_send_rate),
                     ("bounce_rate", check_bounce_rate)):
        ok, reason = fn()
        out[name] = {"ok": ok, "reason": reason}
    return out
