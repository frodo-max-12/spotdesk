#!/usr/bin/env bash
# Provision a fresh Ubuntu VPS for SpotDesk. Run from the repo root as the service user.
set -euo pipefail

echo "== SpotDesk VPS setup =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

mkdir -p config data/logs data/compliance
[ -f .env ] || cp .env.example .env

echo
echo "Done. Next steps:"
echo "  1. Fill .env (mailbox, ANTHROPIC_API_KEY, MOUSER_API_KEY, BACKFILL_FLOOR)."
echo "  2. Copy config/credentials.json (Google OAuth client) onto this box."
echo "  3. Mint the Gmail token on a machine WITH a browser:  python main.py --setup"
echo "     then copy config/token.json here (chmod 600)."
echo "  4. Import the vendor panel:  ./.venv/bin/python -m spotdesk.cli import-vendors vendors.csv"
echo "  5. Install the service:  sudo cp deploy/spotdesk.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload && sudo systemctl enable --now spotdesk"
echo "  6. Watch it:  journalctl -u spotdesk -f"
