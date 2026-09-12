# Deploying SpotDesk 24/7

The agent is one Python process (poller + monitors + dashboard). It runs anywhere
Python 3.11+ runs; the two production shapes are a small Linux VPS (systemd, always
on) or a Mac (launchd/cron).

## 0. What you need

| Thing | Where |
|---|---|
| Google OAuth client (Desktop app) | Google Cloud Console → Gmail API enabled → `config/credentials.json` |
| Gmail token for the agent mailbox | `python main.py --setup` on a machine with a browser → `config/token.json` |
| Anthropic API key | console.anthropic.com → `.env` `ANTHROPIC_API_KEY` |
| Mouser Search API key (free) | mouser.com/api-hub → `.env` `MOUSER_API_KEY` |
| Vendor panel CSV | export from your contact sheet → `spotdesk.cli import-vendors` |

The LLM backend defaults to the **Anthropic Messages API** — metered, unattended-safe,
the right thing for a 24/7 server. (`LLM_BACKEND=claude_cli` exists for local
development on a machine that already has a Claude CLI login; use the API for
anything unattended.)

## 1. VPS (recommended for production)

**Host: Oracle Cloud Always Free tier.** An Always Free Ampere A1 instance (up to
4 OCPU / 24 GB across instances — even 1 OCPU / 6 GB is plenty; the agent is one
small Python process) with Ubuntu 24.04 runs SpotDesk at zero recurring cost.
Create the instance, add your SSH key, then:

```bash
# as the service user, in the repo root
bash deploy/setup_vps.sh
# fill .env, copy credentials.json + token.json (see script output)
sudo cp deploy/spotdesk.service /etc/systemd/system/spotdesk.service
# edit the unit's user/paths, then:
sudo systemctl daemon-reload && sudo systemctl enable --now spotdesk
journalctl -u spotdesk -f
```

Reach the dashboard from your laptop without exposing it:

```bash
ssh -L 8080:127.0.0.1:8080 <user>@<vps-ip>   # then open http://localhost:8080
```

Keep `DASHBOARD_HOST=127.0.0.1`. The app refuses a network bind without
`DASHBOARD_USER`/`DASHBOARD_PASSWORD` — it can send reviewed drafts.

## 2. Mac (cron-style)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python main.py --setup            # one-time OAuth
./.venv/bin/python main.py                    # foreground: agent + dashboard
# or one cycle per cron tick:
(crontab -l; echo "*/5 * * * * cd $HOME/spotdesk && ./.venv/bin/python main.py --once >> data/logs/cron.log 2>&1") | crontab -
```

## 3. Go-live sequence (staged, safe)

1. **`AGENT_MODE=testing`** (the default). Everything the agent wants to send appears
   as Gmail **drafts** — nothing external goes out on its own. Live with this for a
   few days: read the drafts, correct classifications
   (`python -m spotdesk.cli correct <email_id> <type> "<why>"`), tune margins.
2. **Set `BACKFILL_FLOOR`** (YYYY/MM/DD) BEFORE the first run against a mailbox with
   history, or the startup sweep will process the entire inbox.
3. Import the vendor panel; sanity-check routing with
   `python -m spotdesk.cli lookup <MPN>` and a test RFQ.
4. Populate `data/compliance/bis_denied.txt` and `ofac_sdn.txt` (one entry per line,
   `Name|Address|Country`) — the gate warns loudly while they're empty.
5. **`AGENT_MODE=automatic`** when the drafts have been consistently right. Sends are
   still human-clicked (all policies default `draft`); automatic mode only arms the
   `POLICY_*=auto` dials you explicitly flip, ack first, quotes later, POs never.

## 4. Operational must-knows

- **One agent per mailbox.** Two processes on the same inbox will each handle the
  same unread mail and double-draft. The systemd unit enforces a single instance.
- **Token care:** `config/token.json` refreshes itself headlessly; if it is ever
  revoked, the log says so and you re-run `--setup` and copy it across.
- **Backups:** `data/desk.db` is the desk's memory — snapshot it daily
  (`sqlite3 data/desk.db ".backup data/backups/desk-$(date +%F).db"` in cron).
- **Weekly distill:** `python -m spotdesk.cli distill` (or cron it) drops the week's
  KPIs + surprises into Drafts for the oversight inbox.

## 5. Production notes

Set the production mailbox, oversight CC list and test address in `.env` (see
`.env.example`). Proof/test sends go to `TEST_EMAIL`; every real outbound is CC'd
to `OVERSIGHT_CC`. The operators work out of the Gmail Drafts folder + the
dashboard at `http://localhost:8080`; the only daily decisions are Send /
edit-then-Send / discard, plus `desk close` when a deal resolves.
