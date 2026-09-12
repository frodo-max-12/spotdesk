"""SpotDesk entry point.

    python main.py --setup       one-time Gmail OAuth (opens a browser)
    python main.py               agent + dashboard together (production default)
    python main.py --agent       agent only
    python main.py --dashboard   dashboard only
    python main.py --once        one full cycle, then exit (cron-style)
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spotdesk import config, db                                # noqa: E402


def setup_logging():
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s",
                            datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = RotatingFileHandler(config.LOG_FILE, maxBytes=10_000_000, backupCount=5)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s"))
    root.addHandler(fh)


def run_setup():
    from spotdesk.gmail.client import GmailClient
    print("=" * 60)
    print("SpotDesk — first-time setup")
    print(f"Authenticate the agent mailbox ({config.AGENT_MAILBOX}).")
    print("A browser will open; sign in with THAT account.")
    print("=" * 60)
    input("Press Enter to continue…")
    client = GmailClient()
    client.authenticate(interactive=True)
    db.init_schema()
    print(f"\nAuthenticated as {client.user_email}. Setup complete — run: python main.py")


def main():
    ap = argparse.ArgumentParser(description="SpotDesk")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--dashboard", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    setup_logging()
    if args.setup:
        run_setup()
        return

    db.init_schema()
    from spotdesk.pipeline.agent import DeskAgent

    if args.once:
        agent = DeskAgent()
        import json
        print(json.dumps(agent.run_once(), indent=2, default=str))
        return

    if args.dashboard:
        from spotdesk.dashboard import app as dash
        dash.validate_security()
        try:  # attach Gmail if a token exists so the Release button works
            from spotdesk.gmail.client import GmailClient
            client = GmailClient()
            client.authenticate()
            dash.set_gmail(client)
        except Exception as e:
            logging.getLogger("spotdesk").warning(
                "dashboard running without Gmail (release disabled): %s", e)
        import uvicorn
        uvicorn.run(dash.app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT,
                    log_level="info")
        return

    agent = DeskAgent()
    if args.agent:
        agent.run()
        return

    # Default: agent in a background thread, dashboard in the foreground.
    from spotdesk.dashboard import app as dash
    dash.validate_security()
    dash.set_gmail(agent.gmail)
    threading.Thread(target=agent.run, daemon=True).start()
    import uvicorn
    uvicorn.run(dash.app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT,
                log_level="warning")


if __name__ == "__main__":
    main()
