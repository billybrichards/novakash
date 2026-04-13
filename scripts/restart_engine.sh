#!/usr/bin/env bash
#
# Montreal engine restart helper with log rotation.
#
# Designed to work both from CI (deploy-engine.yml calls this via SSH)
# and from a manual SSH session. The script:
#   1. Rotates engine.log (copy with timestamp, truncate original)
#   2. Prunes old rotated logs (keeps KEEP_N newest, default 5)
#   3. Stops existing engine gracefully (SIGTERM -> wait -> SIGKILL)
#   4. Starts engine in background with proper fd redirection
#   5. Verifies exactly 1 process is running
#
# The key fix vs. the old inline SSH command: stdout/stderr are fully
# redirected and the process is disowned, so SSH exits cleanly without
# hanging on an inherited file descriptor.
#
# Usage (on Montreal, as novakash or via sudo -u novakash):
#   bash /home/novakash/novakash/scripts/restart_engine.sh
#   bash /home/novakash/novakash/scripts/restart_engine.sh --keep-running  # verify only

set -euo pipefail

LOG_DIR="/home/novakash"
CURRENT_LOG="${LOG_DIR}/engine.log"
ENGINE_DIR="/home/novakash/novakash/engine"
KEEP_N="${KEEP_N:-5}"
KEEP_RUNNING=false

for arg in "$@"; do
    case "$arg" in
        --keep-running) KEEP_RUNNING=true ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

timestamp() { date +%Y%m%d_%H%M%S; }
log_info() { echo "[$(date +%H:%M:%S)] $*"; }

# ─── Step 1: Rotate the current log if it exists ─────────────────────────
if [ -f "$CURRENT_LOG" ]; then
    ROTATED="${CURRENT_LOG}.$(timestamp)"
    log_info "Rotating $CURRENT_LOG -> $ROTATED"
    cp "$CURRENT_LOG" "$ROTATED"
    : > "$CURRENT_LOG"  # Truncate in place
    log_info "Rotated. Size of archive: $(du -h "$ROTATED" | cut -f1)"
fi

# ─── Step 2: Prune old archives (keep newest KEEP_N) ─────────────────────
# shellcheck disable=SC2012
ls -t ${CURRENT_LOG}.* 2>/dev/null | tail -n +$((KEEP_N + 1)) | xargs rm -f 2>/dev/null || true

if [ "$KEEP_RUNNING" = true ]; then
    log_info "--keep-running set; skipping kill/start"
    exit 0
fi

# ─── Step 3: Stop existing engine (graceful then forced) ─────────────────
if pgrep -f 'python3 main.py' > /dev/null 2>&1; then
    log_info "Sending SIGTERM to existing engine"
    pkill -TERM -f 'python3 main.py' || true
    # Wait up to 5s for graceful shutdown
    for i in 1 2 3 4 5; do
        if ! pgrep -f 'python3 main.py' > /dev/null 2>&1; then
            log_info "Engine stopped gracefully after ${i}s"
            break
        fi
        sleep 1
    done
    # Force kill if still alive
    if pgrep -f 'python3 main.py' > /dev/null 2>&1; then
        log_info "SIGTERM did not stop engine, sending SIGKILL"
        pkill -9 -f 'python3 main.py' 2>/dev/null || true
        sleep 1
    fi
fi

if pgrep -f 'python3 main.py' > /dev/null 2>&1; then
    log_info "ERROR: processes still running after SIGKILL"
    ps aux | grep 'python3 main.py' | grep -v grep || true
    exit 1
fi

# ─── Step 4: Start engine (append to log, full fd redirect) ──────────────
log_info "Starting engine in $ENGINE_DIR"
cd "$ENGINE_DIR"
nohup python3 main.py >> "$CURRENT_LOG" 2>&1 </dev/null &
disown

# Wait briefly for startup
sleep 5

# ─── Step 5: Verify exactly 1 process running ────────────────────────────
PID_COUNT=$(pgrep -cf 'python3 main.py' 2>/dev/null || echo 0)
if [ "$PID_COUNT" -eq 0 ]; then
    log_info "ERROR: Engine failed to start"
    tail -20 "$CURRENT_LOG" 2>/dev/null >&2 || true
    exit 1
elif [ "$PID_COUNT" -gt 2 ]; then
    log_info "WARNING: expected 1-2 python3 main.py processes, got $PID_COUNT"
    ps aux | grep 'python3 main.py' | grep -v grep || true
    exit 1
fi

log_info "Engine started (PID $(pgrep -f 'python3 main.py' | head -1))"
exit 0
