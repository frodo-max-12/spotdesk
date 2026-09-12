"""Line card + brand normalization.

The line card (line_card.json) answers "are we an authorized distributor for this
brand, and which of our lines make this kind of part?". Brand aliasing collapses the
many spellings customers use ('ST' / 'STMicro' / 'ST Microelectronics') onto one
canonical token so routing and authorization checks actually match.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_LINE_CARD_PATH = Path(__file__).resolve().parent / "line_card.json"

# canonical UPPER token -> variants (matched case-insensitively)
_GROUPS = {
    "STM": ["ST", "STM", "STMICRO", "STMICROELECTRONICS", "ST MICROELECTRONICS", "ST MICRO"],
    "TI": ["TI", "TEXAS", "TEXAS INSTRUMENTS", "TEXAS INSTRUMENT", "BURR BROWN", "NATIONAL SEMICONDUCTOR"],
    "ONSEMI": ["ON", "ONSEMI", "ON SEMI", "ON SEMICONDUCTOR", "ONSEMICONDUCTOR", "FAIRCHILD"],
    "NXP": ["NXP", "NXP SEMICONDUCTORS", "FREESCALE"],
    "INFINEON": ["INFINEON", "IFX", "CYPRESS", "INTERNATIONAL RECTIFIER", "IR"],
    "MICROCHIP": ["MICROCHIP", "ATMEL", "MCHP", "MICROSEMI"],
    "RENESAS": ["RENESAS", "INTERSIL", "IDT", "DIALOG"],
    "ADI": ["ADI", "ANALOG", "ANALOG DEVICES", "MAXIM", "MAXIM INTEGRATED", "LINEAR", "LINEAR TECHNOLOGY", "LTC"],
    "VISHAY": ["VISHAY", "DALE", "SILICONIX"],
    "MICRON": ["MICRON", "SPECTEK"],
    "SAMSUNG": ["SAMSUNG"],
    "SKHYNIX": ["HYNIX", "SK HYNIX", "SKHYNIX"],
    "TOSHIBA": ["TOSHIBA", "KIOXIA"],
    "MURATA": ["MURATA"],
    "TDK": ["TDK", "EPCOS"],
    "YAGEO": ["YAGEO", "PHYCOMP"],
    "KEMET": ["KEMET"],
    "AVX": ["AVX", "KYOCERA"],
    "BOURNS": ["BOURNS"],
    "PANASONIC": ["PANASONIC", "MATSUSHITA"],
    "NICHICON": ["NICHICON"],
    "ROHM": ["ROHM"],
    "DIODES": ["DIODES", "DIODES INC", "DIODES INCORPORATED", "ZETEX"],
    "WURTH": ["WURTH", "WURTH ELEKTRONIK", "WUERTH"],
    "TE": ["TE", "TE CONNECTIVITY", "TYCO", "AMP"],
    "MOLEX": ["MOLEX"],
    "AMPHENOL": ["AMPHENOL", "AMPHENOL INDIA", "FCI"],
    "HIROSE": ["HIROSE", "HRS"],
    "JST": ["JST"],
    "LITTELFUSE": ["LITTELFUSE", "LITTLEFUSE"],
    "MARVELL": ["MARVELL"],
    "BROADCOM": ["BROADCOM", "AVAGO", "LSI"],
    "WESTERN-DIGITAL": ["WD", "WESTERN DIGITAL", "WESTERN-DIGITAL", "SANDISK"],
    "SEAGATE": ["SEAGATE"],
    "INTEL": ["INTEL", "ALTERA"],
    "AMD": ["AMD", "XILINX"],
    "NVIDIA": ["NVIDIA"],
    "MORNSUN": ["MORNSUN"],
    "SILERGY": ["SILERGY"],
    "SGMICRO": ["SGMICRO", "SG MICRO"],
    "GOFORD": ["GOFORD"],
    "SMC": ["SMC", "SMC DIODE", "SMC DIODE SOLUTIONS"],
    "COILMASTER": ["COILMASTER"],
    "STANDEX": ["STANDEX", "STANDEX MEDER", "MEDER"],
    "KLS": ["KLS"],
    "EVERLIGHT": ["EVERLIGHT"],
    "NETSOL": ["NETSOL"],
    "CLAFPOWER": ["CLAFPOWER", "CLAF", "CLAF POWER"],
    "ABRACON": ["ABRACON"],
    "BELFUSE": ["BELFUSE", "BEL FUSE"],
    "PULSE": ["PULSE", "PULSE ELECTRONICS"],
    "SUMIDA": ["SUMIDA"],
    "SCHURTER": ["SCHURTER"],
    "PHOENIX": ["PHOENIX", "PHOENIX CONTACT"],
    "CDIL": ["CDIL", "CONTINENTAL DEVICE"],
}

_ALIAS: dict[str, str] = {}
for _canon, _variants in _GROUPS.items():
    _ALIAS[_canon] = _canon
    for _v in _variants:
        _ALIAS[_v.upper().strip()] = _canon

_SUFFIX = re.compile(
    r"\b(SEMICONDUCTORS?|TECHNOLOG(?:Y|IES)|ELECTRONICS?|MICROELECTRONICS|"
    r"CORP(?:ORATION)?|INC|LTD|LLC|GMBH|CO|COMPANY|INTERNATIONAL|SOLUTIONS?|"
    r"PTE|PVT|INDIA)\b", re.I)


def canonical(brand: str) -> str:
    """Canonical UPPER token for a brand / manufacturer string."""
    if not brand:
        return ""
    b = re.sub(r"\s+", " ", str(brand).strip().upper())
    if b in _ALIAS:
        return _ALIAS[b]
    stripped = re.sub(r"\s+", " ", _SUFFIX.sub("", b)).strip(" .,-")
    if stripped in _ALIAS:
        return _ALIAS[stripped]
    tok = stripped.split(" ")[0] if stripped else b
    return _ALIAS.get(tok, stripped or b)


def brand_match(requested: str, vendor_brands) -> bool:
    """True if the requested manufacturer matches any of the vendor's brands after
    normalization. ['ANY'] / ['OPEN-MARKET'] never brand-match here — brokers are
    added separately by the router."""
    if not requested:
        return False
    rc = canonical(requested)
    return bool(rc) and any(canonical(vb) == rc for vb in (vendor_brands or []))


class LineCard:
    """Authorized-line lookups over line_card.json."""

    def __init__(self, path: Path | None = None):
        self.path = path or _LINE_CARD_PATH
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {"categories": {}}
        self._brands: dict[str, dict] = {}
        for cat_key, cat in (self.data.get("categories") or {}).items():
            for b in cat.get("brands", []):
                self._brands[canonical(b.get("name", ""))] = {
                    "name": b.get("name"),
                    "category": cat.get("display_name", cat_key),
                    "products": b.get("products", []),
                }

    def all_brands(self) -> list[str]:
        return [b["name"] for b in self._brands.values()]

    def is_authorized(self, manufacturer: str) -> bool:
        return canonical(manufacturer) in self._brands

    def brand_info(self, manufacturer: str) -> dict | None:
        return self._brands.get(canonical(manufacturer))

    def summary(self) -> str:
        """Compact 'brand — category: products' listing for cross-reference prompts."""
        lines = []
        for b in self._brands.values():
            prods = ", ".join(b["products"][:6])
            lines.append(f"- {b['name']} ({b['category']}): {prods}")
        return "\n".join(lines)

    def brands_for_keywords(self, text: str) -> list[dict]:
        """Authorized brands whose product list mentions any keyword from `text` —
        used to route description-only lines ('DC-DC converter 24V…') to the lines
        that actually make that kind of part."""
        words = {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())}
        out = []
        for b in self._brands.values():
            blob = " ".join(b["products"]).lower()
            if any(w in blob for w in words):
                out.append(b)
        return out


@lru_cache(maxsize=1)
def line_card() -> LineCard:
    return LineCard()
