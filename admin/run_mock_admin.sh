#!/bin/bash
# แอดมินจำลอง — สำเนาของ ai-camera-admin (python_server) ที่รันคู่กับแอดมินเทสตัวเดิมได้
#
#   ./admin/run_mock_admin.sh start     เปิด backend + frontend (เบื้องหลัง)
#   ./admin/run_mock_admin.sh stop
#   ./admin/run_mock_admin.sh status
#
# ต่างจากแอดมินจริงแค่:
#   - ทุกพอร์ตย้ายได้ (ค่าข้างล่าง) จึงไม่ชนกับแอดมินเทสที่ 8011 / 8002
#   - หน้า version manager เลือกได้ว่าเวอร์ชันไหนส่งเป็น .so (build farm) หรือเป็น Source (.py)
#   - กุญแจ DLC กับ builder key ไม่อยู่ใน git — สคริปต์นี้สร้างให้ครั้งแรก (admin/.mock.env,
#     admin/backend/.builder_key) ถ้าจะส่ง DLC ให้เครื่องจริงต้องใส่กุญแจ DLC ตัวจริงใน .mock.env เอง
#
# build worker ที่คุยกับแอดมินตัวนี้: ./run-worker-mock.sh (ที่รากของ repo)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REAL_ADMIN="${REAL_ADMIN:-$HOME/Documents/python_server/ai-camera-admin}"

BACKEND_PORT="${MOCK_BACKEND_PORT:-18011}"
FRONTEND_PORT="${MOCK_FRONTEND_PORT:-18002}"
CLIENT_UPDATE_PORT="${MOCK_CLIENT_UPDATE_PORT:-18432}"
UPDATE_CONTROL_PORT="${MOCK_UPDATE_CONTROL_PORT:-18433}"
# Python ตัวแรกที่ import ของที่ backend / frontend ใช้ได้ครบ — venv ของแอดมินจริงก่อน
# (venv backend ตัวจริงอาจตาม requirements.txt ไม่ทัน เช่นยังไม่มี Pillow) ชี้เองได้ด้วย MOCK_*_PY
pick_python() {
    local mods="$1"; shift
    for py in "$@"; do
        [ -x "$py" ] && "$py" -c "import $mods" >/dev/null 2>&1 && { echo "$py"; return; }
    done
    echo "!! no Python here imports: $mods" >&2; exit 1
}
BACKEND_PY="${MOCK_BACKEND_PY:-$(pick_python "PIL, fastapi, socketio, uvicorn, sqlalchemy, cryptography" \
    "$REAL_ADMIN/backend/venv/bin/python" /opt/miniconda3/bin/python3)}"
FRONTEND_PY="${MOCK_FRONTEND_PY:-$(pick_python "flask_socketio, eventlet, requests" \
    "$REAL_ADMIN/frontend/venv/bin/python" /opt/miniconda3/bin/python3)}"

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"
LOG_DIR="$HERE/log"
mkdir -p "$LOG_DIR"

first_run() {
    # กุญแจ DLC: ของจำลอง สร้างใหม่ครั้งเดียว (ส่ง DLC ให้เครื่องจริงไม่ได้จนกว่าจะใส่ตัวจริงแทน)
    if [ ! -f "$HERE/.mock.env" ]; then
        key="$("$BACKEND_PY" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
        printf 'AI_CAMERA_ADMIN_DLC_KEY=%s\n' "$key" > "$HERE/.mock.env"
        chmod 600 "$HERE/.mock.env"
        echo "created admin/.mock.env (mock DLC key)"
    fi
    if [ ! -f "$HERE/backend/.builder_key" ]; then
        "$BACKEND_PY" -c 'import secrets; print(secrets.token_hex(32))' > "$HERE/backend/.builder_key"
        chmod 600 "$HERE/backend/.builder_key"
        echo "created admin/backend/.builder_key"
    fi
    # repo ของ client: ยืมจากแอดมินจริงในเครื่อง (ไม่ต้องโหลดจาก Bitbucket ใหม่) แล้วชี้ origin กลับไป Bitbucket
    if [ ! -d "$HERE/backend/client_code/source/.git" ] && [ -d "$REAL_ADMIN/backend/client_code/source/.git" ]; then
        mkdir -p "$HERE/backend/client_code"
        git clone -q "$REAL_ADMIN/backend/client_code/source" "$HERE/backend/client_code/source"
        url="$(git -C "$REAL_ADMIN/backend/client_code/source" remote get-url origin)"
        git -C "$HERE/backend/client_code/source" remote set-url origin "$url"
        git -C "$HERE/backend/client_code/source" fetch -q origin || true
        echo "cloned client repo into admin/backend/client_code/source"
    fi
    mkdir -p "$HERE/backend/dlc_master"
}

export_env() {
    set -a; . "$HERE/.mock.env"; set +a
    export AI_CAMERA_ADMIN_BACKEND_PORT="$BACKEND_PORT"
    export AI_CAMERA_ADMIN_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
    export AI_CAMERA_ADMIN_BACKEND_PUBLIC_URL="http://127.0.0.1:$BACKEND_PORT"
    export AI_CAMERA_ADMIN_FRONTEND_PORT="$FRONTEND_PORT"
    export AI_CAMERA_ADMIN_FRONTEND_URL="http://127.0.0.1:$FRONTEND_PORT"
    export AI_CAMERA_ADMIN_CLIENT_UPDATE_PORT="$CLIENT_UPDATE_PORT"
    export AI_CAMERA_ADMIN_UPDATE_CONTROL_PORT="$UPDATE_CONTROL_PORT"
    # เปิดหน้าแอดมินได้จากเครื่องนี้และจาก LAN ที่ระบุ (ห้ามใช้ค่านี้บน production)
    export AI_CAMERA_ADMIN_EXTRA_ALLOWED_IPS="${MOCK_ALLOWED_IPS:-127.0.0.1,$LAN_IP}"
    # Device Keys sit in the store's MySQL, which this copy does not reach: trust every
    # Branch so a real machine can connect (MOCK_TRUST_DEVICES=0 to check keys again).
    export MOCK_TRUST_DEVICES="${MOCK_TRUST_DEVICES:-1}"
}

pid_of() { lsof -nP -t -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }

start() {
    first_run
    export_env
    if [ -n "$(pid_of "$BACKEND_PORT")" ]; then echo "backend already on :$BACKEND_PORT"; else
        (cd "$HERE/backend" && nohup "$BACKEND_PY" -u app_ai_camera_backend.py < /dev/null >> "$LOG_DIR/mock-backend.log" 2>&1 &)
    fi
    if [ -n "$(pid_of "$FRONTEND_PORT")" ]; then echo "frontend already on :$FRONTEND_PORT"; else
        (cd "$HERE/frontend" && nohup "$FRONTEND_PY" -u app_ai_camera_frontend.py < /dev/null >> "$LOG_DIR/mock-frontend.log" 2>&1 &)
    fi
    for _ in $(seq 1 40); do
        [ -n "$(pid_of "$BACKEND_PORT")" ] && [ -n "$(pid_of "$FRONTEND_PORT")" ] && break
        sleep 0.5
    done
    status
    echo "admin page : http://$LAN_IP:$FRONTEND_PORT/ai-camera/version-manager"
    echo "branch     : SOCKET_SERVER_HOST=http://$LAN_IP:$FRONTEND_PORT  SOCKET_SERVER_PATH=/ai-camera/socket.io"
    echo "logs       : $LOG_DIR/mock-{backend,frontend}.log"
}

stop() {
    for port in "$FRONTEND_PORT" "$BACKEND_PORT"; do
        pid="$(pid_of "$port")"
        [ -n "$pid" ] && kill "$pid" && echo "stopped :$port (pid $pid)"
    done
    # backend ฟังหลายพอร์ต (FastAPI + socket เก่า) ใน process เดียว — ปิดตัวนี้ทีเดียวพอ
    true
}

status() {
    for spec in "backend:$BACKEND_PORT" "frontend:$FRONTEND_PORT" "client-update:$CLIENT_UPDATE_PORT"; do
        name="${spec%%:*}"; port="${spec##*:}"
        pid="$(pid_of "$port")"
        if [ -n "$pid" ]; then state="running (pid $pid)"; else state="down"; fi
        printf '%-14s :%-6s %s\n' "$name" "$port" "$state"
    done
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    restart) stop; sleep 1; start ;;
    *) echo "usage: $0 start|stop|status|restart"; exit 2 ;;
esac
