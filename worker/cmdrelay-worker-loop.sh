#!/bin/bash
# cmdrelay worker loop — keeps worker.py running persistently on the VM.
# The worker itself loops across slickterm sessions (waiting idle when no
# session is active); this outer loop only covers crashes.
# Installed by install.sh --role worker to ~/.local/share/cmdrelay/
# (alongside worker.py); logs to ~/.local/share/cmdrelay/worker.log.
# No LLM tokens involved.
# Spawned by vm-boot-recovery.sh on VM replacement (guarded), or manually
# via `muse.exec background=true` from a persistent chat.
# To stop: kill this loop AND the worker.py child (pkill -f cmdrelay/worker.py).
# Guard: only one loop, via pid file. (pgrep is avoided here: the
# spawning wrapper's own command line can contain this script's name
# and false-positive as a "duplicate".) If another loop is already
# running, exit — so a duplicate wake / manual respawn can't spawn a
# second worker that would steal commands from the live session.
INSTALL_DIR="${CMDRELAY_INSTALL_DIR:-$HOME/.local/share/cmdrelay}"
WORKER_PY="$INSTALL_DIR/worker.py"
WORKER_LOG="$INSTALL_DIR/worker.log"
PID_FILE="$HOME/hooks/state/cmdrelay-worker-loop.pid"
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || echo "")"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null \
        && tr '\0' ' ' < "/proc/$OLD_PID/cmdline" 2>/dev/null | grep -q "cmdrelay-worker-loop.sh"; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') worker-loop: already running (pid $OLD_PID), exiting" >> "$WORKER_LOG" 2>&1
        exit 0
    fi
fi
echo "$$" > "$PID_FILE"
while true; do
    python3 "$WORKER_PY" >> "$WORKER_LOG" 2>&1
    sleep 2
done
