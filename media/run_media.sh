#!/bin/bash
# MediaMTX for the lab: ./media/run_media.sh start|stop|status
#
# First run makes ../.live.env (mode 600, gitignored) with the secrets the admin,
# MediaMTX and the Alitech lab page share. Nothing secret is in git or on a command line.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="$ROOT/.live.env"
LOG="$HERE/mediamtx.log"
MTX="$(command -v mediamtx || true)"
LAN_IP="${LAN_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)}"

make_env() {
    [ -f "$ENV_FILE" ] && return
    umask 077
    rnd() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$1"; }
    {
        echo "AICAM_LIVE_SERVICE_KEY=$(rnd 48)"
        echo "AICAM_MTX_API_SECRET=$(rnd 48)"
        echo "ALITECH_LIVE_SESSION_SECRET=$(rnd 64)"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "created .live.env"
}

pid_of() { lsof -nP -t -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }

start() {
    [ -n "$MTX" ] || { echo "mediamtx not found (brew install mediamtx)"; exit 1; }
    make_env
    if [ -n "$(pid_of 9997)" ]; then echo "mediamtx already running"; return; fi
    # Origins of the Alitech lab page allowed to open WHEP; host the browser uses.
    export MTX_WEBRTCALLOWORIGINS="http://$LAN_IP:18100,http://127.0.0.1:18100,http://localhost:18100"
    export MTX_WEBRTCADDITIONALHOSTS="$LAN_IP"
    (cd "$HERE" && nohup "$MTX" "$HERE/mediamtx.yml" < /dev/null >> "$LOG" 2>&1 &)
    for _ in $(seq 1 20); do [ -n "$(pid_of 9997)" ] && break; sleep 0.5; done
    status
    echo "set for the admin: AICAM_WHEP_BASE=http://$LAN_IP:8889"
}

stop() { pid="$(pid_of 9997)"; [ -n "$pid" ] && kill "$pid" && echo "stopped mediamtx (pid $pid)" || echo "not running"; }
status() { for p in 9997 8889 8189; do printf ":%s %s\n" "$p" "$( [ -n "$(lsof -nP -t -i:$p 2>/dev/null | head -1)" ] && echo up || echo down)"; done; }

case "${1:-status}" in start) start ;; stop) stop ;; status) status ;; *) echo "usage: $0 start|stop|status"; exit 2 ;; esac
