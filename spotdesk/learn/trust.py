"""Counterparty trust — the multiplier the router and gates actually read.

Clamped [0, 100]; every change is audited with its reason so a score is always
explainable. No-response is deliberately neutral (censored ≠ bad)."""

from __future__ import annotations

import logging

from .. import config, db

log = logging.getLogger("spotdesk.trust")


def update_trust(email: str, delta: int, reason: str) -> int:
    email = (email or "").lower()
    row = db.query_one("SELECT trust_score FROM counterparties WHERE email = ?", (email,))
    if not row:
        return -1
    new = max(0, min(100, (row.get("trust_score") or config.DEFAULT_TRUST_SCORE) + delta))
    db.update("counterparties", {"trust_score": new}, "email = ?", (email,))
    db.audit("trust_update", {"email": email, "delta": delta, "new": new, "reason": reason})
    return new


def on_deal_won(email: str) -> int:
    return update_trust(email, +config.TRUST_BOOST_PER_WON, "deal_won")


def on_deal_lost(email: str) -> int:
    return update_trust(email, -config.TRUST_PENALTY_PER_LOST, "deal_lost")


def on_bounce(email: str) -> int:
    return update_trust(email, -config.TRUST_PENALTY_PER_BOUNCE, "bounce")


def blacklist(email: str, reason: str = "") -> int:
    db.update("counterparties",
              {"blacklisted": 1, "trust_score": 0, "notes": reason or "blacklisted"},
              "email = ?", ((email or "").lower(),))
    db.audit("blacklist", {"email": email, "reason": reason}, actor="human")
    return 0


def trust_factor(score: int | None) -> float:
    """0-100 → [0.5, 1.5] multiplier for scoring/routing."""
    s = score if score is not None else config.DEFAULT_TRUST_SCORE
    return 0.5 + (s / 100.0)
