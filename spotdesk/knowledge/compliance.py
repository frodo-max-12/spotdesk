"""Compliance screening — two layers, both cheap, both non-optional for semi exports.

1. Counterparty screening (before any outbound to a new counterparty):
   internal blacklist → BIS Denied Persons → OFAC SDN. List files are plain text,
   one entry per line ("Name|Address|Country"); ships empty — populate before going
   live with unknown counterparties (the check WARNS loudly while empty).

2. Part screening (per quoted line): dual-use / EUD keyword watchlist — FPGAs,
   high-speed converters, RF/microwave, rad-hard/MIL, crypto. A hit tags the line
   "Requires End-User Declaration" and holds the quote for a human. This is a
   screening aid, not a legal determination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from .. import db

# ---- part screening (EUD / dual-use) ----

_EUD_RULES = {
    "FPGA / programmable logic": [
        r"\bfpga\b", r"\bcpld\b", r"field[- ]programmable",
        r"\bzynq\b", r"\bvirtex\b", r"\bkintex\b", r"\bartix\b", r"\bstratix\b",
        r"\barria\b", r"\bcyclone\b", r"\bagilex\b",
    ],
    "High-speed data converter": [
        r"high[- ]speed\s+(adc|dac)", r"\bgsps\b", r"\d+\s*gsps", r"rf\s+(adc|dac)",
    ],
    "RF / microwave": [
        r"\brf\b\s*(power\s*)?(amp|amplifier|transceiver|frontend|front[- ]end)",
        r"microwave", r"\bgan\b", r"\bgaas\b", r"\bmmic\b",
        r"transceiver", r"power\s+amplifier",
    ],
    "Rad-hard / space / military grade": [
        r"rad[- ]?hard", r"radiation[- ]tolerant", r"space[- ]grade", r"\bqml\b",
        r"mil[- ]?spec", r"mil[- ]?std", r"\bmilitary\b", r"aerospace",
    ],
    "Cryptographic / security": [
        r"crypto", r"cryptographic", r"secure\s+element", r"\bhsm\b", r"\btpm\b",
        r"encryption",
    ],
}


def _extra_eud_keywords() -> dict[str, str]:
    out = {}
    for chunk in (config.EUD_EXTRA_KEYWORDS or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            kw, _, cat = chunk.partition(":")
            out[kw.strip().lower()] = cat.strip() or "User watchlist"
        else:
            out[chunk.lower()] = "User watchlist"
    return out


def check_eud(mpn: str = "", manufacturer: str = "", description: str = "") -> tuple[bool, str | None]:
    """(flagged, reason). Reason names the dual-use category matched."""
    blob = " ".join(str(x or "") for x in (mpn, manufacturer, description)).lower()
    if not blob.strip():
        return False, None
    for category, patterns in _EUD_RULES.items():
        for pat in patterns:
            if re.search(pat, blob):
                return True, f"Requires End-User Declaration — {category}"
    for kw, cat in _extra_eud_keywords().items():
        if kw and kw in blob:
            return True, f"Requires End-User Declaration — {cat}"
    return False, None


# ---- counterparty screening (BIS / OFAC / blacklist) ----

@dataclass
class ScreenResult:
    cleared: bool
    severity: str                       # critical / warning / ok
    findings: list[str] = field(default_factory=list)
    sources_checked: list[str] = field(default_factory=list)


def _load_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip().lower() for ln in path.read_text(errors="replace").splitlines()
            if ln.strip() and not ln.startswith("#")]


def _name_match(needle: str, entries: list[str]) -> str | None:
    """Conservative substring match on the name field; short/generic tokens are
    skipped so 'ltd' never matches every entry."""
    if not needle or len(needle) < 5:
        return None
    nl = needle.lower()
    if nl in ("noreply", "support", "info", "sales", "admin"):
        return None
    for entry in entries:
        name = entry.split("|", 1)[0]
        if name and (nl in name or name in nl):
            return entry
    return None


def screen_counterparty(email: str, company: str | None = None) -> ScreenResult:
    findings: list[str] = []
    sources: list[str] = []

    row = db.query_one("SELECT blacklisted, notes FROM counterparties WHERE email = ?",
                       ((email or "").lower(),))
    sources.append("internal_blacklist")
    if row and row.get("blacklisted"):
        findings.append(f"INTERNAL BLACKLIST: {row.get('notes') or 'no reason recorded'}")
        return ScreenResult(False, "critical", findings, sources)

    bis = _load_list(config.BIS_DENIED_PARTIES_FILE)
    sources.append(f"BIS_denied ({len(bis)})")
    if bis and company:
        hit = _name_match(company, bis)
        if hit:
            findings.append(f"BIS DENIED PARTIES match: {hit}")
            return ScreenResult(False, "critical", findings, sources)

    ofac = _load_list(config.OFAC_SDN_FILE)
    sources.append(f"OFAC_SDN ({len(ofac)})")
    if ofac and company:
        hit = _name_match(company, ofac)
        if hit:
            findings.append(f"OFAC SDN match: {hit}")
            return ScreenResult(False, "critical", findings, sources)

    if not bis and not ofac:
        findings.append(f"WARN: BIS/OFAC list files empty — populate "
                        f"{config.BIS_DENIED_PARTIES_FILE} and {config.OFAC_SDN_FILE} "
                        f"before autonomous sends to unknown counterparties.")
        return ScreenResult(True, "warning", findings, sources)

    return ScreenResult(True, "ok", findings, sources)


def record_flag(email: str, result: ScreenResult) -> None:
    db.insert("compliance_flags", {
        "counterparty_email": email,
        "list_source": ",".join(result.sources_checked),
        "severity": result.severity,
        "action_required": "; ".join(result.findings),
    })
    db.audit("compliance_block", {"email": email, "findings": result.findings})
