"""Shared market-lookup types + the parallel channel runner."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

HOME = Path.home()


def read_key_file(filename: str) -> Optional[str]:
    try:
        val = (HOME / filename).read_text().strip()
        return val or None
    except FileNotFoundError:
        return None


def read_json_creds(path: Path) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


@dataclass
class PriceBreak:
    qty: int
    unit_price_usd: float


@dataclass
class Quote:
    """One channel's answer for one MPN, normalized across every source."""
    distributor: str
    mpn: str
    found: bool = False
    manufacturer: Optional[str] = None
    description: Optional[str] = None
    distributor_pn: Optional[str] = None
    stock: Optional[int] = None
    moq: Optional[int] = None
    package: Optional[str] = None
    lead_time_days: Optional[int] = None
    price_breaks: list[PriceBreak] = field(default_factory=list)
    datasheet_url: Optional[str] = None
    product_url: Optional[str] = None
    error: Optional[str] = None

    @property
    def best_unit_price(self) -> Optional[float]:
        if not self.price_breaks:
            return None
        return min(pb.unit_price_usd for pb in self.price_breaks)

    @property
    def starting_unit_price(self) -> Optional[float]:
        if not self.price_breaks:
            return None
        return min(self.price_breaks, key=lambda pb: pb.qty).unit_price_usd

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_parallel(lookups: list[tuple[str, Callable[[str], object]]],
                 mpn: str, timeout_sec: int = 15) -> list:
    """Run each (name, fn) in a thread. A channel that raises or times out becomes an
    error Quote — one slow distributor never blocks an RFQ."""
    results: list = []
    if not lookups:
        return results
    with ThreadPoolExecutor(max_workers=len(lookups)) as ex:
        futures = {ex.submit(fn, mpn): name for name, fn in lookups}
        try:
            for fut in as_completed(futures, timeout=timeout_sec + 5):
                name = futures[fut]
                try:
                    results.append(fut.result(timeout=timeout_sec))
                except FuturesTimeout:
                    results.append(Quote(distributor=name, mpn=mpn, error="timeout"))
                except Exception as e:  # surface, never crash the desk
                    results.append(Quote(distributor=name, mpn=mpn,
                                         error=f"{type(e).__name__}: {e}"))
        except FuturesTimeout:
            done = {futures[f] for f in futures if f.done()}
            for f, name in futures.items():
                if name not in done and not f.done():
                    f.cancel()
                    results.append(Quote(distributor=name, mpn=mpn, error="timeout"))
    return results


def norm_mpn(s: str) -> str:
    return (s or "").strip().upper().replace("-", "").replace(" ", "").replace("_", "")
