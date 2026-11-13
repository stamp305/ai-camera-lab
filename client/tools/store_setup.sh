#!/bin/bash
# ตั้งค่าเครื่องสาขาหลัง git pull — ใช้ได้ทุกสาขา รันซ้ำได้ไม่จำกัด
#
#   bash tools/store_setup.sh          ตรวจอย่างเดียว ไม่แก้อะไร
#   bash tools/store_setup.sh --apply  ลงมือแก้
#
# ทำสามอย่าง: เติมคีย์ที่ขาดใน .env (ไม่ทับของเดิม) · ตรวจ keyguard · ตรวจของที่ git ให้ไม่ได้
# **ไม่แตะ** dlc_local/ · video/ · .lcache · ไม่รีสตาร์ท service
#
# ⚠️ ไฟล์นี้อยู่ใน git ซึ่งเครื่องสาขา clone ลงไป ⇒ ห้ามใส่กุญแจหรือรหัสผ่านที่ยังไม่ได้
# อยู่ใน client.py อยู่แล้ว โดยเฉพาะ keyguard — ดูเหตุผลที่หัวข้อ keyguard ด้านล่าง
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# ถ้า python ของ env หายไป ต้องรู้ตัวทันที ไม่ใช่ให้ทุกการตรวจ "ผ่าน" เพราะรันไม่ได้
# (เจอจริงตอนทดสอบ: ตัวตรวจ drift ผ่านเงียบ ๆ เพราะ python ไม่มี ผลว่างเลยดูเหมือนไม่มีปัญหา)
PY="/opt/miniconda3/envs/ai-camera/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

cd "$ROOT" || exit 1
FAIL=0
CHANGED=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
bad()  { printf '  ❌ %s\n' "$*"; FAIL=1; }

say ""
say "═══ ตั้งค่าเครื่องสาขา ═══"
say "โฟลเดอร์ : $ROOT"
say "commit   : $(git log --oneline -1 2>/dev/null || echo 'ไม่ใช่ git repo')"
[ "$APPLY" -eq 1 ] && say "โหมด     : ลงมือแก้" || say "โหมด     : ตรวจอย่างเดียว (ใส่ --apply เพื่อแก้จริง)"

# ─────────────────────────────────────────────────────────────── 1. .env
#
# `client.py:_create_backend_env()` เขียน .env **เฉพาะตอนยังไม่มีไฟล์** เครื่องที่ตั้ง
# มานานจึงไม่เคยได้คีย์ที่เพิ่มมาทีหลังเลยสักตัว และไม่มี error ให้เห็น — service ที่ต้องใช้
# คีย์นั้นจะใช้ค่า default ของตัวเองซึ่งอาจชี้คนละที่กับที่ตั้งใจ
#
# ค่า default **อ่านจาก `client.py:_create_backend_env` ตอนรัน** ไม่ได้พิมพ์ซ้ำไว้ที่นี่
#
# เหตุผลสองข้อ:
#  1. ในนั้นมีรหัสผ่าน (GLOBALAPI2_PASSWORD, DB_PASSWORD) การพิมพ์ซ้ำ = เพิ่มที่ที่ความลับ
#     อยู่อีกหนึ่งแห่งโดยไม่ได้อะไรกลับมา วันที่จะเลิกเก็บความลับใน source ต้องไล่แก้สองที่
#  2. ถ้าพิมพ์ซ้ำ มันจะล้าหลังเงียบ ๆ ทุกครั้งที่มีคนเพิ่มคีย์ใหม่ใน client.py
#     แล้วสาขาที่ตั้งมานานจะไม่ได้คีย์นั้นตลอดไปโดยไม่มีใครรู้
say ""
say "── 1. .env ──"

ENV_DEFAULTS=$("$PY" - <<'PYEOF' 2>&1
import pathlib, re

block = pathlib.Path("client_logic.py").read_text()
block = block.split("def _create_backend_env", 1)[-1].split("\n    try:", 1)[0]
for key, value in re.findall(r'f"([A-Z][A-Z0-9_]*)=([^"]*)\\n"', block):
    print(f"{key}={value}")
PYEOF
)

# น้อยกว่านี้แปลว่าอ่าน client_logic.py ไม่ออก (โครงไฟล์เปลี่ยน) — ต้องดังไม่ใช่เงียบ
# ไม่งั้นสคริปต์จะรายงานว่า ".env ครบแล้ว" ทั้งที่จริงคือมันไม่รู้จักคีย์ไหนเลย
N_DEFAULTS=$(printf '%s' "$ENV_DEFAULTS" | grep -c '=' || true)
if [ "${N_DEFAULTS:-0}" -lt 10 ]; then
  bad "อ่านค่า default จาก client_logic.py ไม่ออก (ได้ $N_DEFAULTS คีย์)"
  say "     โครงของ _create_backend_env เปลี่ยนไปแล้ว ต้องแก้ตัว regex ในสคริปต์นี้"
  ENV_DEFAULTS=""
else
  ok "อ่าน default จาก client_logic.py ได้ $N_DEFAULTS คีย์"
fi

if [ -z "$ENV_DEFAULTS" ]; then
  :
elif [ ! -f .env ]; then
  warn ".env ยังไม่มี — client.py จะสร้างให้เองตอนสตาร์ทครั้งแรก ข้ามขั้นนี้"
else
  MISSING=""
  while IFS='=' read -r k v; do
    [ -z "$k" ] && continue
    grep -q "^${k}=" .env || MISSING="${MISSING}${k}=${v}"$'\n'
  done <<< "$ENV_DEFAULTS"

  if [ -z "$MISSING" ]; then
    ok ".env มีคีย์ครบแล้ว"
  elif [ "$APPLY" -eq 1 ]; then
    BAK=".env.bak-$(date +%Y%m%d-%H%M%S)"
    cp .env "$BAK"
    printf '%s' "$MISSING" >> .env
    ok "เติม $(printf '%s' "$MISSING" | grep -c '=') คีย์ที่ขาด (สำรองไว้ที่ $BAK)"
    printf '%s' "$MISSING" | sed 's/=.*//' | sed 's/^/     + /'
    CHANGED=1
  else
    warn "ขาด $(printf '%s' "$MISSING" | grep -c '=') คีย์:"
    printf '%s' "$MISSING" | sed 's/=.*//' | sed 's/^/     - /'
  fi

  # VIDEO_PIPELINE_ONLY แยกออกมาเพราะเป็น **นโยบาย** ไม่ใช่ค่า default
  # ตั้งเองผิดพลาดแล้วสาขาจะรันระบบเก่าเงียบ ๆ ทั้งที่คิดว่า cutover แล้ว
  CUR=$(grep '^VIDEO_PIPELINE_ONLY=' .env | head -1 | cut -d= -f2)
  case "${CUR:-}" in
    1) ok "VIDEO_PIPELINE_ONLY=1 (รันท่อวิดีโอใหม่อย่างเดียว)" ;;
    0) warn "VIDEO_PIPELINE_ONLY=0 — สาขานี้ยังรัน service เก่าคู่ไปด้วย ตั้งใจหรือเปล่า" ;;
    *) bad "ไม่มี VIDEO_PIPELINE_ONLY ใน .env — จะได้ค่า 0 คือรันของเก่าคู่ไปด้วย"
       say "     ตั้งด้วย: echo 'VIDEO_PIPELINE_ONLY=1' >> .env" ;;
  esac
fi

# ─────────────────────────────────────────────────────── 2. keyguard
#
# ⚠️ **ห้ามให้สคริปต์นี้สร้าง keyguard เอง** keyguard ถือกุญแจที่ใช้ถอดรหัส dlc_local/
# ทั้งหมด สคริปต์นี้อยู่ใน git ที่เครื่องสาขา clone ลงไป ⇒ ฝังกุญแจไว้ที่นี่ = แจกกุญแจ
# ให้ทุกสาขาพร้อมกัน ซึ่งทำให้การเข้ารหัส DLC ไม่เหลือความหมาย
# (เหตุผลเดียวกับที่ `.gitignore` กัน `dlc_src/` และ `keyguard.py`)
#
# ก๊อปจากสาขาที่มีอยู่แล้วผ่าน AnyDesk เท่านั้น
say ""
say "── 2. keyguard ──"

if ! "$PY" -c "import keyguard" 2>/dev/null; then
  bad "ไม่มี keyguard — ทุก service ที่ import โมดูลเข้ารหัสจะสตาร์ทไม่ขึ้นทั้งหมด"
  say "     ก๊อป keyguard.py (หรือ keyguard.so) จากสาขาที่รันอยู่แล้วมาวางที่"
  say "     $ROOT/  ผ่าน AnyDesk file transfer"
  say "     (สคริปต์นี้สร้างให้ไม่ได้โดยตั้งใจ — มันอยู่ใน git จะกลายเป็นการแจกกุญแจ)"
else
  "$PY" -c "import keyguard; keyguard.fernet_legacy()" 2>/dev/null \
    && ok "keyguard.fernet_legacy() ใช้ได้" \
    || bad "มี keyguard แต่ fernet_legacy() พัง"

  # ตรวจของจริง ไม่ใช่แค่รูปแบบกุญแจ — กุญแจผิดตัวก็ผ่านการเช็คข้างบนได้
  SAMPLE=$(ls dlc_local/*/main.py 2>/dev/null | head -1)
  if [ -n "$SAMPLE" ]; then
    "$PY" - "$SAMPLE" <<'PYEOF' 2>/dev/null && ok "ถอดรหัส $(basename "$(dirname "$SAMPLE")") ได้จริง" || bad "ถอดรหัส dlc_local ไม่ได้ — กุญแจไม่ตรงกับ ciphertext ที่เครื่องนี้มี"
import sys, pathlib, keyguard
raw = pathlib.Path(sys.argv[1]).read_text()
if not raw.startswith("gAAAA"):
    sys.exit(0)
keyguard.fernet_legacy().decrypt(raw.encode())
PYEOF
  else
    warn "ไม่มีไฟล์ใน dlc_local/ ให้ทดสอบ — ยังไม่เคยรับ DLC push"
  fi
fi

# ─────────────────────────────────────── 3. ของที่ git pull ให้ไม่ได้
say ""
say "── 3. ของที่ git ให้ไม่ได้ ──"

[ -d dlc_local ] && ok "dlc_local/ มี $(ls dlc_local 2>/dev/null | wc -l | tr -d ' ') โฟลเดอร์: $(ls dlc_local 2>/dev/null | tr '\n' ' ')" \
                 || bad "ไม่มี dlc_local/ เลย — ต้อง DLC push ก่อน"

# ข้อมูล activate อยู่ในไฟล์เข้ารหัส .lcache (device_identity.py) — เปิดดูเนื้อหาจาก shell ไม่ได้ เช็คได้แค่ว่ามีไฟล์
_IDSTORE="$HOME/Library/Application Support/AICamera/.lcache"
[ -f "$_IDSTORE" ] && ok "identity store (.lcache) มีอยู่" \
                   || warn "ไม่มี .lcache — แอปยังไม่เคยบูตเวอร์ชันใหม่ หรือยังไม่ได้ activate"

# ทางถอยของ face_service — ดู playbook §0.1 ว่าทำไมห้ามให้หาย
if [ -f dlc_local/camera_service/models/yolo26n.pt ]; then
  ok "yolo26n.pt อยู่ใน camera_service/models (ทางถอยของ face_service)"
  say "     อย่าลืมติ๊ก camera_service ทุกครั้งที่ push ไม่งั้น delete_folder จะลบทิ้ง"
elif [ -f dlc_local/face_service/models/yolo26n.pt ]; then
  ok "yolo26n.pt อยู่ใน face_service/models"
else
  warn "ไม่มี yolo26n.pt ที่ไหนเลย — face_service (ลงเวลา) จะสตาร์ทไม่ขึ้น"
fi

git remote -v 2>/dev/null | grep -q ATATT \
  && bad "มี API token ฝังอยู่ใน git remote URL — ย้ายไปเก็บใน Keychain แล้วเพิกถอน token ตัวนั้น" \
  || ok "git remote ไม่มีความลับฝังอยู่"

# หมายเหตุ: ไม่มีตัวตรวจ "สคริปต์ตามทัน client.py มั้ย" อีกแล้ว เพราะขั้นที่ 1 อ่าน
# ค่า default จาก client.py ตอนรัน การหลุด sync จึงเกิดขึ้นไม่ได้ตั้งแต่แรก
# (ตัวที่กันไว้แทนคือ N_DEFAULTS < 10 ซึ่งดักกรณีอ่านไฟล์ไม่ออก)

# ────────────────────────────────────────────────────────────── สรุป
say ""
if [ "$FAIL" -eq 1 ]; then
  say "═══ ❌ ยังไม่พร้อมสตาร์ท — แก้ข้อที่เป็น ❌ ก่อน ═══"
  exit 1
fi
[ "$APPLY" -eq 0 ] && [ "$CHANGED" -eq 0 ] && say "═══ ✅ ผ่าน — รันซ้ำด้วย --apply ถ้ามี ⚠️ ที่ต้องแก้ ═══" \
                                           || say "═══ ✅ พร้อมสตาร์ท ═══"
say ""
say "ขั้นต่อไป:  open $ROOT/run_ai_camera_client.app"
say "รอ 90 วินาที แล้วตรวจด้วย: bash tools/store_check.sh"
say ""
