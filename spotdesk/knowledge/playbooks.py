"""Playbook loader — prose knowledge lives in git-versioned markdown, not code.

Operators (and the weekly distill pass) edit the .md files; the agent re-reads them on
every decision, so a playbook edit changes behaviour with no deploy. Numbers belong in
SQL; judgment belongs here.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from .. import config

log = logging.getLogger("spotdesk.playbooks")


@lru_cache(maxsize=16)
def _read(name: str) -> str:
    path = config.PLAYBOOKS_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("playbook missing: %s", path)
        return ""


def reload() -> None:
    """Drop the cache (the agent calls this at the top of each poll cycle so live
    playbook edits take effect within a minute)."""
    _read.cache_clear()


def market_conventions() -> str:
    return _read("MARKET_CONVENTIONS.md")


def broker_playbook() -> str:
    return _read("BROKER_PLAYBOOK.md")


def reply_style() -> str:
    return _read("REPLY_STYLE.md")


def outreach_playbook() -> str:
    return _read("OUTREACH_PLAYBOOK.md")


def classification_context() -> str:
    """Business context that helps the classifier tell a customer RFQ from a vendor
    stock-offer blast — the two most-confused classes on this desk."""
    return (market_conventions()
            + "\n\nClassification hints from the real mailbox:\n"
            "- A VENDOR 'offer' / 'available stock' / daily 'DRAM offer' blast (a supplier\n"
            "  pushing an unsolicited price list AT us) is vendor market intel, not a customer rfq.\n"
            "- A CUSTOMER rfq leads with a specific part number + firm quantity and asks US to\n"
            "  quote (price / stock / lead time / date code); it may reply into one of our own\n"
            "  offer threads.")


def extraction_context() -> str:
    """Trade conventions for reading vendor cost replies (date codes, EXW, declines)."""
    return market_conventions()


def negotiation_context() -> str:
    return broker_playbook()
