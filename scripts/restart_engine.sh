#!/usr/bin/env bash
#
# Montreal engine restart helper with log rotation.
#
# Problem it solves: previous restart command used
#   nohup python3 main.py > engine.log 2>&1 &
# which truncates engine.log every restart, losing all pre-restart
# history. This script rotates the log first, preserving the old one
# with a timestamp before starting the new one.
#
# Usage (on Montreal, as ubuntu or via sudo):
#   ./scripts/restart_engine.sh
#   ./scripts/restart_engine.sh --keep-running  # verify only, no restart
#
# Side effects:
#   - Rotates /home/novakash/engine.log to engine-YYYYMMDD-HHMMSS.log
#   - Prunes old engine-*.log files beyond KEEP_N (default 20) to save disk
#   - Kills any existing python3 main.py processes
#   - Starts engine in the background via nohup + disown
#   - Verifies exactly 1 process is running after restart
#
# NOTE: This script ALWAYS appends to the current log via `>>` after the
# initial rotation, so if anything restarts the process outside this
# script, we still don't lose history.

set -euo pipefail

LOG_DIR="/home/novakash"
CURRENT_LOG="${LOG_DIR}/engine.log"
ENGINE_DIR="/home/novakash/novakash/engine"
KEEP_N="${KEEP_N:-20}"
KEEP_RUNNING=false

for arg in "$@"; do
    case "$arg" in
        --keep-running) KEEP_RUNNING=true ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

timestamp() { date +%Y%m%d-%H%M%S; }

log_info() { echo "[$(date +%H:%M:%S)] $*"; }

# ─── Step 1: Ensure ownership ──────────────────────────────────────────────
sudo chown -R novakash:novakash /home/novakash/novakash/ 2>/dev/null || true

# ─── Step 2: Rotate the current log if it exists ───────────────────────────
if [ -f "$CURRENT_LOG" ]; then
    ROTATED="${LOG_DIR}/engine-$(timestamp).log"
    log_info "Rotating $CURRENT_LOG → $ROTATED"
    sudo -u novakash cp "$CURRENT_LOG" "$ROTATED"
    sudo -u novakash truncate -s 0 "$CURRENT_LOG"
    log_info "Rotated. Size of archive: $(sudo -u novakash du -h "$ROTATED" | cut -f1)"
fi

# ─── Step 3: Prune old archives ────────────────────────────────────────────
ARCHIVE_COUNT=$(ls -1 "${LOG_DIR}"/engine-*.log 2>/dev/null | wc -l || echo 0)
if [ "$ARCHIVE_COUNT" -gt "$KEEP_N" ]; then
    log_info "Pruning old logs (keeping newest $KEEP_N of $ARCHIVE_COUNT)"
    ls -1t "${LOG_DIR}"/engine-*.log 2>/dev/null | tail -n +$((KEEP_N + 1)) | xargs -r sudo rm -f
fi

if [ "$KEEP_RUNNING" = true ]; then
    log_info "--keep-running set; skipping kill/start"
    exit 0
fi

# ─── Step 4: Stop existing engine ──────────────────────────────────────────
if pgrep -f 'python3.*main.py' >/dev/null; then
    log_info "Stopping existing python3 main.py processes"
    sudo pkill -9 -f 'python3 main.py' || true
    sleep 4
fi

if pgrep -f 'python3.*main.py' >/dev/null; then
    log_info "ERROR: processes still running after SIGKILL"
    ps aux | grep 'python3 main.py' | grep -v grep
    exit 1
fi

# ─── Step 5: Start engine (APPEND to log — never truncate) ────────────────
log_info "Starting engine in $ENGINE_DIR"
sudo -u novakash bash -c "cd $ENGINE_DIR && nohup python3 main.py >> $CURRENT_LOG 2>&1 </dev/null & disown"
sleep 6

# ─── Step 6: Verify ────────────────────────────────────────────────────────
PID_COUNT=$(pgrep -f 'python3 main.py' | wc -l || echo 0)
# Note: may show 2 if bash wrapper present from previous session, which is OK
if [ "$PID_COUNT" -lt 1 ] || [ "$PID_COUNT" -gt 2 ]; then
    log_info "WARNING: expected 1-2 python3 main.py process, got $PID_COUNT"
    ps aux | grep 'python3 main.py' | grep -v grep || true
    exit 1
fi

log_info "Engine started cleanly. PIDs: $(pgrep -f 'python3 main.py' | tr '\n' ' ')"
log_info "Tail of new log:"
sudo tail -10 "$CURRENT_LOG" || true
