#!/usr/bin/env bash
#
# Daily run for Linux/macOS hosts, driven by cron.
#
# This is the route to use on NRLA infrastructure, and it is the RECOMMENDED
# option, for one reason that matters: run from an NRLA egress IP, gov.wales is
# reachable directly. On any cloud host it returns 403 regardless of what you
# send. Running on your own network is the only way to get the full picture
# without the shared-mailbox route.
#
# SETUP
#   1. Copy the project to /opt/senedd-monitor
#   2. python3 -m venv /opt/senedd-monitor/.venv
#      /opt/senedd-monitor/.venv/bin/pip install -r requirements.txt
#   3. cp deploy/monitor.env.example /opt/senedd-monitor/monitor.env
#      and fill it in.  chmod 600 monitor.env
#   4. chmod +x deploy/run-daily.sh
#   5. crontab -e and add the lines at the bottom of this file.
#
# It writes a log per run and keeps 90 days of them, so if the digest stops
# arriving there is something to read rather than a guess.

set -uo pipefail

ROOT="${MONITOR_ROOT:-/opt/senedd-monitor}"
PYTHON="${MONITOR_PYTHON:-$ROOT/.venv/bin/python}"
LOG_DIR="${MONITOR_LOG_DIR:-$ROOT/logs}"
# Where the directorate reads it. A mapped SharePoint library or a synced
# OneDrive folder both work; so does any file share.
PUBLISH_TO="${MONITOR_PUBLISH_TO:-$ROOT/out/index.html}"
LOOKBACK_DAYS="${MONITOR_LOOKBACK_DAYS:-21}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date -u +'%Y%m%d-%H%M').log"

log() { printf '%s  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG"; }

cd "$ROOT" || { echo "cannot cd to $ROOT"; exit 1; }

# Secrets live in a file with 600 permissions, never in the crontab and never
# in the repository.
if [[ -f "$ROOT/monitor.env" ]]; then
  set -a; . "$ROOT/monitor.env"; set +a
  log "loaded configuration from monitor.env"
else
  log "WARNING: no monitor.env found — email will stay in dry-run mode"
fi

log "=== Senedd monitor daily run starting ==="

# Tests first. A broken collector must not quietly publish a misleading page.
if ! "$PYTHON" -m tests.test_monitor >>"$LOG" 2>&1; then
  log "TESTS FAILED — aborting before touching the archive or sending anything"
  log "Read $LOG, then fix before the next run."
  exit 1
fi
log "tests passed"

# Collect. `collect` exits non-zero when a source we depend on returned nothing.
# We capture that but continue, because a partial run still has value and the
# dashboard reports the gap in red at the top of the page.
"$PYTHON" -m monitor.cli collect --days "$LOOKBACK_DAYS" >>"$LOG" 2>&1
COLLECT_STATUS=$?
if [[ $COLLECT_STATUS -ne 0 ]]; then
  log "NOTE: collect reported a source failure (exit $COLLECT_STATUS)."
  log "      The dashboard will flag this run as incomplete. Check the log."
fi

"$PYTHON" -m monitor.cli dashboard --out "$PUBLISH_TO" >>"$LOG" 2>&1 \
  && log "dashboard published to $PUBLISH_TO" \
  || log "ERROR: dashboard build failed"

# Alerts before the digest: anything critical should not wait for the round-up.
"$PYTHON" -m monitor.cli alert --send >>"$LOG" 2>&1 || log "alert step failed"
"$PYTHON" -m monitor.cli digest --days 1 --send \
  --dashboard-url "${MONITOR_DASHBOARD_URL:-}" >>"$LOG" 2>&1 \
  || log "digest step failed"

"$PYTHON" -m monitor.cli stats >>"$LOG" 2>&1

# Back up the archive. It is a single file, so this is cheap and it is the whole
# disaster-recovery story.
BACKUP_DIR="${MONITOR_BACKUP_DIR:-$ROOT/backups}"
mkdir -p "$BACKUP_DIR"
cp data/monitor.sqlite3 "$BACKUP_DIR/monitor-$(date -u +'%Y%m%d').sqlite3" \
  && log "archive backed up"
find "$BACKUP_DIR" -name 'monitor-*.sqlite3' -mtime +30 -delete 2>/dev/null
find "$LOG_DIR"    -name 'run-*.log'         -mtime +90 -delete 2>/dev/null

log "=== finished ==="
exit $COLLECT_STATUS

# ---------------------------------------------------------------------------
# CRONTAB
# ---------------------------------------------------------------------------
# Times are the server's local time. Plenary sits Tuesday and Wednesday;
# committees mostly Wednesday and Thursday. The Senedd returns from summer
# recess on 14 September 2026.
#
#   # Weekdays: four times a day during sitting weeks
#   30 7,12,17,20 * * 1-5  /opt/senedd-monitor/deploy/run-daily.sh
#
#   # Saturday: one catch-up run for anything published late on Friday
#   30 8 * * 6             /opt/senedd-monitor/deploy/run-daily.sh
#
#   # Sunday: a wider sweep, to catch anything a 21-day window missed
#   0 2 * * 0  MONITOR_LOOKBACK_DAYS=45 /opt/senedd-monitor/deploy/run-daily.sh
#
# If you want a single line and nothing else, this is the one that matters:
#
#   30 7 * * 1-5  /opt/senedd-monitor/deploy/run-daily.sh
#
# ---------------------------------------------------------------------------
# MONITORING THE MONITOR
# ---------------------------------------------------------------------------
# The failure that destroys trust is a quiet week that was actually a broken
# feed. This script exits non-zero when a source we depend on returns nothing,
# so point something at that. Simplest option, using a free dead-man's-switch
# service — add to the end of the script:
#
#   curl -fsS --retry 3 "https://hc-ping.com/<your-uuid>/$COLLECT_STATUS"
#
# If the run stops happening at all, you get told. Without something like this,
# a silent scheduler failure looks exactly like a quiet fortnight in the Senedd.
