#!/bin/bash
# Copy the Alitech portal + analytics stylesheets and the icons the sidebar uses from a local
# checkout of the company repo (alitech-frontend) into app/static/vendor/.
# They are the company's files, so they stay out of this personal repo (gitignored).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${ALITECH_FRONTEND:-$HOME/Documents/Alitech/alitech-frontend}/app/static"
DST="$HERE/app/static/vendor"
[ -f "$SRC/css/portal.css" ] || { echo "alitech-frontend not found at $SRC (set ALITECH_FRONTEND)"; exit 1; }
mkdir -p "$DST/icons"
cp "$SRC/css/portal.css" "$DST/portal.css"
cp "$SRC/css/analytics.css" "$DST/analytics.css"
mkdir -p "$DST/loading"
cp "$SRC/images/loading/blob-loader-transparent.gif" "$DST/loading/"
for f in logo_aicam.png 1.svg 2.svg Sign_out.svg Headphones.svg Bell_Ring.svg; do
    cp "$SRC/images/icons/$f" "$DST/icons/$f"
done
echo "copied portal.css, analytics.css + icons into app/static/vendor"
