#!/bin/bash
# ตรวจสาขาหลังสตาร์ท — รันได้ตลอดเวลา ไม่แตะอะไรเลย
#
#   bash tools/store_check.sh
#
# เรียงตามลำดับ "พังแล้วเสียหายมากที่สุดก่อน" ไม่ใช่ตามลำดับของท่อ
# ทุกข้อที่เป็น ❌ คือของที่**พังเงียบ** ไม่มี error ขึ้นที่ไหน ถ้าไม่ตรวจก็ไม่มีใครรู้
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
FAIL=0

ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
bad()  { printf '  ❌ %s\n' "$*"; FAIL=1; }

# ดึงค่าจาก JSON ด้วย path แบบจุด เช่น  jget retention.incidents_total
# เดินคีย์ทีละชั้นแทน eval — สคริปต์นี้รันบนเครื่องลูกค้าและกิน JSON จาก HTTP
# ต่อให้เป็น localhost ก็ไม่มีเหตุผลที่จะให้ payload สั่งรันโค้ดได้
jget() {
  python3 -c '
import sys, json
try:
    node = json.load(sys.stdin)
    for key in sys.argv[1].split("."):
        node = node[key]
    print("" if node is None else node)
except Exception:
    pass
' "$1" 2>/dev/null
}

printf '\n═══ ตรวจสาขา  %s ═══\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# สาขานี้ใช้ท่อวิดีโอใหม่ (recorder→consumer→analyzer→report) หรือยังเป็นชุดเดิม
# (camera/in_pass/reid ...) — ดูจากตาราง service_ports ที่หน้า /system ใช้
# ต่อ DB ไม่ได้ก็ดูว่ามีตัวไหนใน 8012-8015 ถือพอร์ตอยู่มั้ย
PIPE=0
if REG=$(mysql ai_camera -N -B -e "SELECT name FROM service_ports" 2>/dev/null); then
  printf '%s\n' "$REG" | grep -qE '^(recorder|consumer|analyzer|report)_service$' && PIPE=1
else
  for p in 8012 8013 8014 8015; do
    c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "localhost:$p/" 2>/dev/null)
    [ -n "$c" ] && [ "$c" != "000" ] && PIPE=1
  done
fi

# ── 1. service ขึ้นครบมั้ย ────────────────────────────────────────────
printf '\n── 1. service ──\n'
# path ที่ตรวจต่อ service: data/frontend มี health ของตัวเอง (หน้า / ของ frontend
# เด้งไป /login และ data ไม่มี route /) ส่วน face ไม่มี route / — ตอบ HTTP อะไรก็ได้
# แปลว่ามีโปรเซสถือพอร์ตอยู่ ถือว่าขึ้นแล้ว
for spec in 8000:data:/health 8012:recorder:/ 8013:consumer:/ 8014:analyzer:/ \
            8015:report:/ 3000:frontend:/frontend-health 8009:face:ANY \
            8003:camera:ANY 8004:in_pass:ANY 8016:reid:ANY; do
  port="${spec%%:*}"; rest="${spec#*:}"; name="${rest%%:*}"; path="${rest#*:}"
  # ตรวจเฉพาะชุดที่สาขานี้ใช้ (พอร์ตตามค่าเริ่มต้น ถ้าแก้ที่หน้า /system ต้องแก้ตรงนี้ด้วย)
  case "$name" in
    recorder|consumer|analyzer|report) [ "$PIPE" = 0 ] && continue ;;
    camera|in_pass|reid)               [ "$PIPE" = 1 ] && continue ;;
  esac
  url="localhost:$port${path/ANY//}"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)
  if [ "$code" = "200" ] || { [ "$path" = "ANY" ] && [ -n "$code" ] && [ "$code" != "000" ]; }; then
    ok "$name ($port)"
  elif [ -z "$code" ] || [ "$code" = "000" ]; then
    if [ "$name" = "face" ]; then
      warn "$name ($port) ไม่ตอบ — ลงเวลาใช้ไม่ได้ (ตั้งใจปิดไว้หรือเปล่า)"
    else
      bad "$name ($port) ไม่ตอบ — ไม่มีโปรเซสถือพอร์ตนี้ (ยังไม่ได้ส่ง service ลงเครื่อง / นอกเวลาร้าน / ล้ม)"
    fi
  else
    bad "$name ($port) ตอบ HTTP $code ที่ ${path}"
  fi
done

# ── 2. retention — พังแล้วข้อมูลหายถาวร ต้องมาก่อนทุกอย่าง ───────────
if [ "$PIPE" = 0 ]; then
  printf '\n── 2-4. ท่อวิดีโอ ──\n'
  ok "สาขานี้ยังใช้ชุด service เดิม (ไม่มี recorder/consumer/analyzer/report) ข้าม"
else
printf '\n── 2. retention (เสียแล้วกู้ไม่ได้) ──\n'
R=$(curl -s --max-time 5 localhost:8012/ 2>/dev/null)
if [ -z "$R" ]; then
  bad "recorder ไม่ตอบ ข้ามการตรวจ"
else
  cams=$(printf '%s' "$R" | jget cameras_running)
  tot=$(printf '%s' "$R" | jget cameras_total)
  inc=$(printf '%s' "$R" | jget retention.incidents_total)
  lost=$(printf '%s' "$R" | jget retention.minutes_lost_total)
  [ "${cams:-0}" = "${tot:-1}" ] && ok "กล้องอัดอยู่ $cams/$tot" || bad "กล้องอัดอยู่แค่ $cams จาก $tot"
  [ "${inc:-1}" = "0" ] && ok "ไม่เคยเสียวิดีโอที่ยังไม่ได้ทำ" \
                        || bad "เคยเสียวิดีโอ $inc ครั้ง รวม $lost นาที — ข้อมูลหายถาวร"
fi

# ── 3. consumer ตามทันมั้ย — ตามไม่ทันจะจบที่ข้อ 2 ───────────────────
printf '\n── 3. consumer ──\n'
C=$(curl -s --max-time 5 localhost:8013/ 2>/dev/null)
if [ -z "$C" ]; then
  bad "consumer ไม่ตอบ"
else
  st=$(printf '%s' "$C" | jget status)
  bl=$(printf '%s' "$C" | jget backlog)
  pr=$(printf '%s' "$C" | jget processed)
  rt=$(printf '%s' "$C" | jget realtime_ratio_avg)
  er=$(printf '%s' "$C" | jget last_error)
  printf '     status=%s backlog=%s ทำแล้ว=%s ratio=%s\n' "$st" "$bl" "$pr" "$rt"
  [ "$st" = "behind" ] && bad "ตามไม่ทัน — backlog จะโตจนชน retention" || ok "สถานะ $st"
  [ -n "${er:-}" ] && [ "$er" != "" ] && bad "last_error: $er" || ok "ไม่มี error ค้าง"
  # client.log is sealed (log_seal.py); these markers are kept in the client's events file.
  grep -aqE "ปิด detect pool|detect worker ล้มเหลว" "$(getconf DARWIN_USER_TEMP_DIR)com.advice.aicamera/service_events" 2>/dev/null \
    && warn "detect pool เคยล้ม ถอยไปทำในโปรเซสหลัก (ช้าลง ~5 เท่า)" \
    || ok "detect pool ปกติ"
fi

# ── 4. analyzer — โซนไม่ตรงกล้องคือตัวที่เงียบที่สุดในระบบ ───────────
printf '\n── 4. analyzer ──\n'
A=$(curl -s --max-time 5 localhost:8014/ 2>/dev/null)
if [ -z "$A" ]; then
  bad "analyzer ไม่ตอบ"
else
  printf '%s' "$A" | python3 -c '
import sys, json
d = json.load(sys.stdin)
res = d.get("last_run", {}).get("results") or []
print(f"     demo_apple: {d.get(\"demo_validator\")}")
if not res:
    print("  ⚠️  ยังไม่ได้วิเคราะห์รอบไหนเลย (รอบละ 5 นาที เพิ่งสตาร์ทหรือเปล่า)")
for r in res:
    nz = r.get("segments_without_zone", 0)
    flag = "❌" if nz else "✅"
    print(f"  {flag} {r[\"day\"]}  segment {r[\"segments\"]}  เขียน {r[\"logs_written\"]}  "
          f"ล้ม {r[\"logs_failed\"]}  ไม่มีโซน {nz}")
    if nz:
        print("       โซนไม่ตรงกล้อง -> hotzone/rate/charge จะเป็นศูนย์ตอนสิ้นวันแบบไม่มี error")
    if r.get("logs_failed"):
        print("       post_log ไม่มี retry -> แถวที่ล้มหายถาวร")
'
fi

fi

# ── 5. frontend proxy ────────────────────────────────────────────────
printf '\n── 5. frontend ──\n'
# ทุกหน้าอยู่หลัง login แล้ว: ไม่มี session ต้องได้ 303 ไป /login (ไม่ใช่ 307 ที่
# เบราว์เซอร์เครื่องอื่นใน LAN ตามไม่ได้) และหน้า /login เองต้องเปิดได้
# GET ไม่ใช่ HEAD (-I): middleware ตอบ 401 ให้ทุก method ที่ไม่ใช่ GET โดยตั้งใจ
H=$(curl -s -o /dev/null -D - --max-time 5 "http://127.0.0.1:3000/store" 2>/dev/null | tr -d '\r')
S=$(printf '%s' "$H" | head -1)
L=$(printf '%s' "$H" | awk -F': ' 'tolower($1)=="location"{print $2}')
case "$S" in
  *303*) [ "$L" = "/login" ] && ok "หน้าในร้านล็อกไว้ เด้งไป /login" || bad "303 ไปที่ ${L:-ไม่มี location}" ;;
  *200*) bad "/store เปิดได้โดยไม่ต้อง login" ;;
  *307*) bad "ยังคืน 307 ให้เบราว์เซอร์ — เปิดจากเครื่องอื่นใน LAN จะพัง (ต้อง git pull)" ;;
  *)     bad "frontend ไม่ตอบ: ${S:-ไม่มีคำตอบ}" ;;
esac
LC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:3000/login" 2>/dev/null)
[ "$LC" = "200" ] && ok "หน้า /login เปิดได้" || bad "หน้า /login ตอบ HTTP ${LC:-000}"

# ── 6. ข้อมูลถึง DB จริงมั้ย ─────────────────────────────────────────
printf '\n── 6. ข้อมูลถึง DB ──\n'
# ไม่ใส่ -u/-p ในสคริปต์: user/รหัสอ่านจาก ~/.my.cnf (chmod 600) เท่านั้น
# รหัสจะได้ไม่โผล่ใน ps / shell history / ตัวสคริปต์
if command -v mysql >/dev/null 2>&1; then
  mysql ai_camera -N -B -e "
    SELECT CONCAT('     แถววันนี้ ', COUNT(*), ' · ล่าสุด ', IFNULL(MAX(timestamp),'ไม่มี'))
    FROM in_pass_logs WHERE DATE(timestamp)=CURDATE();
    SELECT CONCAT('     เวลาร้าน ', open_time, ' - ', close_time) FROM store LIMIT 1;
  " 2>/dev/null || warn "ต่อ MariaDB ไม่ได้ — ตั้ง ~/.my.cnf ([client] user/password, chmod 600) ด้วย user ที่มีแค่ SELECT บน ai_camera"
else
  warn "ไม่มีคำสั่ง mysql ข้ามการตรวจ DB"
fi

# ── 7. โปรเซสค้างจากรอบก่อน ──────────────────────────────────────────
printf '\n── 7. โปรเซสกำพร้า ──\n'
ORPH=$(ps -eo pid,ppid,command | grep "[s]pawn_main" | awk '$2==1' | wc -l | tr -d ' ')
[ "$ORPH" = "0" ] && ok "ไม่มีโปรเซสกำพร้า" \
                  || warn "มี $ORPH โปรเซสค้างจากรอบก่อน (ppid 1) — kill ทิ้งได้: ps -eo pid,ppid,command | grep '[s]pawn_main'"

printf '\n'
[ "$FAIL" -eq 1 ] && printf '═══ ❌ มีข้อที่ต้องแก้ ═══\n\n' && exit 1
printf '═══ ✅ ผ่านหมด ═══\n\n'
