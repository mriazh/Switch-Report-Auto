# Switch-Report-Auto

Hands-free automation of the Aruba AirWave switch-report lifecycle for
GMF AeroAsia: trigger report definition 681 on the NMS, poll and
download the CSV export ZIP, and populate **8 Excel workbooks**
(daily `<DD>` sheets + monthly `Monthly` tab) — running headless on
Debian Linux via systemd, with optional Telegram notifications.

---

## Architecture

```
 CLI / systemd
      │
      ▼
 scheduler.py ──► airwave_client.py ──► AirWave NMS (Playwright, headless)
      │              (login / trigger 681 / poll / download)
      ▼
 archive_extractor.py   (unzip + validate 9 mandatory CSVs)
      │
      ▼
 excel_writer.py        (OpenPyXL: clone <DD>/Monthly sheet, inject 8 workbooks)
      │
      ▼
 notifier.py            (Telegram if configured, else console)
```

| Module | Responsibility |
|---|---|
| `switch_report/config.py` | Typed env-driven `Settings` (pydantic-settings, `.env` file) |
| `switch_report/airwave_client.py` | Playwright: login, trigger report 681, poll `/reports_list`, download ZIP |
| `switch_report/archive_extractor.py` | Unzip → flatten → validate the 9 required CSV categories |
| `switch_report/excel_writer.py` | OpenPyXL: sheet cloning, cell cleaning, inject 8 workbooks idempotently |
| `switch_report/notifier.py` | Telegram status messages, console fallback |
| `switch_report/scheduler.py` | CLI entry point + full pipeline orchestration |

**The 8 target workbooks** (populated under `Report/Switch/YYYY-MM/Output/`):

1. `Avg Uptime By Any Controller.xlsx`
2. `Avg Uptime By Any Switch.xlsx`
3. `Avg Uptime by Device.xlsx`
4. `Most Utilization Switch.xlsx`
5. `Most Utilization By Usage.xlsx`
6. `Top Utilization CPU dan Memory.xlsx` (with ✓ tick marks)
7. `Daily Monitor Up Time Switch All GMF CGK HO & OSA.xlsx`
8. `Device Switch Downtime Reebort Detail.xlsx`

Missing output workbooks are auto-seeded by copying the master template
from `config/templates/`. Re-running for an existing date overwrites the
target sheet cleanly (idempotent).

---

## Installation (Debian Linux, headless)

> Requires **Python 3.10+**. All operations are headless (no X11/GUI);
> Playwright runs Chromium in headless mode.

```bash
# 1. System dependencies
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip git

# 2. Get the source
sudo mkdir -p /opt/switch-report-auto
sudo git clone <repo-url> /opt/switch-report-auto
cd /opt/switch-report-auto

# 3. Create the dedicated service user (no login shell)
sudo useradd --system --create-home --shell /usr/sbin/nologin switchreport
sudo chown -R switchreport:switchreport /opt/switch-report-auto

# 4. Python virtualenv
python3 -m venv /opt/switch-report-auto/.venv
/opt/switch-report-auto/.venv/bin/pip install -e .

# 5. Install the headless Chromium + its system libs (Playwright)
/opt/switch-report-auto/.venv/bin/playwright install chromium
sudo /opt/switch-report-auto/.venv/bin/playwright install-deps chromium

# 6. Environment file (see next section)
cp config/.env.example /opt/switch-report-auto/.env
sudo nano /opt/switch-report-auto/.env     # fill in AirWave credentials
sudo chown switchreport:switchreport /opt/switch-report-auto/.env
sudo chmod 600 /opt/switch-report-auto/.env
```

---

## Environment variables

All configuration is read from `.env` in the working directory
(never hardcoded — NFR-4). Copy `config/.env.example` to `.env`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `AIRWAVE_URL` | ✅ | — | AirWave NMS base URL, e.g. `https://192.168.240.196` |
| `AIRWAVE_USERNAME` | ✅ | — | NMS login user |
| `AIRWAVE_PASSWORD` | ✅ | — | NMS login password |
| `AIRWAVE_POLL_INTERVAL_SECONDS` | | `20` | `/reports_list` poll cadence |
| `AIRWAVE_POLL_TIMEOUT_MINUTES` | | `5` | Per-attempt poll timeout |
| `AIRWAVE_MAX_RETRIES` | | `3` | Max trigger+poll retry attempts |
| `REPORT_BASE_DIR` | | `Report/Switch` | Root for `Raw/` + `Output/` trees |
| `TEMPLATES_DIR` | | `config/templates` | Master workbooks used to seed missing outputs |
| `GOWA_BASE_URL` | | *(empty)* | GOWA WhatsApp gateway base URL (optional) |
| `GOWA_TARGET_JID` | | *(empty)* | WhatsApp target JID, e.g. `628123456789@s.whatsapp.net` (optional) |
| `GOWA_DEVICE_ID` | | *(empty)* | GOWA device ID sent as `X-Device-Id` header when set (optional) |
| `GOWA_TIMEOUT_SECONDS` | | `5.0` | HTTP timeout for GOWA `/send/message` |
| `TIMEZONE` | | `Asia/Jakarta` | Reporting timezone (WIB) |

GOWA/WhatsApp is optional: leave `GOWA_BASE_URL` and `GOWA_TARGET_JID` empty for console-only notifications.

---

## systemd setup

Ship four units in `deploy/systemd/`:

```
switch-report-daily.service    daily pipeline, --mode daily
switch-report-daily.timer      06:00 WIB every day
switch-report-monthly.service  monthly pipeline, --mode monthly
switch-report-monthly.timer    07:00 WIB on the 1st
```

> **Timezone matters.** systemd timer expressions are evaluated in the
> OS local time. Set the host clock to WIB first:
> ```bash
> sudo timedatectl set-timezone Asia/Jakarta
> ```

Install and enable:

```bash
sudo cp deploy/systemd/switch-report-daily.service \
         deploy/systemd/switch-report-daily.timer \
         deploy/systemd/switch-report-monthly.service \
         deploy/systemd/switch-report-monthly.timer \
         /etc/systemd/system/

sudo systemctl daemon-reload

# Enable + start both timers
sudo systemctl enable --now switch-report-daily.timer
sudo systemctl enable --now switch-report-monthly.timer

# Inspect
systemctl list-timers | grep switch-report
journalctl -u switch-report-daily.service -f
```

The service units run as the `switchreport` user with
`WorkingDirectory=/opt/switch-report-auto`, so the `.env` file is picked
up automatically. Adjust `User=`/`WorkingDirectory` in the units if your
layout differs.

### Backfill (manual run for a specific date)

```bash
# Daily backfill for 2026-09-30 (report day = that date)
sudo -u switchreport /opt/switch-report-auto/.venv/bin/switch-report \
    --mode daily --date 2026-09-30

# Monthly backfill (report month = month before the reference day)
sudo -u switchreport /opt/switch-report-auto/.venv/bin/switch-report \
    --mode monthly --date 2026-10-01
```

### Backfill (many days in one run)

`scripts/backfill.py` runs a batch of days sequentially, one report-definition
run each, with a pause between days. Unlike `--date`, here `Raw/<DD>.zip` and
sheet `<DD>` always hold the data **of** day `<DD>`.

```bash
# 2026-10-01 .. 2026-10-08 plus 2026-10-10, 10s between days (default)
sudo -u switchreport /opt/switch-report-auto/.venv/bin/python \
    scripts/backfill.py --start 2026-10-01 --end 2026-10-08 --dates 2026-10-10

# Log the plan without touching AirWave
python scripts/backfill.py --start 2026-10-01 --end 2026-10-08 --dry-run

# Change the pause (seconds)
python scripts/backfill.py --dates 2026-10-10 --sleep 30
```

One browser and one login serve the whole batch; a failing day is logged and
the batch continues. Exit code `0` only when every day succeeded. No WhatsApp
notifications are sent — the console log is the report.

---

## Manual CLI execution

```bash
# Daily: pull D-1 (yesterday in WIB), download + populate
switch-report --mode daily

# Monthly: pull M-1 (previous month), download + populate
switch-report --mode monthly

# Skip the AirWave download and reuse an existing ZIP in Raw/
switch-report --mode daily --date 2026-10-08 --skip-download

# All options
switch-report --mode daily --date 2026-10-08 [--skip-download]
```

Exit code: `0` on success, `1` on any failure (bad args, missing `.env`,
download/extraction/injection error). The Telegram/console notification
fires on both success and failure.

**Date semantics (Asia/Jakarta):**
- **daily** — report day is `--date` if given, else *yesterday* WIB.
  ZIP lands at `Report/Switch/YYYY-MM/Raw/<DD>.zip`, extracted to
  `Raw/<DD>/`, sheet `<DD>`.
- **monthly** — report month is the month *before* the run/backfill day.
  ZIP at `Report/Switch/YYYY-MM/Raw/Monthly.zip`, extracted to
  `Raw/Monthly/`, sheet `Monthly`.

---

## Dependency lockfiles

`uv.lock` (uv-native) and `requirements.lock` (pip/venv export) pin every
dependency for reproducible installs. Regenerate both after editing
`pyproject.toml`:

```bash
uv lock
uv export --format requirements-txt --all-extras --no-emit-project --no-hashes \
    -o requirements.lock
```

Install from the lock instead of resolving fresh:

```bash
uv sync --locked --extra dev          # creates .venv from uv.lock
# or, plain venv + pip:
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock && pip install -e .
```

CI (`.github/workflows/ci.yml`, aliased by `.github/workflows/cli.yml`) runs on
every push/PR to `master`/`main` and does exactly `uv sync --locked --extra dev`
followed by `uv run pytest tests/ -v`.

---

## Running the tests

```bash
cd /opt/switch-report-auto
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Full suite (no network, no browser — Playwright is mocked)
pytest

# Single module
pytest tests/test_excel_writer.py -v
```

All suites are unit-level and run headless: Playwright's `Page` is a
mock (`FakePage`), Telegram's `requests.post` is patched, and the
AirWave download step is monkeypatched. No live NMS or real Chromium is
required for tests.

---

## Data layout

```
Report/Switch/
  2026-10/
    Raw/
      08.zip            daily ZIP
      08/               extracted 9 CSVs
      Monthly.zip       monthly ZIP
      Monthly/
    Output/             8 workbooks (auto-seeded from templates if missing)
      Avg Uptime By Any Controller.xlsx
      ... (8 total)
.auth/
  airwave_session.json  Playwright storage state (session reuse)
config/
  templates/            8 master template workbooks
  .env.example
deploy/
  systemd/              4 units (2 service + 2 timer)
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `configuration error: ... AIRWAVE_URL` | `.env` missing or unreadable by `switchreport` |
| Poll times out repeatedly | AirWave report queue slow; raise `AIRWAVE_MAX_RETRIES` or `AIRWAVE_POLL_TIMEOUT_MINUTES` |
| `Required CSV(s) missing from archive` | AirWave exported a partial report; re-run without `--skip-download` |
| Sheet not found after injection | Confirm `config/templates/` has the 8 master workbooks |
| Timer didn't fire | `systemctl list-timers`; verify host TZ is `Asia/Jakarta` (`timedatectl`) |
| Playwright launch fails on Debian | `playwright install-deps chromium` not run; needs system libs |
| No WhatsApp message | `GOWA_BASE_URL`/`GOWA_TARGET_JID` unset, or GOWA HTTP error → console fallback only |
