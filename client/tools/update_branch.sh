#!/bin/bash
# Bring a branch installed by git clone to the latest main and upgrade it.
#
# Works on a branch that does not have this file yet, so run it from origin/main:
#
#   cd /Applications/advice/ai-camera-client
#   git fetch origin && git show origin/main:tools/update_branch.sh > /tmp/update_branch.sh && bash /tmp/update_branch.sh
#
#   bash /tmp/update_branch.sh -y      no question before the real upgrade
#
# Handles both kinds of branch:
#   - on main already: fast-forward to origin/main;
#   - stuck on an old commit ("You are not currently on a branch"): move to main.
# Local edits under frontend/ (the retired Next.js UI: build output, .env, a hand
# fix here and there) are copied to the backup folder, then discarded. Anything
# else edited, or a commit made on the branch itself, stops the script untouched.
# Untracked files (.env, client_config.json, dlc_local/, logs, images) are never
# touched. Then tools/upgrade_branch.sh does the rest: stop, back up the DB,
# update, start.

APP_DIR="${AICAM_APP_DIR:-/Applications/advice/ai-camera-client}"
ASSUME_YES=0
[ "$1" = "-y" ] && ASSUME_YES=1
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/aicamera_upgrade_backups/$STAMP-git"

say()  { echo ""; echo "── $1"; }
ok()   { echo "  ✅ $1"; }
stop() { echo "  ❌ $1"; echo ""; echo "หยุดตรงนี้ ยังไม่ได้เปลี่ยนอะไรใน git"; exit 1; }

cd "$APP_DIR" 2>/dev/null || stop "ไม่พบโฟลเดอร์ $APP_DIR (ตั้ง AICAM_APP_DIR ถ้าอยู่ที่อื่น)"
git rev-parse --git-dir >/dev/null 2>&1 || stop "$APP_DIR ไม่ใช่ git repo"
echo "═══ อัปเดตสาขาจาก git: $APP_DIR"

say "1. ดึงข้อมูลล่าสุด"
git fetch origin --quiet || stop "git fetch ไม่ผ่าน (เน็ต หรือสิทธิ์เข้า Bitbucket)"
ok "origin/main = $(git log -1 --format='%h %s' origin/main)"

say "2. ไฟล์ที่ถูกแก้"
changed="$(git status --porcelain --untracked-files=no | cut -c4-)"
if [ -z "$changed" ]; then
    ok "ไม่มี"
else
    others="$(echo "$changed" | grep -v '^frontend/' || true)"
    if [ -n "$others" ]; then
        echo "$others" | sed 's/^/      /'
        stop "มีไฟล์นอก frontend/ ถูกแก้ — ต้องดูก่อนว่าเป็นของสาขาหรือไม่ (ส่งรายชื่อนี้ให้ทีมงาน)"
    fi
    mkdir -p "$BACKUP"
    # Hand edits (not build output) are kept as files and as one diff.
    echo "$changed" | grep -vE '^frontend/(\.next/|\.env$)' | while read -r f; do
        [ -f "$f" ] && mkdir -p "$BACKUP/$(dirname "$f")" && cp "$f" "$BACKUP/$f"
    done
    git diff -- frontend/ ':!frontend/.next' > "$BACKUP/frontend-edits.diff"
    git checkout -- frontend/ || stop "คืนค่าไฟล์ใน frontend/ ไม่ได้"
    ok "เก็บสำเนาไว้ที่ $BACKUP แล้วทิ้งการแก้ใน frontend/ ($(echo "$changed" | wc -l | tr -d ' ') ไฟล์ ส่วนใหญ่เป็นไฟล์ build)"
fi

say "3. ย้ายไป main ล่าสุด"
# A commit made right here (on main, or on a detached old commit) would be left
# behind by the move, so either one stops the script.
own="$(git log --oneline origin/main..HEAD)"
if [ -n "$own" ]; then
    echo "$own" | sed 's/^/      /'
    stop "โค้ดที่ใช้อยู่มี commit ที่ไม่อยู่บน origin — ต้องดูก่อน"
fi
if git show-ref --verify --quiet refs/heads/main; then
    own="$(git log --oneline origin/main..main)"
    if [ -n "$own" ]; then
        echo "$own" | sed 's/^/      /'
        stop "main ในเครื่องนี้มี commit ที่ไม่อยู่บน origin — ต้องดูก่อน"
    fi
fi
before="$(git log -1 --format='%h' HEAD)"
git checkout --quiet -B main origin/main || stop "ย้ายไป main ไม่ได้"
ok "$before → $(git log -1 --format='%h %s' HEAD)"

say "4. ตรวจก่อนอัปเกรด"
bash tools/upgrade_branch.sh --check

if [ "$ASSUME_YES" != 1 ]; then
    echo ""
    printf "อัปเกรดจริงเลยไหม (หยุด client, สำรอง DB, อัปเดต, เปิด client)? พิมพ์ yes: "
    read -r answer
    [ "$answer" = "yes" ] || { echo "ยังไม่อัปเกรด — โค้ดอยู่บน main แล้ว รันต่อเองได้: bash tools/upgrade_branch.sh"; exit 0; }
fi

say "5. อัปเกรด"
bash tools/upgrade_branch.sh
