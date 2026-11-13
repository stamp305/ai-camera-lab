#!/bin/bash
# Upgrade a git-clone branch to this client version. Run after `git pull`:
#
#   bash tools/upgrade_branch.sh            upgrade (safe to run again)
#   bash tools/upgrade_branch.sh --check    report only, change nothing
#   bash tools/upgrade_branch.sh --stop     stop the client and its LaunchAgent
#   bash tools/upgrade_branch.sh --start    start them again
#
# Always runs the ai-camera conda env's Python, whatever env the shell has active:
# running this under (base) is how a branch ended up building for the wrong Python.
PY="${AICAM_PYTHON:-/opt/miniconda3/envs/ai-camera/bin/python}"
if [ ! -x "$PY" ]; then
    echo "❌ ไม่พบ $PY (conda env ai-camera)"
    exit 1
fi
exec "$PY" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/upgrade_branch.py" "$@"
