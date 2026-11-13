#!/bin/bash
set -e

echo "[1/4] Preparing Release Directory..."
rm -rf release 2>/dev/null || true
mkdir -p release/AICamera.app/Contents/MacOS
mkdir -p release/AICamera.app/Contents/Resources/app

echo "[2/4] Generating Info.plist and Launcher..."
cat << 'EOF' > release/AICamera.app/Contents/Info.plist
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>AICameraLauncher</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.advice.aicamera</string>
    <key>CFBundleName</key>
    <string>AICamera</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
EOF

cat << 'EOF' > release/AICamera.app/Contents/MacOS/AICameraLauncher
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/../Resources/app"

# Put the startup window up before anything else: bootstrap.py spends seconds to
# minutes (key derivation, first-run quarantine scan, database init) before it
# could show one itself, and a double-click that shows nothing reads as broken.
SUPPORT="$HOME/Library/Application Support/AICamera"
# The status file lives in this user's private temporary folder (runtime_paths.py).
RUN_DIR="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)"
RUN_DIR="${RUN_DIR:-${TMPDIR:-/tmp/}}com.advice.aicamera"
STATUS="$RUN_DIR/startup_status"
mkdir -p "$SUPPORT/logs" 2>/dev/null
mkdir -p -m 700 "$RUN_DIR" 2>/dev/null

# Everything below writes into $SUPPORT. An installer that ran as root once left it
# owned by root, and the app then died on its first line with nothing on screen.
if [ ! -w "$SUPPORT" ] || [ ! -w "$SUPPORT/logs" ]; then
    osascript -e "display alert \"AICamera cannot start\" message \"The app cannot write to its own data folder (it belongs to another user):

$SUPPORT

Open Terminal and run:
sudo chown -R $USER:staff \\\"$SUPPORT\\\"

then open AICamera again.\" as critical" >/dev/null 2>&1
    exit 1
fi

rm -f "$STATUS"
# The window runs through "AICamera Startup.app" so its Dock tile carries the AICamera
# name and icon instead of "python3.12" (see the helper step below). Tcl looks for its
# library next to the program it was started as, so point it at the real one.
HELPER="$DIR/../Helpers/AICamera Startup.app/Contents/MacOS/python3.12"
if [ -f .loading_gui.py ]; then
    if [ -x "$HELPER" ]; then
        TCL_LIBRARY="$PWD/python_env/lib/tcl8.6" TK_LIBRARY="$PWD/python_env/lib/tk8.6" \
            "$HELPER" .loading_gui.py >/dev/null 2>&1 &
    else
        ./python_env/bin/python3.12 .loading_gui.py >/dev/null 2>&1 &
    fi
    export AICAMERA_SPLASH_SHOWN=1
fi

# No launcher.log: raw stderr mixed every library's output with paths a shop
# could read. What matters is logged instead: uncaught errors and MariaDB's
# messages go to bootstrap.log, services' errors to client.log (through the
# filter in log_redact.py). Files left by older versions go too.
rm -f "$SUPPORT/logs/launcher.log"*
./python_env/bin/python3.12 bootstrap.py 2>/dev/null
rc=$?
# A normal shutdown ends in os._exit(0) or a signal (rc >= 128); anything else is
# a crash, which the window turns into a message with a button to the logs.
if [ "$rc" -ne 0 ] && [ "$rc" -lt 128 ]; then
    printf '{"state": "error", "text": "The app could not start (exit %s). Please send the logs folder to support."}' "$rc" > "$STATUS"
fi
exit "$rc"
EOF
chmod +x release/AICamera.app/Contents/MacOS/AICameraLauncher

echo "[3/4] Compiling Python files to .so with Cython..."
# The version goes inside the compiled code (_build_info.so, read first by
# version_store) instead of shipping VERSION / client_version.json, plain files
# a shop could open and edit. Removed from the repo again after bundling.
# A fresh install from the .dmg always says V.1.0.0: which version a branch
# really runs is the admin's call, and on first identify it pushes the
# assigned one (each update payload carries its own _build_info.so).
APP_VERSION="V.1.0.0"
printf 'VERSION = "%s"\n' "$APP_VERSION" > _build_info.py
# The web UI's templates/ and static/ ship sealed in frontend_web/assets.bin,
# under a key derived from the DLC key in _secrets (frontend_web/assets_vault.py).
# Removed from the working copy however the build ends: left there, a source
# checkout would serve the packed copy instead of the pages being edited.
trap './python_env/bin/python3.12 tools/pack_ui_assets.py --clean' EXIT
./python_env/bin/python3.12 tools/pack_ui_assets.py
./python_env/bin/python3.12 build_cython.py

if [ -f "data_service/configs/secure_env.pyx" ]; then
    echo "[3b/4] Compiling Cython secure_env..."
    # secure_env.pyx is GENERATED from _secrets.py. Compiling whatever copy is
    # lying around shipped a dev machine's test-admin address
    # (a LAN address) in every build for weeks, silently, because nothing
    # regenerated it. Regenerate from _secrets.py on every build instead.
    # env -i: generate_secure_env.py lets real environment variables win over
    # _secrets.py, and this repo's .env holds a 127.0.0.1 test admin.
    env -i HOME="$HOME" PATH=/usr/bin:/bin ./python_env/bin/python3.12 generate_secure_env.py
    # A bare cythonize() call only emits the .c file, never actually compiles
    # it — setup_secure_env.py does this correctly via setuptools.setup(),
    # which is what actually produces the loadable .so.
    ./python_env/bin/python3.12 setup_secure_env.py build_ext --inplace
    # build_cython.py renamed an earlier build to secure_env.so; the line above
    # writes the ABI-suffixed name instead, so both end up on disk and the
    # stale one ships too. Python prefers the ABI name, which hides the
    # problem — until a build that skips this step silently falls back to the
    # old credentials baked into secure_env.so.
    rm -f data_service/configs/secure_env.so
else
    echo "[3b/4] secure_env.pyx not present — skipping (not referenced by current data_service source)"
fi

echo "[4/4] Assembling Application Bundle..."
# Copy everything except dev-only, test-only, backup, and local build artifacts.
rsync -avL --exclude='release' --exclude='dist' --exclude='build' --exclude='.git' --exclude='.DS_Store' \
          --exclude='temp_update' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
          --exclude='python_env' --exclude='bin' --exclude='db_support' --exclude='node_modules' \
          --exclude='backup_*' --exclude='build_test_sandbox' --exclude='*.pyx' --exclude='*.c' \
          --exclude='setup_secure_env.py' --exclude='__compile_cython.py' --exclude='build_cython.py' --exclude='build_phase4.py' \
          --exclude='build_mac_app.sh' --exclude='build_mac_app.sh.bak' --exclude='AICamera.spec' \
          --exclude='scripts' --exclude='tests' --exclude='mock_*' --exclude='test_*.py' --exclude='*_test.py' --exclude='fix_*.py' \
          --exclude='*.py.bak' --exclude='*.bak' --exclude='*.bak-*' --exclude='*.orig' --exclude='.pytest_cache' \
          --exclude='.env' --exclude='.env.*' --exclude='*-backup' --exclude='*.production-backup' \
          --exclude='/.claude' --exclude='/.stale_*' --exclude='/cusemp_rate_log' --exclude='/video' \
          --exclude='*.log' \
          --exclude='/data_service/static/logs/*' --exclude='/data_service/static/images/floorplans/*' \
          --exclude='_secrets.example.py' --exclude='generate_secure_env.py' --exclude='frontend_python' \
          --exclude='/CLAUDE.md' --exclude='/.gitignore' --exclude='/tools' --exclude='/remote_agent.py' \
          --exclude='/tests_*.py' --exclude='/build_dmg.sh' --exclude='/build_pkg.sh' \
          --exclude='/manifest.json' --exclude='/DELETE_MANIFEST.json' \
          --exclude='/VERSION' --exclude='/client_version.json' --exclude='/_build_info.py' \
          --exclude='/custom_tracking.yaml' \
          --exclude='/icon.png' --exclude='/no_sleep.app' --exclude='/run_ai_camera_client.app' \
          --exclude='/person.png' --exclude='/*.jpg' --exclude='/client_core.so' \
          --exclude='/analyzer_state' --exclude='/upload_state' --exclude='/rate' \
          --exclude='/ViT-FaceMask-Finetuned' --exclude='/yolov8n-face.pt' \
          --exclude='/face_landmarker.task' --exclude='/hand_landmarker.task' \
          --exclude='/client.py' --exclude='/client.so' --exclude='/requirements.txt' --exclude='/logs' \
          --exclude='/frontend_web/templates' --exclude='/frontend_web/static' \
          ./ release/AICamera.app/Contents/Resources/app/ 2>/dev/null || true

# Clean any accidental dev/test leftovers from the bundle before shipping.
find release/AICamera.app/Contents/Resources/app -type d \( -name '__pycache__' -o -name '.pytest_cache' -o -name 'tests' -o -name 'scripts' -o -name 'backup_*' -o -name 'build_test_sandbox' \) -prune -exec rm -rf {} + 2>/dev/null || true
find release/AICamera.app/Contents/Resources/app \( -name 'test_*.py' -o -name '*_test.py' -o -name 'mock_*' -o -name '*.py.bak' -o -name '*.bak' -o -name '*.bak-*' -o -name '*.orig' -o -name 'fix_*.py' \) -delete 2>/dev/null || true

# Ensure the bundle stays production-only.
find release/AICamera.app/Contents/Resources/app -name '*.py' | grep -E '/(test_|.*_test|mock_|fix_|build_).*\.py$' -q && find release/AICamera.app/Contents/Resources/app \( -name 'test_*.py' -o -name '*_test.py' -o -name 'mock_*' -o -name 'fix_*.py' \) -delete 2>/dev/null || true

# Remove .py files from bundle ONLY if a compiled .so file exists for them
find release/AICamera.app/Contents/Resources/app -name "*.py" | while read -r py_file; do
  so_file="${py_file%.py}.so"
  if [ -f "$so_file" ]; then
    base_py=$(basename "$py_file")
    if [ "$base_py" != "bootstrap.py" ] && [ "$base_py" != "client_logic.py" ] && [ "$base_py" != "menubar_app.py" ]; then
      rm -f "$py_file"
    fi
  fi
done

# APFS clone python_env (instant, copy-on-write)
# The trailing slash matters: python_env may be a symlink to a shared env on the
# dev Mac, and without it cp copies the link itself — a bundle that only works on
# the machine that built it.
cp -Rc python_env/ release/AICamera.app/Contents/Resources/app/python_env 2>/dev/null || true

# Managed services stay encrypted on disk; dlc_import_hook decrypts them at import.
# Spawned child processes (multiprocessing "spawn") import their target's module
# before any of their own code runs, so the hook has to be there from interpreter
# start: a copy in the bundle's site-packages plus a .pth that imports it.
SITE_PACKAGES="release/AICamera.app/Contents/Resources/app/python_env/lib/python3.12/site-packages"
cp dlc_import_hook.so "$SITE_PACKAGES/dlc_import_hook.so"
echo "import dlc_import_hook" > "$SITE_PACKAGES/aicamera_dlc_hook.pth"

# Copy bin + db_support
cp -R bin/ release/AICamera.app/Contents/Resources/app/bin
# bin/ on the build Mac holds a failed download (mariadb.tar.gz, an error page)
# and a replaced mariadbd kept as mariadbd.broken; neither is used.
rm -f release/AICamera.app/Contents/Resources/app/bin/*.tar.gz release/AICamera.app/Contents/Resources/app/bin/*.broken
cp -R db_support/ release/AICamera.app/Contents/Resources/app/db_support

# Fix mariadb dylib path — discover the ACTUAL absolute path baked into the
# extension at build time (varies by dev machine/home dir) instead of
# guessing, so the rewrite to a relative @loader_path actually applies.
MARIADB_SO="release/AICamera.app/Contents/Resources/app/python_env/lib/python3.12/site-packages/mariadb/_mariadb.cpython-312-darwin.so"
MARIADB_OLD_PATH=$(otool -L "$MARIADB_SO" 2>/dev/null | grep "libmariadb.3.dylib" | awk '{print $1}')
if [ -n "$MARIADB_OLD_PATH" ]; then
    install_name_tool -change "$MARIADB_OLD_PATH" @loader_path/../../../mariadb/libmariadb.3.dylib "$MARIADB_SO" || true
    echo "  Fixed mariadb dylib path: $MARIADB_OLD_PATH -> @loader_path/../../../mariadb/libmariadb.3.dylib"
else
    echo "  ⚠️  Could not find libmariadb.3.dylib reference in $MARIADB_SO — skipping fix"
fi


# Copy .so files from source tree (already compiled in-place)
# NOTE: exclude rules must come BEFORE the include='*/' rule — rsync applies
# the first matching filter per path, so putting excludes after an include
# that matches directories lets rsync recurse into (and copy) the release/
# folder into itself, ballooning the bundle every rebuild.
rsync -avL --exclude='python_env' --exclude='release' --exclude='build' --exclude='.git' \
  --exclude='/.stale_*' --exclude='backup_*' --exclude='/client_core.so' --exclude='/client.so' \
  --exclude='/dlc_local' --exclude='/managed_services' \
  --include='*.so' --include='*/' --exclude='*' --prune-empty-dirs \
  ./ release/AICamera.app/Contents/Resources/app/ 2>/dev/null || true

# Create thin .py launchers for entry points (they import .so)
for pair in "bootstrap:bootstrap" "client_logic:client_logic" "menubar_app:menubar_app" "updater:updater"; do
  py_name="${pair%%:*}"
  mod_name="${pair##*:}"
  so_path="release/AICamera.app/Contents/Resources/app/${py_name}.so"
  if [ -f "$so_path" ]; then
    cat > "release/AICamera.app/Contents/Resources/app/${py_name}.py" << LAUNCHER
import sys
import ${mod_name}

if __name__ == '__main__':
    ${mod_name}.main()
LAUNCHER
    echo "  Launcher: ${py_name}.py -> ${mod_name}.so"
  fi
done

# Copy Cython .so for secure_env
cp data_service/configs/secure_env.cpython-312-darwin.so release/AICamera.app/Contents/Resources/app/data_service/configs/secure_env.cpython-312-darwin.so 2>/dev/null || true

# Copy app icons
cp MyIcon.icns release/AICamera.app/Contents/Resources/AppIcon.icns 2>/dev/null || true

# "AICamera Startup.app": a bundle around the app's own Python, used only to run the
# startup window. Tk always gives that window's process a Dock tile while it is up;
# a bare interpreter shows there (and in the menu bar) as "python3.12" with Tk's
# default picture, while inside this bundle it is "AICamera" with the app icon from
# its first frame. The executable is a RELATIVE link to python_env, so nothing is
# duplicated and the bundle stays self-contained.
STARTUP_APP="release/AICamera.app/Contents/Helpers/AICamera Startup.app"
mkdir -p "$STARTUP_APP/Contents/MacOS" "$STARTUP_APP/Contents/Resources"
ln -sf "../../../../Resources/app/python_env/bin/python3.12" "$STARTUP_APP/Contents/MacOS/python3.12"
cp MyIcon.icns "$STARTUP_APP/Contents/Resources/AppIcon.icns"
cat << 'PLIST' > "$STARTUP_APP/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>python3.12</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.advice.aicamera.startup</string>
    <key>CFBundleName</key>
    <string>AICamera</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
</dict>
</plist>
PLIST

# Strip comments from all .py files in the bundle
./python_env/bin/python3.12 -c "
import os
app_dir = 'release/AICamera.app/Contents/Resources/app'
for root, dirs, files in os.walk(app_dir):
    for f in files:
        if f.endswith('.py'):
            p = os.path.join(root, f)
            with open(p, 'r', encoding='utf-8', errors='ignore') as file:
                lines = file.readlines()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('#') and not stripped.startswith('#!'):
                    continue
                new_lines.append(line)
            with open(p, 'w', encoding='utf-8') as file:
                file.writelines(new_lines)
"

# Keep only what git accounts for: the rsync above takes the whole working copy, so
# whatever else sat in the build Mac's checkout (a .venv, img/, the .so of modules
# deleted long ago) went into the app too (tools/prune_bundle.py).
./python_env/bin/python3.12 tools/prune_bundle.py release/AICamera.app/Contents/Resources/app

rm -f __compile_cython.py
# Generated for this build only (the bundle has its own _build_info.so). Left in
# the repo, a dev checkout would report this build's version forever.
rm -f _build_info.py _build_info.c _build_info.so _build_info.cpython-*.so

# =====================================================================================
# [5/5] Verify the bundle before calling it a build.
#
# set -e above aborts the moment any step fails, which leaves a half-assembled
# release/ that still looks like an app: a shipped bundle has been found with
# entry-point sources instead of launchers and with comments never stripped —
# code that never went through the steps above. Fail loudly here instead.
# =====================================================================================
echo "[5/5] Verifying bundle..."
APP_DIR="release/AICamera.app/Contents/Resources/app"
VERIFY_FAILED=0
_bad() { echo "  X $1"; VERIFY_FAILED=$((VERIFY_FAILED + 1)); }
_ok()  { echo "  . $1"; }

# --- the pieces without which the app cannot start at all ---
for required in \
    "release/AICamera.app/Contents/MacOS/AICameraLauncher" \
    "$APP_DIR/.loading_gui.py" \
    "$APP_DIR/loading_gui.so" \
    "$APP_DIR/runtime_paths.so" \
    "$APP_DIR/log_redact.so" \
    "$APP_DIR/log_seal.so" \
    "$APP_DIR/legacy_clone.so" \
    "$APP_DIR/frontend_web/assets.bin" \
    "$APP_DIR/frontend_web/assets_vault.so" \
    "$APP_DIR/python_env/lib/python3.12/site-packages/dlc_import_hook.so" \
    "release/AICamera.app/Contents/Helpers/AICamera Startup.app/Contents/MacOS/python3.12" \
    "$APP_DIR/python_env/bin/python3.12" \
    "$APP_DIR/bin/mariadbd" \
    "$APP_DIR/_build_info.so"; do
    if [ -e "$required" ]; then _ok "present: ${required#release/AICamera.app/Contents/}"
    else _bad "MISSING: ${required#release/AICamera.app/Contents/}"; fi
done

# --- nothing may be a link out of the bundle. A dangling absolute symlink is
#     invisible here (it resolves on THIS Mac) and only fails at the store. ---
ESCAPED=$(find "$APP_DIR" -type l 2>/dev/null | while read -r link; do
    target=$(readlink "$link")
    case "$target" in
        (/*) case "$target" in
                 ("$PWD/release/AICamera.app"*) ;;
                 (*) echo "${link#"$APP_DIR/"} -> $target" ;;
             esac ;;
    esac
done | head -5) || true
if [ -n "$ESCAPED" ]; then
    _bad "symlinks pointing outside the bundle (broken on every other Mac):"
    echo "$ESCAPED" | sed 's|^|      |'
else
    _ok "no symlink escapes the bundle"
fi

# --- python_env/bin/db_support must be real folders, not links back to the dev Mac ---
for d in python_env bin db_support; do
    if [ -L "$APP_DIR/$d" ]; then _bad "$d is a symlink ($(readlink "$APP_DIR/$d")) - only works on this Mac"
    else _ok "$d is a real folder"; fi
done

# --- entry points must be the thin launcher, not the whole source file ---
for name in bootstrap client_logic menubar_app updater; do
    so="$APP_DIR/$name.so"
    py="$APP_DIR/$name.py"
    [ -f "$so" ] || continue
    if [ ! -f "$py" ]; then
        _bad "$name.so shipped without its $name.py launcher"
    elif [ "$(wc -l < "$py" | tr -d ' ')" -le 8 ]; then
        _ok "$name.py is a launcher"
    else
        _bad "$name.py is $(wc -l < "$py" | tr -d ' ') lines - full source shipped next to $name.so"
    fi
done

# --- any other .py sitting beside its .so means readable source leaked out.
#     A file of 8 lines or fewer is a launcher, which is meant to be there;
#     frontend_python/main.py always ships as source because the client opens
#     it by path (run_frontend_service), so its .so is never imported. ---
LEAKED=$(find "$APP_DIR" -name '*.so' 2>/dev/null | while read -r so; do
    py="${so%.so}.py"
    [ -f "$py" ] || continue
    case "$py" in
        (*/frontend_python/main.py) continue ;;
    esac
    [ "$(wc -l < "$py" | tr -d ' ')" -le 8 ] && continue
    echo "$py"
done | head -5) || true
if [ -n "$LEAKED" ]; then
    _bad "readable source shipped alongside its .so:"
    echo "$LEAKED" | sed 's|^|      |'
else
    _ok "no source left beside a compiled module"
fi

# --- nothing that belongs to a developer's working copy may ship: backups,
#     logs and secret sources carry credentials and local paths out to every
#     store. A rename like .bak-20260830 slips past a plain *.bak filter. ---
STRAY=$(find "$APP_DIR" -not -path "*/python_env/*" \
    \( -name '*.bak' -o -name '*.bak-*' -o -name '*.orig' -o -name '*.log' \
       -o -name '_secrets.py' -o -name '*.pyx' -o -name '*.c' \
       -o -name '.env*' -o -name '*-backup' -o -name '*.production-backup' \
       -o -name '.stale_*' -o -name '.claude' \) 2>/dev/null | head -5) || true
if [ -n "$STRAY" ]; then
    _bad "developer-only files shipped inside the bundle:"
    echo "$STRAY" | sed 's|^|      |'
else
    _ok "no backup/log/secret-source files in the bundle"
fi

# --- the encrypted-services hook must load in every interpreter the app starts,
#     or each service fails to import as soon as its files are kept encrypted. ---
HOOK_OK=$("$APP_DIR/python_env/bin/python3.12" -c "
import sys
print(any(type(f).__name__ == '_ManagedServiceFinder' for f in sys.meta_path))
" 2>/dev/null) || true
if [ "$HOOK_OK" = "True" ]; then
    _ok "dlc_import_hook active in a fresh bundled interpreter"
else
    _bad "dlc_import_hook NOT active in the bundled python_env (managed services would not import)"
fi

# --- what this Mac's own app recorded: snapshots of real people (in_pass,
#     charge, ...) and the shop's uploaded floor plans. The running app writes
#     them into data_service/static/ of whichever copy it runs from, so a build
#     made on a Mac that has run the client used to carry hundreds of them to
#     every store. ---
RECORDED=$(find "$APP_DIR/data_service/static/logs" "$APP_DIR/data_service/static/images/floorplans" \
    -type f 2>/dev/null | wc -l | tr -d ' ')
if [ "$RECORDED" != "0" ]; then
    _bad "$RECORDED recorded image(s) from this Mac shipped in data_service/static/"
else
    _ok "no recorded people/floor-plan images in the bundle"
fi

# --- the address the app will actually talk to. A dev machine's test admin
#     leaking into a store build is invisible until the store cannot activate. ---
BAKED_HOST=$(cd "$APP_DIR" && ./python_env/bin/python3.12 -c "
import os, sys
sys.path.insert(0, '.')
from data_service.configs import secure_env
secure_env.load_secure_env()
print(os.environ.get('API_SERVER_HOST', ''), os.environ.get('SOCKET_SERVER_HOST', ''))
" 2>/dev/null) || true
EXPECTED_HOST="${AICAMERA_EXPECTED_HOST:-app.advice.th}"
case "$BAKED_HOST" in
    (*"$EXPECTED_HOST"*) _ok "server address is $EXPECTED_HOST" ;;
    ("") _bad "could not read the baked server address" ;;
    (*) _bad "baked server address is NOT $EXPECTED_HOST: $BAKED_HOST" ;;
esac

# --- the version is compiled in, and nothing readable says it ---
BAKED_VERSION=$(cd "$APP_DIR" && ./python_env/bin/python3.12 -c "
import sys; sys.path.insert(0, '.')
from data_service.configs.version_store import baked_version
print(baked_version('.') or '')
" 2>/dev/null) || true
if [ "$BAKED_VERSION" = "$APP_VERSION" ]; then _ok "version $APP_VERSION compiled into _build_info.so"
else _bad "compiled version is '$BAKED_VERSION', expected '$APP_VERSION'"; fi
for plain in VERSION client_version.json manifest.json DELETE_MANIFEST.json custom_tracking.yaml \
             icon.png no_sleep.app run_ai_camera_client.app person.png client_core.so \
             client.so requirements.txt logs bin/mariadb.tar.gz bin/mariadbd.broken; do
    [ -e "$APP_DIR/$plain" ] && _bad "$plain shipped in the bundle (not used by the app; readable or editable by the shop)"
done
# face_service's models (the 343 MB mask model, the face detector and two
# landmarkers) are fetched by face_service itself into the data folder
# (face_service/model_store.py); stores that never run it skip them.
for model in ViT-FaceMask-Finetuned yolov8n-face.pt face_landmarker.task hand_landmarker.task; do
    [ -e "$APP_DIR/$model" ] && _bad "$model shipped in the bundle (face_service downloads it)"
done

# --- the web UI ships sealed: no readable pages or scripts, and the bundle opens ---
for folder in templates static; do
    [ -e "$APP_DIR/frontend_web/$folder" ] && _bad "frontend_web/$folder shipped as plain files (they belong in assets.bin)"
done
UI_PAGES=$(cd "$APP_DIR" && ./python_env/bin/python3.12 -c "
import sys; sys.path.insert(0, 'frontend_web')
import assets_vault
print(len(assets_vault.templates()))
" 2>/dev/null) || UI_PAGES=0
if [ "${UI_PAGES:-0}" -gt 0 ]; then _ok "web UI sealed in assets.bin ($UI_PAGES pages open with the DLC key)"
else _bad "frontend_web/assets.bin does not open with the DLC key"; fi

# --- nothing the repo does not account for (tools/prune_bundle.py ran) ---
if STRAYS=$(./python_env/bin/python3.12 tools/prune_bundle.py "$APP_DIR" --check); then
    _ok "every file comes from git or from this build"
else
    _bad "files in the bundle that git does not account for:"
    echo "$STRAYS"
fi

# --- the comment-strip step is last, so leftover comments mean it never ran ---
UNSTRIPPED=$(find "$APP_DIR" -name '*.py' -exec grep -lE '^[[:space:]]*#([^!]|$)' {} + 2>/dev/null | head -5) || true
if [ -n "$UNSTRIPPED" ]; then
    _bad "comments still present - the strip step never ran on:"
    echo "$UNSTRIPPED" | sed 's|^|      |'
else
    _ok "comments stripped from every .py"
fi

if [ "$VERIFY_FAILED" -ne 0 ]; then
    echo ""
    echo "BUILD FAILED VERIFICATION ($VERIFY_FAILED problem(s)) - release/AICamera.app is NOT shippable."
    echo "Do not distribute it. Rerun the build and fix the first error it printed."
    exit 1
fi

echo "✅ Build Complete! Your App is ready at: release/AICamera.app"