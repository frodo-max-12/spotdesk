"""Central configuration. Single source of truth, driven by environment / .env.

No pydantic dependency — a small hand-rolled loader keeps the core zero-surprise.
Everything the operators tune lives here; module code never reads os.environ directly.
"""

from __future__ import annotations

import os
from pathlib import Path

# ============================================================
# .env loader (tiny, stdlib). Values already in the environment win.
# ============================================================
REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = REPO_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file(_ENV_FILE)


def _s(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(float(os.environ.get(key, "").strip() or default))
    except (ValueError, TypeError):
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _b(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# ============================================================
# Paths
# ============================================================
DATA_DIR = Path(_s("SPOTDESK_DATA_DIR", str(REPO_ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "desk.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
DEALS_DIR = Path(_s("SPOTDESK_DEALS_DIR", str(REPO_ROOT / "deals")))
PLAYBOOKS_DIR = Path(__file__).resolve().parent / "knowledge" / "playbooks"

# ============================================================
# Identity — who the agent writes as
# ============================================================
COMPANY_NAME = _s("COMPANY_NAME", "Your Company")
COMPANY_ENTITY = _s("COMPANY_ENTITY", "Your Company Pte Ltd")
COMPANY_TAGLINE = _s("COMPANY_TAGLINE",
                     "Singapore + India | 25+ years in business | ISO 9001:2015 | "
                     "50+ franchise lines + active spot desk")
COMPANY_WEBSITE = _s("COMPANY_WEBSITE", "www.example.com")
QUOTE_PREFIX = _s("QUOTE_PREFIX", "YC")            # quote numbers: YC-Q-YYYYMMDD-XXXX
SIGNOFF_NAME = _s("SIGNOFF_NAME", "International Sales")

# The mailbox the agent runs on (single shared inbox) — set in .env
AGENT_MAILBOX = _s("AGENT_MAILBOX", "sales@example.com")
INTERNAL_DOMAINS = [d.lower() for d in _list("INTERNAL_DOMAINS", "example.com")]
# Oversight addresses CC'd on every REAL outbound (visibility for the ops team)
OVERSIGHT_CC = _list("OVERSIGHT_CC", "")
# Where test sends go (test-first rule: prove the render before any bulk/real send)
TEST_EMAIL = _s("TEST_EMAIL", "you@example.com")

# ============================================================
# Gmail OAuth
# ============================================================
GOOGLE_CREDENTIALS_FILE = _s("GOOGLE_CREDENTIALS_FILE", str(REPO_ROOT / "config" / "credentials.json"))
GOOGLE_TOKEN_FILE = _s("GOOGLE_TOKEN_FILE", str(REPO_ROOT / "config" / "token.json"))
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",   # drafts-first HITL needs compose
]

# ============================================================
# LLM backend
#   anthropic  → Anthropic Messages API (default; production / unattended-safe)
#   claude_cli → local `claude` CLI (development convenience)
# ============================================================
LLM_BACKEND = _s("LLM_BACKEND", "anthropic")
ANTHROPIC_API_KEY = _s("ANTHROPIC_API_KEY") or (
    (Path.home() / ".anthropic_api_key").read_text().strip()
    if (Path.home() / ".anthropic_api_key").exists() else ""
)
MODEL_LIGHT = _s("MODEL_LIGHT", "claude-haiku-4-5-20251001")    # classify, small extractions
MODEL_HEAVY = _s("MODEL_HEAVY", "claude-sonnet-5")              # BOM / pricing / planning / drafting
LLM_TIMEOUT_SECONDS = _i("LLM_TIMEOUT_SECONDS", 600)
LLM_MAX_RETRIES = _i("LLM_MAX_RETRIES", 2)
CLAUDE_CLI_EFFORT = _s("CLAUDE_CLI_EFFORT", "high")             # cli backend only

# ============================================================
# Agent behaviour
# ============================================================
# testing   → NOTHING external is sent automatically; drafts are still created (drafts are safe).
#             Any configured auto-send is suppressed, vendor mail redirects to TEST_EMAIL or is skipped.
# automatic → auto-send policies below take effect for real.
AGENT_MODE = _s("AGENT_MODE", "testing")
IS_AUTOMATIC = AGENT_MODE.lower() == "automatic"

# Drafts-first HITL: per-action send policy. "draft" = create a Gmail draft, human sends.
# "auto" = send directly when gates pass (only honoured in AGENT_MODE=automatic).
# The shipped default is draft-everything: the human is the send button.
SEND_POLICY = {
    "ack":              _s("POLICY_ACK", "draft"),
    "vendor_rfq":       _s("POLICY_VENDOR_RFQ", "draft"),
    "quote":            _s("POLICY_QUOTE", "draft"),
    "negotiation_reply": _s("POLICY_NEGOTIATION", "draft"),
    "po_ack":           _s("POLICY_PO_ACK", "draft"),
    "vendor_po":        _s("POLICY_VENDOR_PO", "draft"),   # a PO is money — keep as draft
    "followup":         _s("POLICY_FOLLOWUP", "draft"),
    "logistics_update": _s("POLICY_LOGISTICS", "draft"),
}

POLL_INTERVAL_SECONDS = _i("POLL_INTERVAL_SECONDS", 60)
MONITOR_INTERVAL_SECONDS = _i("MONITOR_INTERVAL_SECONDS", 120)
BACKFILL_FLOOR = _s("BACKFILL_FLOOR", "")           # YYYY/MM/DD — never sweep mail older than this
VENDOR_REPLY_WAIT_HOURS = _i("VENDOR_REPLY_WAIT_HOURS", 24)
FOLLOWUP_DAY3_ENABLED = _b("FOLLOWUP_DAY3_ENABLED", True)
FOLLOWUP_DAY7_ENABLED = _b("FOLLOWUP_DAY7_ENABLED", True)
QUOTE_VALIDITY_DAYS = _i("QUOTE_VALIDITY_DAYS", 30)
DEAL_NO_RESPONSE_DAYS = _i("DEAL_NO_RESPONSE_DAYS", 21)  # sent quote silent this long → censored no_response

# ============================================================
# Sourcing / routing
# ============================================================
VENDOR_MAX_PER_PART = _i("VENDOR_MAX_PER_PART", 8)   # cap brokers RFQ'd per part (0 = no cap)
RFQ_SEND_SPACING_SECONDS = _f("RFQ_SEND_SPACING_SECONDS", 2.0)  # throttle on batch release
CONSOLIDATION_LLM_PLANNING = _b("CONSOLIDATION_LLM_PLANNING", True)

# ============================================================
# Pricing — margin engine + margin guard
# ============================================================
DEFAULT_MARGIN_PERCENT = _f("DEFAULT_MARGIN_PERCENT", 15.0)
MARGIN_OVERRIDES_RAW = _s("MARGIN_OVERRIDES", "")    # "MCU:20,Passives:12,STMicroelectronics:18"
DEFAULT_EXPENSES_PERCENT = _f("DEFAULT_EXPENSES_PERCENT", 3.0)   # freight/handling over cost
FINANCE_BUFFER_PCT = _f("FINANCE_BUFFER_PCT", 1.5)   # working-capital cost in the landed floor
SAFETY_FUDGE_PCT = _f("SAFETY_FUDGE_PCT", 2.0)       # never-below-this cushion in the landed floor
MIN_MARGIN_PERCENT = _f("MIN_MARGIN_PERCENT", 8.0)   # gate: hold quotes with any line under this
NEGOTIATION_MIN_MARGIN_PERCENT = _f("NEGOTIATION_MIN_MARGIN_PERCENT", 10.0)
MAX_VENDOR_ASK_PERCENT = _f("MAX_VENDOR_ASK_PERCENT", 20.0)  # cap on one round's "go lower" ask
AUTO_SEND_MAX_VALUE = _f("AUTO_SEND_MAX_VALUE", 0.0)  # 0 = no ceiling
TRANSIT_WEEKS_FALLBACK = _f("TRANSIT_WEEKS_FALLBACK", 2.0)   # inbound transit when no estimate exists


def margin_overrides() -> dict[str, float]:
    out: dict[str, float] = {}
    for pair in MARGIN_OVERRIDES_RAW.split(","):
        if ":" in pair:
            k, _, v = pair.partition(":")
            try:
                out[k.strip().lower()] = float(v.strip())
            except ValueError:
                continue
    return out


# ============================================================
# FX (used only to normalize a non-USD vendor cost to USD)
# ============================================================
FX_AUTO_FETCH = _b("FX_AUTO_FETCH", True)
FX_BUFFER_PERCENT = _f("FX_BUFFER_PERCENT", 2.0)
FX_REFRESH_HOURS = _i("FX_REFRESH_HOURS", 20)
USD_INR_FALLBACK = _f("USD_INR_FALLBACK", 83.0)

# ============================================================
# Market lookup channels
# ============================================================
PART_LOOKUP_ENABLED = _b("PART_LOOKUP_ENABLED", True)
MOUSER_API_KEY = _s("MOUSER_API_KEY") or (
    (Path.home() / ".mouser_api_key").read_text().strip()
    if (Path.home() / ".mouser_api_key").exists() else ""
)
DIGIKEY_CREDS_PATH = Path(_s("DIGIKEY_CREDS_PATH", str(Path.home() / ".digikey_creds.json")))
OEMSTRADE_ENABLED = _b("OEMSTRADE_ENABLED", True)
LOOKUP_TIMEOUT_SECONDS = _i("LOOKUP_TIMEOUT_SECONDS", 20)

# ============================================================
# Trust / learning
# ============================================================
DEFAULT_TRUST_SCORE = _i("DEFAULT_TRUST_SCORE", 50)
TRUST_BOOST_PER_WON = _i("TRUST_BOOST_PER_WON", 5)
TRUST_PENALTY_PER_LOST = _i("TRUST_PENALTY_PER_LOST", 15)
TRUST_PENALTY_PER_BOUNCE = _i("TRUST_PENALTY_PER_BOUNCE", 2)

# ============================================================
# Circuit breakers
# ============================================================
MAX_OUTBOUND_PER_DAY = _i("MAX_OUTBOUND_PER_DAY", 300)          # all agent-sent mail
MAX_COLD_SENDS_PER_DAY = _i("MAX_COLD_SENDS_PER_DAY", 300)      # ramped separately below
BOUNCE_RATE_HALT_PCT = _f("BOUNCE_RATE_HALT_PCT", 15.0)
ANOMALY_PRICE_MULTIPLIER = _f("ANOMALY_PRICE_MULTIPLIER", 10.0)
MAX_CAPITAL_PER_SKU_USD = _f("MAX_CAPITAL_PER_SKU_USD", 500_000)
MAX_CAPITAL_PER_COUNTERPARTY_USD = _f("MAX_CAPITAL_PER_COUNTERPARTY_USD", 1_000_000)
AUTHENTICITY_MEDIAN_RATIO = _f("AUTHENTICITY_MEDIAN_RATIO", 0.6)  # cheapest < 60% of panel median → verify CoC

# ============================================================
# Cold outreach (growth engine)
# ============================================================
COLD_PACING_MIN_S = _f("COLD_PACING_MIN_S", 15.0)   # random jitter window between cold sends
COLD_PACING_MAX_S = _f("COLD_PACING_MAX_S", 30.0)
FOLLOWUP_PACING_S = _f("FOLLOWUP_PACING_S", 1.0)    # threaded replies are high-trust; 1s is safe
SKUS_PER_COMPANY = _i("SKUS_PER_COMPANY", 5)        # anti-forwarding subset size
# Volume ramp: total historical cold sends → per-day cap
COLD_RAMP = [(1000, 300), (5000, 500), (10 ** 9, 1500)]
DOMAIN_BLOCKLIST = set(_list("DOMAIN_BLOCKLIST",
                             "inventec.com,inventec.com.tw,compal.com,"
                             "rockwellautomation.com,ra.rockwell.com,deltaww.com,delta-corp.com"))
DOMAIN_AUTOBLOCK_BOUNCES = _i("DOMAIN_AUTOBLOCK_BOUNCES", 5)

# ============================================================
# Compliance
# ============================================================
BIS_DENIED_PARTIES_FILE = DATA_DIR / "compliance" / "bis_denied.txt"
OFAC_SDN_FILE = DATA_DIR / "compliance" / "ofac_sdn.txt"
EUD_EXTRA_KEYWORDS = _s("EUD_EXTRA_KEYWORDS", "")

# ============================================================
# Dashboard (minimal by design — Gmail is the real workspace)
# ============================================================
DASHBOARD_HOST = _s("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _i("DASHBOARD_PORT", 8080)
DASHBOARD_USER = _s("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = _s("DASHBOARD_PASSWORD", "")

# ============================================================
# Logging
# ============================================================
LOG_LEVEL = _s("LOG_LEVEL", "INFO")
LOG_FILE = LOG_DIR / "spotdesk.log"
