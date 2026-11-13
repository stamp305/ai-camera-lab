#!/bin/bash
# What the LaunchAgent (com.advice.aicamera.client) runs; tools/upgrade_branch.py installs it.
#
# It replaces run_ai_camera_client.app + no_sleep.app in Login Items: launchd starts
# this at login and again whenever it exits, so a client that dies mid-day comes back
# on its own, and caffeinate keeps the Mac awake for as long as the client runs.
#
# The client is never started twice. Anything already running the client from this
# folder — the old Login Item app, someone in a terminal, or updater.py relaunching
# after an update (it starts bootstrap.py) — is watched instead, and this takes over
# only once that copy has exited.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${AICAM_PYTHON:-/opt/miniconda3/envs/ai-camera/bin/python}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

client_running() {
    local pid cwd
    for pid in $(pgrep -f 'client\.py|client_logic\.py|bootstrap\.py'); do
        [ "$pid" = "$$" ] && continue
        cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
        [ "$cwd" = "$APP_DIR" ] && return 0
    done
    return 1
}

# Right after boot the Login Item app starts the client too; let it win the race
# rather than start a second copy alongside it.
boot="$(sysctl -n kern.boottime | sed -E 's/^\{ sec = ([0-9]+),.*/\1/')"
if [ $(( $(date +%s) - boot )) -lt 300 ]; then
    sleep 60
fi

while client_running; do
    sleep 30
done

if [ ! -x "$PY" ]; then
    echo "$(date '+%F %T') python not found: $PY" >&2
    sleep 300
    exit 1
fi

cd "$APP_DIR" || exit 1
exec /usr/bin/caffeinate -ims "$PY" "$APP_DIR/client.py"
