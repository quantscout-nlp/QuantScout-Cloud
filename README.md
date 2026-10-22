# Recovery Desk (QuantScout Cloud space, standalone)

An automated Capitulation-Recovery / Momentum watchlist built on the locked Rule 1/2/3 + Anti-Knife rulebook (v1.6.1). Screening and status updates only: **orders are OFF and there is no order code here.** Not trade advice. A human submits every order.

This branch, `recovery-desk`, is the desk's home. It shares no code with the QuantScout engine on `main`. Its only footprint on `main` is one scheduler file, because GitHub runs schedules only from the default branch.

| Path | What it is |
|---|---|
| `recovery/rules.py` | The locked rules as code: Rule 1 cap, Rule 2 momentum, battery, Rule 3 exits, tiers, RVOL20, AK-D / AK-S / AK Combined |
| `recovery/providers.py` | Alpaca SIP, then Webull (real time), then Massive / Tiingo for daily bars. Rate-limited; every row is labelled with its source |
| `recovery/desk.py` | One cycle: daily qualification, live-session overlay, stale-feed guard, signed snapshot, change history |
| `recovery/store.py` | The desk's private Google Drive folder (`drive.file` scope) |
| `recovery/held.py` | Robinhood positions upload parser and the held-name stop block (EXIT_FLAG / STOP_NOT_COMPUTED) |
| `recovery_app.py` | The dashboard. Its own Streamlit app, shows signed snapshots only. Its one write is the Robinhood positions upload |
| `scripts/run_recovery_desk.py` | `--loop` = always-on worker (primary); no flag = one cycle (GitHub failover) |
| `scripts/google_drive_token.py` | One-time Google setup: creates the folder, uploads the universe, prints the secrets |
| `deploy/`, `Dockerfile` | Hardened always-on worker (systemd sandbox or a read-only container) |
| `tests/` | Unit tests: rule examples from your files, Anti-Knife reset, stale feed, Webull signature, tamper checks |

- **Design, data feeds, cadence and setup:** [`docs/RECOVERY_DESK_CLOUD.md`](docs/RECOVERY_DESK_CLOUD.md)
- **Security model and checklist:** [`SECURITY.md`](SECURITY.md)

Run locally:

```
pip install -r requirements.txt
python -m unittest discover -s tests
python scripts/run_recovery_desk.py --out recovery_out     # one cycle into a local folder
RECOVERY_LOCAL_DIR=recovery_out streamlit run recovery_app.py
```
