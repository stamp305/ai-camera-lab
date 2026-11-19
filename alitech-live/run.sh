#!/bin/bash
# Alitech lab portal: ./alitech-live/run.sh start|stop|status   (port 18100)
# Needs ../.live.env (made by media/run_media.sh), the lab admin and MediaMTX running.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PORT="${ALITECH_LIVE_PORT:-18100}"
PY="${ALITECH_LIVE_PY:-$HOME/Documents/ai-camera-client-git/python_env/bin/python3.12}"
LAN_IP="${LAN_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)}"
LOG="$HERE/data/portal.log"
pid_of() { lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true; }

start() {
    [ -f "$ROOT/.live.env" ] || { echo "run media/run_media.sh start first"; exit 1; }
    [ -f "$HERE/app/static/vendor/portal.css" ] || bash "$HERE/fetch_assets.sh"
    [ -n "$(pid_of)" ] && { echo "already on :$PORT"; return; }
    mkdir -p -m 700 "$HERE/data"
    set -a; . "$ROOT/.live.env"; set +a
    export AICAM_WHEP_BASE="${AICAM_WHEP_BASE:-http://$LAN_IP:8889}"
    (cd "$HERE" && nohup "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --no-server-header \
        --no-access-log < /dev/null >> "$LOG" 2>&1 &)
    for _ in $(seq 1 30); do [ -n "$(pid_of)" ] && break; sleep 0.5; done
    echo "portal : http://$LAN_IP:$PORT/login"
    [ -s "$HERE/data/users.json" ] || echo "no account yet: cd alitech-live && $PY -m app.create_user <email> \"<name>\" admin"
}
stop() { p="$(pid_of)"; [ -n "$p" ] && kill "$p" && echo "stopped :$PORT" || echo "not running"; }
status() { [ -n "$(pid_of)" ] && echo "up :$PORT" || echo "down"; }
case "${1:-status}" in start) start ;; stop) stop ;; status) status ;; *) echo "usage: $0 start|stop|status"; exit 2 ;; esac
