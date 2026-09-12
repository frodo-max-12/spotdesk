"""Test fixtures — isolated data dir + schema per test session, no network."""

import os
import sys
import tempfile
from pathlib import Path

# Point the app at a throwaway data dir BEFORE any spotdesk import reads config.
_TMP = tempfile.mkdtemp(prefix="spotdesk-test-")
os.environ["SPOTDESK_DATA_DIR"] = _TMP
os.environ["AGENT_MODE"] = "testing"
os.environ["PART_LOOKUP_ENABLED"] = "false"
os.environ["OEMSTRADE_ENABLED"] = "false"
os.environ["FX_AUTO_FETCH"] = "false"
os.environ["CONSOLIDATION_LLM_PLANNING"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from spotdesk import db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    db.init_schema()
    yield


@pytest.fixture()
def clean_db():
    """Wipe mutable tables between tests that write."""
    # Children before parents — foreign keys are ON.
    with db.connect() as conn:
        for t in ("negotiation_rounds", "shipments", "vendor_quotes", "vendor_pos",
                  "vendor_rfqs", "outbound_actions", "deals", "emails",
                  "price_history", "market_offers", "outreach_sends",
                  "outreach_campaigns", "suppression_list", "counterparties",
                  "audit_log", "compliance_flags"):
            conn.execute(f"DELETE FROM {t}")
    yield
