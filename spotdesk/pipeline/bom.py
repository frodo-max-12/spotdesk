"""BOM extraction — email text/HTML, Excel (.xlsx/.xls), CSV, PDF.

The HTML path matters most: real RFQ tables arrive as Gmail HTML with jagged rows
(colspans, empty cells) and 15KB <style> blocks ahead of the parts table. The cleaner
below converts tables to aligned markdown so the model reads row structure reliably —
each guard here traces to a live email that broke a naive version.
"""

from __future__ import annotations

import io
import logging
import re
from html.parser import HTMLParser

from ..llm import brain

log = logging.getLogger("spotdesk.bom")

# Rough MPN shape — used only as a cheap signal (does this text contain part numbers?),
# never as the extractor itself.
MPN_PATTERN = re.compile(r"\b[A-Z]{1,5}[0-9][A-Z0-9\-/]{3,30}\b", re.IGNORECASE)


class _HTMLTableExtractor(HTMLParser):
    """HTML → clean text with markdown-style tables.

    Tags whose CONTENT is code/metadata (style/script/title) are skipped entirely —
    a vendor blast once carried a ~15KB <style> block that burned the token budget and
    pushed the actual rows past every downstream cap. Only tags WITH a real closing tag
    may be skip-counted: void elements (<meta>, <br>) never emit an end tag and would
    leave the skip depth stuck, silently swallowing the rest of the document."""

    _SKIP_CONTENT_TAGS = {"style", "script", "title"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._in_table = False
        self._in_cell = False
        self._cell_buf: list[str] = []
        self._row_cells: list[str] = []
        self._table_rows: list[list[str]] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self._SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t == "table":
            self._in_table = True
            self._table_rows = []
        elif t == "tr" and self._in_table:
            self._row_cells = []
        elif t in ("td", "th") and self._in_table:
            self._in_cell = True
            self._cell_buf = []
        elif t in ("br", "p", "div", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self._SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if t == "table" and self._in_table:
            self._in_table = False
            if self._table_rows:
                self.out.append("\n")
                # Real-world Gmail tables are JAGGED — pad every row to the widest so
                # per-column width indexing can't go out of range (the old crash dumped
                # raw HTML to the model and truncated a 20-part BOM to 6 rows).
                ncols = max(len(r) for r in self._table_rows)
                padded = [list(r) + [""] * (ncols - len(r)) for r in self._table_rows]
                widths = [max(len(str(row[j])) for row in padded) for j in range(ncols)]
                for i, row in enumerate(padded):
                    self.out.append(" | ".join(str(c).ljust(widths[j])
                                               for j, c in enumerate(row)) + "\n")
                    if i == 0 and len(padded) > 1:
                        self.out.append(" | ".join("-" * w for w in widths) + "\n")
                self.out.append("\n")
        elif t == "tr" and self._in_table:
            if self._row_cells:
                self._table_rows.append(self._row_cells)
        elif t in ("td", "th") and self._in_table:
            self._in_cell = False
            self._row_cells.append(" ".join("".join(self._cell_buf).split()))

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_cell:
            self._cell_buf.append(data)
        elif not self._in_table:
            self.out.append(data)

    def get_text(self) -> str:
        return "".join(self.out)


def html_to_clean_text(html: str) -> str:
    if not html or "<" not in html:
        return html or ""
    try:
        parser = _HTMLTableExtractor()
        parser.feed(html)
        text = parser.get_text()
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        return text.strip()
    except Exception as e:
        log.warning("HTML cleaning failed, using raw: %s", e)
        return html


def best_body(body_text: str, body_html: str, min_len: int = 50) -> str:
    """Pick the body to feed downstream: prefer the cleaned HTML whenever the plain
    text is thin, has no part numbers, or the HTML carries a <table> (part numbers
    often live ONLY inside tables that the plain-text part strips)."""
    text = body_text or ""
    html = body_html or ""
    if not html:
        return text
    has_mpns = bool(MPN_PATTERN.search(text))
    has_table = "<table" in html.lower()
    if len(text.strip()) < min_len or not has_mpns or has_table:
        cleaned = html_to_clean_text(html)
        if len(cleaned) > len(text) or MPN_PATTERN.search(cleaned) or has_table:
            return cleaned
    return text


# ============================================================
# Extraction entry points
# ============================================================

def extract_from_email(subject: str, body: str, our_domains: list[str] | None = None) -> list[dict]:
    cleaned = html_to_clean_text(body) if body and "<" in body and ">" in body else body
    return brain.extract_bom(subject, cleaned, our_domains=our_domains)


def _excel_to_text(file_bytes: bytes) -> str:
    """Whole workbook → plain text tables (all sheets, all non-empty rows) so the model
    finds the real header row and odd column names itself. Handles .xlsx (openpyxl)
    and legacy .xls (xlrd)."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
        parts = []
        for name in wb.sheetnames:
            ws = wb[name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c).strip() for c in row]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"### Sheet: {name}\n" + "\n".join(rows))
        try:
            wb.close()
        except Exception:
            pass
        if parts:
            return "\n\n".join(parts)
    except Exception as e:
        log.info("openpyxl failed (%s); trying legacy .xls reader", e)
    try:
        import xlrd
    except ImportError:
        log.warning("xlrd not installed — cannot read legacy .xls")
        return ""
    book = xlrd.open_workbook(file_contents=file_bytes)
    parts = []
    for sh in book.sheets():
        rows = []
        for r in range(sh.nrows):
            cells = ["" if sh.cell_value(r, c) is None else str(sh.cell_value(r, c)).strip()
                     for c in range(sh.ncols)]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"### Sheet: {sh.name}\n" + "\n".join(rows))
    return "\n\n".join(parts)


def extract_from_attachment(file_bytes: bytes, filename: str, mime_type: str = "") -> list[dict]:
    fn = (filename or "").lower()
    try:
        if fn.endswith((".xlsx", ".xls")):
            text = _excel_to_text(file_bytes)
        elif fn.endswith(".csv"):
            text = file_bytes.decode("utf-8", errors="replace")
        elif fn.endswith(".pdf"):
            text = _pdf_to_text(file_bytes)
        else:
            log.info("unsupported attachment type: %s (%s)", filename, mime_type)
            return []
    except Exception as e:
        log.error("attachment %s parse failed: %s", filename, e)
        return []
    if not text.strip():
        return []
    return brain.extract_bom(f"BOM attachment: {filename}", text)


def _pdf_to_text(file_bytes: bytes) -> str:
    import pdfplumber
    text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
            for table in page.extract_tables():
                for row in table:
                    text += "\t".join(str(c) if c else "" for c in row) + "\n"
    return text


# ============================================================
# Merge / dedup
# ============================================================

def merge_items(collected: list[dict]) -> list[dict]:
    """Composite-key merge (mpn|description|package) so DISTINCT rows are never
    dropped: a row with no MPN, or a repeated MPN with a different package, is kept.
    Only exact duplicates (same email parsed as both plain + HTML) collapse, filling
    each other's missing fields. (The old MPN-only merge turned 27 rows into 20.)"""
    def _key(it):
        return ((it.get("mpn") or "").strip().upper(),
                (it.get("description") or "").strip().upper(),
                (it.get("package") or "").strip().upper())

    merged: dict = {}
    out: list[dict] = []
    for it in collected:
        if not isinstance(it, dict):
            continue
        k = _key(it)
        if k == ("", "", ""):
            continue
        if k not in merged:
            merged[k] = it
            out.append(it)
        else:
            ex = merged[k]
            for fld in ("manufacturer", "quantity", "target_price", "currency",
                        "required_date", "special_requirements", "annual_quantity",
                        "package", "mpn", "description"):
                if not ex.get(fld) and it.get(fld):
                    ex[fld] = it[fld]
    return out
