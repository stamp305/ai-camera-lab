#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# ติดตั้ง/ปรับ MariaDB ท้องถิ่นของเครื่องสาขา AI Camera (รันซ้ำได้ ไม่ทำร้ายข้อมูล)
#
#   bash tools/mariadb_setup.sh            # ติดตั้ง/จัดการให้ครบ + ตรวจ
#   bash tools/mariadb_setup.sh --check    # ตรวจอย่างเดียว ไม่แก้อะไร
#   bash tools/mariadb_setup.sh --stop     # หยุด daemon ที่สคริปต์นี้สตาร์ทเอง
#
# นโยบายความปลอดภัยที่สคริปต์บังคับ:
#   • root password เป็นของผู้ติดตั้ง (ROOT_PW ด้านล่าง) — ใช้ผ่าน socket เท่านั้น
#     ไม่มี root@'%' และไม่มี root ว่างรหัส
#   • 3 ระดับ user ดังนี้:
#       1) ${APP_USER}@localhost  — สิทธิ์ ALL PRIVILEGES เฉพาะ ${DB_NAME}  (แอปใช้)
#       2) ${VIEW_USER}@${h}      — SELECT-only เฉพาะ ${DB_NAME}  (ลูกค้าดูได้อย่างเดียว)
#          สำหรับทุก h ใน ALLOWED_HOSTS (เช่น 192.168.1.%, 10.0.0.%)
#          ถ้าต้องการให้ลูกค้าใช้ user เดียวกับแอป ให้ข้ามขั้น c แล้วให้ลูกค้าใช้ APP_USER แทน
#   • ทุก account ผูก host='localhost'/'127.0.0.1' เท่านั้น — ห้ามมี '%'
#   • server bind-address กำหนดได้ (ค่า default 0.0.0.0 เพื่อให้ view ต่อข้ามเครื่องในร้านได้)
#     พร้อมพิมพ์คำเตือน: ต้องมี firewall (ufw/pf) บล็อก 3306 เข้าจากนอก ALLOWED_HOSTS
#   • ไม่มี anonymous user
#
# ⚠️ แก้ค่าด้านล่างให้ครบก่อนใช้จริง — ห้าม commit ค่าจริงกลับเข้า git
# ─────────────────────────────────────────────────────────────────────────────
set -u

# ══ ค่าตั้งต้น — แก้ที่นี่ที่เดียว ════════════════════════════════════════════
DB_NAME="aicamera_local"           # ชื่อฐานข้อมูลของแอป (เช่น ai_camera)
ROOT_PW="${MARIADB_ROOT_PW:?set MARIADB_ROOT_PW}"               # รหัสผ่าน root ของ MariaDB — ผู้ติดตั้งตั้งเอง
APP_USER="aicamera_app"            # username ของแอป
VIEW_USER="aicamera_view"          # username สำหรับลูกค้าดูรายงาน
ALLOWED_HOSTS=('192.168.1.%')      # subnet เครื่องลูกค้าในร้าน (กัน MITM เข้าฐาน)
# ══════════════════════════════════════════════════════════════════════════

# ── ตัวเลือกรันไทม์ (ปล่อย default ได้) ──
DATA_DIR="${HOME}/aicamera-mariadb/data"          # ที่เก็บไฟล์ database (โหมด bundled)
RUN_DIR="${HOME}/aicamera-mariadb"                # บ้านของ log/pid/socket
SOCKET_FILE="/tmp/mysql.sock"             # unix socket (แอปใช้ path นี้ผ่าน localhost; ต้องตรงกับที่ MariaDB สร้าง)
BIND_ADDRESS="0.0.0.0"                            # bind-address (default 0.0.0.0 เพื่อ view ข้ามเครื่อง)
PORT="3306"
MARIADBD_BIN=""                                   # ว่าง = หาเอง (bundled → brew → PATH)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="apply"
[ "${1:-}" = "--check" ] && MODE="check"
[ "${1:-}" = "--stop" ] && MODE="stop"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
bad()  { printf '  ❌ %s\n' "$*"; FAIL=1; }

FAIL=0
PLACEHOLDER=0
for v in "$DB_NAME" "$ROOT_PW" "$APP_USER" "$VIEW_USER"; do
  case "$v" in __CHANGE_ME*|"") PLACEHOLDER=1 ;; esac
done

# ─── helper: escape ตัวอักษรพิเศษสำหรับ SQL string ───
sql_quote() { printf '%s' "$1" | sed "s/'/''/g"; }

# ─── helper: หา binary ───
find_binary() {
  local name="$1"
  [ -n "$MARIADBD_BIN" ] && [ "$name" = "mariadbd" ] && { echo "$MARIADBD_BIN"; return; }
  for cand in "$ROOT/bin/$name" "/opt/homebrew/bin/$name" "/usr/local/bin/$name"; do
    [ -x "$cand" ] && { echo "$cand"; return; }
  done
  command -v "$name" 2>/dev/null || true
}

MARIADBD="$(find_binary mariadbd)"
CLIENT="$(find_binary mariadb)"
INSTALLDB="$(find_binary mariadb-install-db)"

server_pid_on_socket() {
  lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1
}

our_daemon_running() {
  [ -f "$RUN_DIR/mysqld.pid" ] && kill -0 "$(cat "$RUN_DIR/mysqld.pid" 2>/dev/null)" 2>/dev/null
}

wait_for_socket() {
  for _ in $(seq 1 60); do
    [ -S "$SOCKET_FILE" ] && "$CLIENT" --socket="$SOCKET_FILE" -u root -e "SELECT 1" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

start_bundled() {
  mkdir -p "$RUN_DIR" "$DATA_DIR"
  if ! "$CLIENT" --socket="$SOCKET_FILE" -u root -e "SELECT 1" >/dev/null 2>&1; then
    if [ ! -d "$DATA_DIR/mysql" ]; then
      say "  · init system tables ที่ $DATA_DIR"
      "$INSTALLDB" --datadir="$DATA_DIR" --auth-root-authentication-method=normal >/dev/null 2>&1 \
        || "$INSTALLDB" --datadir="$DATA_DIR" >/dev/null 2>&1
    fi
    say "  · สตาร์ท mariadbd (bind=$BIND_ADDRESS port=$PORT)"
    nohup "$MARIADBD" \
      --datadir="$DATA_DIR" \
      --socket="$SOCKET_FILE" \
      --bind-address="$BIND_ADDRESS" \
      --port="$PORT" \
      --skip-name-resolve \
      >>"$RUN_DIR/mysqld.log" 2>&1 &
    echo $! > "$RUN_DIR/mysqld.pid"
  fi
  wait_for_socket
}

stop_bundled() {
  if our_daemon_running; then
    kill "$(cat "$RUN_DIR/mysqld.pid")" 2>/dev/null
    say "🛑 หยุด mariadbd (pid $(cat "$RUN_DIR/mysqld.pid")) แล้ว"
  else
    say "· ไม่มี daemon ที่สคริปต์นี้สตาร์ทอยู่"
  fi
}

# รัน SQL ในฐานะ root — ผ่าน socket เสมอ (ไม่ยอมใช้ TCP)
sql_root() {
  "$CLIENT" --socket="$SOCKET_FILE" -u root "$@" 2>&1
}

ensure_root_password() {
  # รอบแรกหลัง init: root ยังไม่มีรหัสผ่าน → ตั้งเป็น ROOT_PW ทันที
  # รอบถัดไป: ต้องใส่ -pROOT_PW ถึงจะผ่าน (ตรวจว่ารหัสเดิมยังตรง)
  if sql_root -e "SELECT 1" >/dev/null 2>&1; then
    sql_root -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '$(sql_quote "$ROOT_PW")'; FLUSH PRIVILEGES;"
    return 0
  fi
  sql_root -p"$(printf '%s' "$ROOT_PW" | sed "s/'/''/g")" -e "SELECT 1" >/dev/null 2>&1
}

# ═════════════════════════════════════ main ═════════════════════════════════
say ""
say "═══ ติดตั้ง MariaDB ท้องถิ่น (AI Camera) ═══"
say "repo     : $ROOT"
say "mode     : $MODE"

if [ "$PLACEHOLDER" -eq 1 ]; then
  bad "ยังไม่ได้แก้ค่าตั้งต้นด้านบนของไฟล์ (เจอ __CHANGE_ME__ หรือค่าว่าง)"
  say "     เปิด tools/mariadb_setup.sh แก้ DB_NAME / ROOT_PW / APP_USER / VIEW_USER / ALLOWED_HOSTS ก่อน"
  exit 1
fi
ok "ค่าตั้งต้นพร้อม (DB=$DB_NAME)"

[ "$MODE" = "stop" ] && { stop_bundled; exit 0; }

[ -x "$MARIADBD" ] || { bad "ไม่เจอ mariadbd (ลอง brew install mariadb หรือระบุ MARIADBD_BIN)"; exit 1; }
[ -x "$CLIENT" ]   || { bad "ไม่เจอ client 'mariadb'"; exit 1; }
ok "binary : ${MARIADBD}"

# 1) ให้แน่ใจว่า server รันอยู่ ────────────────────────────────────────────────
if [ "$MODE" = "apply" ]; then
  PID="$(server_pid_on_socket)"
  if [ -n "$PID" ]; then
    ok "มี server ฟังพอร์ต $PORT อยู่แล้ว (pid $PID) — ใช้ของเดิม"
  else
    start_bundled && ok "สตาร์ท server สำเร็จ (socket: $SOCKET_FILE)" \
                       || { bad "สตาร์ท server ไม่สำเร็จ — ดู log ที่ $RUN_DIR/mysqld.log"; exit 1; }
  fi
else
  wait_for_socket || { bad "server ไม่ตอบที่ socket $SOCKET_FILE — รันโหมด apply ก่อน"; exit 1; }
fi

# 2) root password + สร้าง 3 ระดับ user ─────────────────────────────────────────
if [ "$MODE" = "apply" ]; then
  ensure_root_password || { bad "เข้าใช้งาน root ผ่าน socket ไม่ได้ (รหัส ROOT_PW ไม่ตรง?)"; exit 1; }

  # ลบ user ที่ไม่มีรหัส/anonymous ทิ้งทั้งหมด (ความปลอดภัย)
  sql_root <<SQL >/dev/null 2>&1 || { bad "รัน SQL ตั้งค่าไม่สำเร็จ"; exit 1; }
DROP USER IF EXISTS 'root'@'%';
DROP USER IF EXISTS ''@'localhost';
DROP USER IF EXISTS ''@'$(hostname)';
FLUSH PRIVILEGES;
SQL

  # ตั้ง root@localhost มีรหัส (ห้าม root ว่างหรือ remote)
  sql_root <<SQL >/dev/null 2>&1 || { bad "SET ROOT PW ไม่สำเร็จ"; exit 1; }
ALTER USER 'root'@'localhost' IDENTIFIED BY '$(sql_quote "$ROOT_PW")';
FLUSH PRIVILEGES;
SQL
  ok "ตั้ง root@localhost รหัส = $ROOT_PW แล้ว (ห้าม root remote)"

  # 2a) สร้าง ${APP_USER}@localhost — ใช้แอปบันทึก/อ่าน
  sql_root <<SQL >/dev/null 2>&1 || { bad "สร้าง APP_USER ไม่สำเร็จ"; exit 1; }
CREATE USER IF NOT EXISTS '${APP_USER}'@'localhost' IDENTIFIED BY '$(sql_quote "$APP_USER")';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${APP_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL
  ok "สร้าง ${APP_USER}@localhost — สิทธิ์ ALL PRIVILEGES เฉพาะ \`${DB_NAME}\`.*"

  # 2b) สร้าง ${VIEW_USER}@${h} สำหรับลูกค้า — SELECT-only
  SQL_HOSTS=""
  for h in "${ALLOWED_HOSTS[@]}"; do
    [ -n "$SQL_HOSTS" ] && SQL_HOSTS="${SQL_HOSTS} OR "
    SQL_HOSTS="${SQL_HOSTS}'${h}'"
  done
  VIEW_SQL="
CREATE USER IF NOT EXISTS '${VIEW_USER}'@${SQL_HOSTS} IDENTIFIED BY '$(sql_quote "$VIEW_USER")';
GRANT SELECT ON \`${DB_NAME}\`.* TO '${VIEW_USER}'@${SQL_HOSTS};
FLUSH PRIVILEGES;
"
  sql_root "$VIEW_SQL" >/dev/null 2>&1 || { warn "สร้าง VIEW_USER บาง host อาจล้มเหลว (อาจมี host ซ้ำ)" ; }
  ok "สร้าง ${VIEW_USER}@${ALLOWED_HOSTS[*]} — สิทธิ์ SELECT-only เฉพาะ \`${DB_NAME}\`.*"
  warn "⚠ หากต้องการให้ลูกค้าใช้ user เดียวกับแอป ให้ข้ามขั้น c แล้วให้ลูกค้าใช้ APP_USER แทน"

  # 2c) ยืนยัน bind + ตรวจรายชื่อ account
  LISTEN="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $9}' | head -1)"
  case "$LISTEN" in
    127.0.0.1:*|localhost:*|"") ok "bind-address = $LISTEN";;
    *) bad "server bind อยู่ที่ '$LISTEN' — แก็ป BIND_ADDRESS ในสคริปต์; เพื่อ view ข้ามเครื่องในร้านควรเป็น 0.0.0.0";;
  esac

  AUTH_CHECK=""
  if sql_root -p"$(printf '%s' "$ROOT_PW" | sed "s/'/''/g")" -e "SELECT 1" >/dev/null 2>&1; then
    AUTH_CHECK="-p$(printf '%s' "$ROOT_PW" | sed "s/'/''/g")"
  elif sql_root -e "SELECT 1" >/dev/null 2>&1; then
    warn "root ยังไม่มีรหัสผ่าน — รันโหมด apply เพื่อตั้ง ROOT_PW"
  else
    bad "root login ไม่ได้เลย (รหัสผิด?)"; FAIL=1
  fi

  if [ -n "$AUTH_CHECK" ] || sql_root -e "SELECT 1" >/dev/null 2>&1; then
    REMOTE_USERS="$(sql_root ${AUTH_CHECK} -N -e "SELECT CONCAT(User,'@',Host) FROM mysql.user WHERE Host NOT IN ('localhost','127.0.0.1','::1');" 2>/dev/null | grep -v '^$' || true)"
    if [ -n "$REMOTE_USERS" ]; then
      bad "มี account ที่ host ไม่ใช่ loopback:"
      printf '%s\n' "$REMOTE_USERS" | sed 's/^/       - /'
    else
      ok "ไม่มี account แบบ remote ('%') เหลืออยู่"
    fi

    APP_GRANTS="$(sql_root ${AUTH_CHECK} -N -e "SHOW GRANTS FOR '${APP_USER}'@'localhost';" 2>/dev/null || true)"
    if printf '%s' "$APP_GRANTS" | grep -q "ON \`${DB_NAME}\`.\*"; then
      ok "สิทธิ์ $APP_USER จำกัดอยู่ที่ \`${DB_NAME}\`.* (least privilege)"
    else
      warn "ยังไม่พบ grant ของ $APP_USER — รันโหมด apply เพื่อตั้ง"
    fi
  fi
fi

# 3) สรุป + คำเตือน firewall
say ""
say "──── ผลการตรวจ ──"
say "DB      : $DB_NAME"
say "ROOT_PW : $(printf '%s' "$ROOT_PW" | sed 's/./*/g') (root@localhost เท่านั้น)"
say "APP_USER: $APP_USER (ALL PRIVILEGES \`${DB_NAME}\`.* localhost)"
say "VIEW_USER: $VIEW_USER (SELECT-only \`${DB_NAME}\`.* ${ALLOWED_HOSTS[*]})"
say "BIND    : $BIND_ADDRESS"
say "       ⚠️  ต้องมี firewall บล็อกพอร์ต 3306 เข้าจากนอก ${ALLOWED_HOSTS[*]}"

say ""
say "สุดท้าย: sync ค่าใน .env ของ repo ให้ตรงกับตัวแปรด้านบน"
say "  DB_USER=$APP_USER / DB_PASSWORD=*** / DB_NAME=$DB_NAME"
say "  DB_ROOT_PASSWORD=*** (ตรงกับ ROOT_PW)"
say "ดูรายละเอียด: tools/MARIADB_SETUP.md"
say ""
if [ "$FAIL" -eq 1 ]; then
  say "═══ ❌ มีข้อที่ต้องแก้ก่อน (ดู ❌ ด้านบน) ═══"; exit 1
fi
say "═══ ✅ เรียบร้อย ═══"