#!/bin/bash
# Is this branch ready to feed the Heatmap Floorplan card? Run on the branch Mac:
#     bash check_floorplan_ready.sh
# Reads state and logs only: no Device Key, no other secret, changes nothing.
# Not shipped: tools/ is excluded from builds.

A="$HOME/Library/Application Support/AICamera"
LOG="$A/log/log_service.log"
# The app's own Python; a branch run from source (git checkout) uses whatever python3 is on PATH.
PY=/Applications/AICamera.app/Contents/Resources/app/python_env/bin/python3.12
[ -x "$PY" ] || PY=$(command -v python3)
SERVER=https://app.advice.th
ok()   { echo "  ✅ $*"; }
bad()  { echo "  ❌ $*"; }
warn() { echo "  ⚠️  $*"; }

echo "1. App"
# bootstrap.py for the built app, client.py for a branch run from source.
PID=$(pgrep -f "bootstrap.py|[c]lient.py" | head -1)
if [ -n "$PID" ]; then
  ok "running since $(ps -o lstart= -p "$PID")"
  # A branch run from source sets these on purpose, to the real server; only another host is a test left behind.
  OVERRIDE=$(ps eww -o command= -p "$PID" | grep -oE "(SOCKET_SERVER_HOST|AI_CAMERA_ADMIN_API_BASE)=[^ ]+" | grep -v "app\.advice\.th")
  if [ -z "$OVERRIDE" ]; then ok "talks to the real server (no test override)"; else bad "test override still set: $OVERRIDE — quit the app and open it normally"; fi
else
  bad "AICamera is not running"
fi

echo "2. Server reachable"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST -H 'Content-Type: application/json' -d '{}' "$SERVER/ai-camera/api/traffic/zone-hour")
case "$CODE" in
  401) ok "$SERVER answers (401 without a key, as it should)";;
  000) bad "$SERVER not reachable from this branch";;
  *)   warn "$SERVER answered $CODE";;
esac

echo "3. log_service"
if curl -s -m 3 http://127.0.0.1:8010/ | grep -q Hello; then ok "running on :8010"; else bad "not answering on :8010"; fi
if [ -f "$A/managed_services/log_service/heatmap_hour.py" ]; then
  warn "an old test copy (heatmap_hour.py) is installed: push log_service from main again"
else
  ok "no leftover heatmap_hour.py"
fi

echo "4. floorplan_service (picture history)"
if [ -f "$A/pipeline/floorplan/history.json" ]; then
  ok "new version: history.json present, $(ls "$A/pipeline/floorplan/images" 2>/dev/null | wc -l | tr -d ' ') pictures kept"
else
  bad "old version: no pipeline/floorplan/history.json — push floorplan_service from main"
fi

echo "5. Floorplans on this branch (data_service)"
# data_service is on :8000 by default; a branch can move it (service_ports table), so the
# listening Python ports are tried too, the first that answers /floorplan with a list wins.
DS=""
for port in 8000 $(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk '/[Pp]ython/ {n=split($9,a,":"); print a[n]}' | sort -un); do
  case "$(curl -s -m 3 "http://127.0.0.1:$port/floorplan")" in "["*) DS=$port; break;; esac
done
FP=$([ -n "$DS" ] && curl -s -m 5 "http://127.0.0.1:$DS/floorplan")
if [ -n "$FP" ] && [ -n "$PY" ]; then
  echo "     data_service on :$DS"
  echo "$FP" | DS=$DS "$PY" -c '
import json, os, sys, urllib.request
for f in json.load(sys.stdin):
    try:
        code = urllib.request.urlopen("http://127.0.0.1:%s/%s" % (os.environ["DS"], f["imageUrl"].lstrip("/")), timeout=5).status
    except Exception as e:
        code = getattr(e, "code", "unreachable")
    mark = "✅" if f.get("isCalibrated") and code == 200 else ("➖" if not f.get("cameraCount") else "❌")
    print("  %s floorplan %s \"%s\": %s cameras, calibrated=%s, picture HTTP %s"
          % (mark, f["id"], f["name"], f.get("cameraCount", 0), f.get("isCalibrated"), code))
'
else
  bad "no data_service answering /floorplan on this machine"
fi

echo "6. Last pipeline uploads (the zip the server preprocesses)"
if [ -f "$LOG" ]; then
  grep -E "pipeline upload (ok|failed|request failed)" "$LOG" | tail -3 | sed -E 's/ -> \{.*//; s/^/     /'
  MISSING=$(grep -E "floorplan image .* not found" "$LOG" | tail -1)
  LAST_OK=$(grep "pipeline upload ok" "$LOG" | tail -1 | cut -c1-19)
  if [ -n "$LAST_OK" ]; then ok "last upload ok at $LAST_OK"; else bad "no successful upload in the log"; fi
  if [ -n "$MISSING" ] && [[ "$(echo "$MISSING" | cut -c1-19)" > "$LAST_OK" ]]; then warn "a picture could not be found: $(echo "$MISSING" | cut -c1-160)"; fi
else
  bad "no log_service.log"
fi
if [ -f "$A/pipeline/pipeline_upload/floorplan_uploaded.json" ]; then
  ok "pictures reached the server with a zip (new pipeline_upload)"
else
  warn "no picture has gone out with a zip yet (new log_service, after its first upload at minute 0)"
fi

echo "7. Zone Hour feed"
grep -E "zone-hour .* hours taken|zone-hour .* answered" "$LOG" 2>/dev/null | tail -2 | sed 's/^/     /'
