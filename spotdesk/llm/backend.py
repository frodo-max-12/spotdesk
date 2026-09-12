"""LLM backend — one interface, two transports.

    anthropic  → Anthropic Messages API (default). Metered, unattended-safe, the
                 production path for a 24/7 server.
    claude_cli → the local `claude` CLI in -p mode. Development convenience when a
                 CLI login is already on the machine.

Everything above this file talks to `complete(system, user, ...)` and never knows
which transport ran. Both paths share the same JSON-tolerant parsing helpers.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time

from .. import config

log = logging.getLogger("spotdesk.llm")

_anthropic_client = None


def _api_complete(system: str, user: str, *, model: str, max_tokens: int) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set (env, .env, or ~/.anthropic_api_key)")
        from anthropic import Anthropic
        _anthropic_client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


_CLAUDE_PATH = shutil.which("claude") or "claude"


def _cli_complete(system: str, user: str, *, model: str, max_tokens: int) -> str:
    prompt = f"INSTRUCTIONS (follow exactly):\n{system}\n\n---\n\nINPUT:\n{user}"
    argv = [_CLAUDE_PATH, "-p", prompt, "--model", model,
            "--output-format", "json", "--max-turns", "1"]
    if config.CLAUDE_CLI_EFFORT:
        argv += ["--effort", config.CLAUDE_CLI_EFFORT]
    result = subprocess.run(argv, capture_output=True, text=True,
                            timeout=config.LLM_TIMEOUT_SECONDS,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exit {result.returncode}: {result.stderr[:300]}")
    try:
        outer = json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()
    # Guard the error envelope: a usage-limit string must never become an email body.
    if outer.get("is_error") is True:
        raise RuntimeError(f"claude CLI error envelope: {str(outer.get('result'))[:200]}")
    return (outer.get("result") or "").strip()


def complete(system: str, user: str, *, heavy: bool = False,
             max_tokens: int = 4000, model: str | None = None) -> str:
    """Run one completion with retries. heavy=True selects the accuracy model."""
    call_model = model or (config.MODEL_HEAVY if heavy else config.MODEL_LIGHT)
    backend = config.LLM_BACKEND.lower()
    fn = _cli_complete if backend == "claude_cli" else _api_complete
    last_err: Exception | None = None
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            t0 = time.time()
            text = fn(system, user, model=call_model, max_tokens=max_tokens)
            log.debug("llm %s ok (%d chars, %.1fs)", call_model, len(text), time.time() - t0)
            if text:
                return text
            last_err = RuntimeError("empty completion")
        except Exception as e:
            last_err = e
            log.warning("llm attempt %d/%d failed on %s: %s",
                        attempt + 1, config.LLM_MAX_RETRIES + 1, call_model, e)
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"LLM failed after retries: {last_err}")


# ============================================================
# Output-parsing helpers (shared by every task in brain.py)
# ============================================================

def strip_fences(text: str) -> str:
    """Strip a leading ```html / ```json fence and trailing ``` from generated bodies —
    a fenced draft would otherwise reach a customer literally."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def parse_json(text: str):
    """Parse JSON out of a completion, tolerating prose and markdown fences around it.
    Models sometimes preface JSON with a sentence — extract it regardless, for both
    objects and arrays. Raises ValueError if nothing parseable is present."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    else:
        opens = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if opens:
            start = min(opens)
            close = "}" if text[start] == "{" else "]"
            end = text.rfind(close)
            if end > start:
                text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"unparseable JSON in completion: {text[:200]}") from e


def parse_json_safe(text: str, default):
    try:
        return parse_json(text)
    except ValueError:
        return default
