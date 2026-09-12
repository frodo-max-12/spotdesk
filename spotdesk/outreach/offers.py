"""Offer pool — the anti-forwarding SKU subset engine.

Broker offer mail gets forwarded around the industry; if three brokers forward the
same list, buyers realize everyone is selling the same source's inventory. So every
company gets its OWN 5-SKU permutation with jittered ugly quantities, and the combo
each company received is remembered so future batches keep minimizing overlap.

The pool itself lives in data/offer_pool.json (operator-maintained, refreshed from
whatever the desk currently has to move):

    {"skus": {"<key>": {"mpn": "...", "mfg": "...", "family": "...", "dc": "25+",
                        "base_qty": 6000, "types": ["server", "ai"]}},
     "used_combos": {"<company>": ["key1", ...]}}
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import logging
import random
from pathlib import Path

from .. import config

log = logging.getLogger("spotdesk.offers")

POOL_PATH = config.DATA_DIR / "offer_pool.json"


def load_pool() -> dict:
    try:
        return json.loads(POOL_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"skus": {}, "used_combos": {}}


def save_pool(pool: dict) -> None:
    POOL_PATH.write_text(json.dumps(pool, indent=2), encoding="utf-8")


def _jitter_qty(base: int, company: str, sku_key: str) -> int:
    """±20-30% deterministic jitter (same company+sku always renders the same number,
    so re-runs and colleagues at one company see consistent quantities), rounded to
    'ugly' numbers — 220, 880, 10,500 — never 250 / 1000 / 10000."""
    seed = int(hashlib.sha256(f"{company}|{sku_key}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    factor = rng.uniform(0.70, 1.30)
    q = max(20, int(base * factor))
    if q >= 10000:
        q = int(round(q / 500) * 500)
    elif q >= 1000:
        q = int(round(q / 100) * 100)
    else:
        q = int(round(q / 20) * 20)
    # Nudge away from too-round numbers.
    if q % 1000 == 0:
        q += rng.choice([-200, -100, 100, 200])
    return max(20, q)


def suggest_combo(company: str, type_key: str | None = None,
                  avoid_overlap_with: list[str] | None = None,
                  n: int | None = None) -> list[str]:
    """Pick n SKU keys for `company`, minimizing overlap with what this company already
    received AND with the given peer companies' combos."""
    pool = load_pool()
    skus = pool.get("skus", {})
    used = pool.get("used_combos", {})
    n = n or config.SKUS_PER_COMPANY

    candidates = [k for k, s in skus.items()
                  if not type_key or type_key in (s.get("types") or [])]
    if not candidates:
        candidates = list(skus.keys())
    if not candidates:
        return []

    penalty: dict[str, int] = {k: 0 for k in candidates}
    for prior in used.get(company, []):
        if prior in penalty:
            penalty[prior] += 2
    for peer in (avoid_overlap_with or []):
        for prior in used.get(peer, []):
            if prior in penalty:
                penalty[prior] += 1

    seed = int(hashlib.sha256(company.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed + len(used.get(company, [])))
    ranked = sorted(candidates, key=lambda k: (penalty[k], rng.random()))
    combo = ranked[:n]

    used.setdefault(company, [])
    used[company] = (used[company] + combo)[-4 * n:]     # remember recent history
    pool["used_combos"] = used
    save_pool(pool)
    return combo


def make_subject(sku_keys: list[str]) -> str:
    """Style-C subject: specific part-number anchor, plain hyphen, no date tag.
    'Samsung DDR5 64GB RDIMM (M321R8GA0EB2) - Spot Stock Available'"""
    pool = load_pool()
    skus = pool.get("skus", {})
    picks = [skus[k] for k in sku_keys if k in skus][:2]
    if not picks:
        return "Spot Stock Available"
    first = picks[0]
    part1 = f"{first.get('mfg', '')} {first.get('family', '')} ({first.get('mpn', '')})".strip()
    if len(picks) > 1:
        second = picks[1]
        part2 = f"{second.get('mfg', '')} {second.get('family', '')}".strip()
        return f"{part1} & {part2} - Spot Stock Available"
    return f"{part1} - Spot Stock Available"


def offer_table_html(company: str, sku_keys: list[str],
                     fixed_quantities: list[int] | None = None) -> str:
    """The inline stock table: Part Number | MFG | Family | DC | Qty. Minimal 1px
    borders, no images, no attachments. `fixed_quantities` (follow-ups) overrides the
    per-company jitter with the same numbers in the same row positions every week."""
    pool = load_pool()
    skus = pool.get("skus", {})
    esc = html_mod.escape
    cell = 'padding:5px 10px;border:1px solid #cccccc;'
    rows = ""
    for i, key in enumerate(sku_keys):
        s = skus.get(key)
        if not s:
            continue
        if fixed_quantities and i < len(fixed_quantities):
            qty = fixed_quantities[i]
        else:
            qty = _jitter_qty(int(s.get("base_qty") or 1000), company, key)
        rows += (f'<tr><td style="{cell}"><strong>{esc(str(s.get("mpn", "")))}</strong></td>'
                 f'<td style="{cell}">{esc(str(s.get("mfg", "")))}</td>'
                 f'<td style="{cell}">{esc(str(s.get("family", "")))}</td>'
                 f'<td style="{cell}">{esc(str(s.get("dc", "")))}</td>'
                 f'<td style="{cell}text-align:right;">{qty:,}</td></tr>')
    th = 'padding:5px 10px;border:1px solid #cccccc;text-align:left;background:#f5f5f5;'
    return ('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;'
            'font-family:Arial,sans-serif;font-size:13px;margin:10px 0;">'
            f'<tr><th style="{th}">Part Number</th><th style="{th}">MFG</th>'
            f'<th style="{th}">Family</th><th style="{th}">DC</th>'
            f'<th style="{th}">Qty</th></tr>' + rows + "</table>")


def signature_html() -> str:
    esc = html_mod.escape
    return (f'<p style="margin:16px 0 0;color:#333;">Kind Regards,<br>'
            f'{esc(config.SIGNOFF_NAME)}<br>'
            f'{esc(config.COMPANY_ENTITY)} | {esc(config.COMPANY_TAGLINE)}<br>'
            f'{esc(config.COMPANY_WEBSITE)}</p>')
