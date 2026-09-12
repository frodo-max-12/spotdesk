"""Healthcheck — is the agent actually alive and able to work?"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import config, db


def check() -> dict:
    out: dict = {"ts": datetime.now(timezone.utc).isoformat()}

    out["db"] = {"ok": config.DB_PATH.exists(),
                 "path": str(config.DB_PATH),
                 "size_mb": round(config.DB_PATH.stat().st_size / 1e6, 1)
                 if config.DB_PATH.exists() else 0}

    tok = Path(config.GOOGLE_TOKEN_FILE)
    out["gmail_token"] = {"ok": tok.exists(), "path": str(tok)}

    out["llm"] = {"backend": config.LLM_BACKEND,
                  "ok": bool(config.ANTHROPIC_API_KEY) if config.LLM_BACKEND == "anthropic" else True}

    last = db.query_one("SELECT last_run_at FROM ingest_state WHERE source = 'agent_loop'")
    out["last_loop"] = (last or {}).get("last_run_at")

    pending = db.query_one(
        "SELECT COUNT(*) AS n FROM outbound_actions WHERE status = 'drafted'")
    out["drafts_pending"] = (pending or {}).get("n", 0)

    out["mode"] = config.AGENT_MODE
    return out


def heartbeat() -> None:
    db.execute(
        "INSERT INTO ingest_state (source, last_run_at) VALUES ('agent_loop', ?) "
        "ON CONFLICT(source) DO UPDATE SET last_run_at = excluded.last_run_at",
        (db.utcnow(),))
