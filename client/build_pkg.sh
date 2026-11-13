#!/bin/bash
# Packages release/AICamera.app into a distributable .pkg installer that
# installs to /Applications when double-clicked. Uses pkgbuild/productbuild
# (not hdiutil/DiskImages), so it sidesteps the "no mountable file systems"
# hdiutil bug seen on this Dev Mac when building a .dmg.
# Run this AFTER build_mac_app.sh has produced release/AICamera.app.
set -e

APP_PATH="release/AICamera.app"
PKG_NAME="AICamera-Installer"
COMPONENT_PKG="release/${PKG_NAME}-component.pkg"
FINAL_PKG="release/${PKG_NAME}.pkg"
IDENTIFIER="com.advice.aicamera.installer"
# Installer metadata follows the app's own number: "V.1.0.0" in VERSION -> 1.0.0.
VERSION="$(sed -e 's/^V\.//' VERSION | tr -d '[:space:]')"
INSTALL_LOCATION="/Applications"

if [ ! -d "$APP_PATH" ]; then
    echo "❌ $APP_PATH not found — run build_mac_app.sh first."
    exit 1
fi

echo "[1/4] Staging payload..."
STAGING_DIR=$(mktemp -d)
mkdir -p "$STAGING_DIR/payload"
cp -Rc "$APP_PATH" "$STAGING_DIR/payload/"

# Packages reserved for features nothing calls yet (~1.45 GB). Stripped from
# the installer payload only — release/AICamera.app itself is untouched, so
# a local run of the app still has them. postinstall pip-installs the same
# pinned versions back in on the customer's machine, in the background, so
# the installer itself stays small without the app ever running without them
# should one of those features ship before the branch reinstalls.
DEFERRED_PACKAGES="tensorflow==2.20.0 jax==0.4.31 jaxlib==0.4.31 litellm==1.81.10 openai==2.20.0 slack_bolt==1.27.0 slack_sdk==3.40.0 strands-agents==1.25.0"
STAGED_SITE_PACKAGES="$STAGING_DIR/payload/AICamera.app/Contents/Resources/app/python_env/lib/python3.12/site-packages"
echo "  Stripping deferred packages from installer payload (kept in $APP_PATH itself):"
for pkg_pin in $DEFERRED_PACKAGES; do
    pkg_name="${pkg_pin%%==*}"
    # Distribution name -> import/dist-info name: dashes become underscores.
    # strands-agents is the one exception — it installs as a folder literally
    # named 'strands', not 'strands_agents'.
    pkg_dir_name="${pkg_name//-/_}"
    if [ "$pkg_dir_name" = "strands_agents" ]; then
        pkg_dir_name="strands"
    fi
    for match in "$STAGED_SITE_PACKAGES/$pkg_dir_name" "$STAGED_SITE_PACKAGES/${pkg_name//-/_}"-*.dist-info; do
        if [ -e "$match" ]; then
            echo "    - $(basename "$match")"
            rm -rf "$match"
        fi
    done
done

echo "[2/4] Writing postinstall script..."
# The app writes into its own bundle at runtime (.loading_gui.py, .activate_requested,
# .restart_required, VERSION, and every DLC file the updater pulls). pkgbuild installs as
# root, so under /Applications all of those raise PermissionError and the app dies on
# launch. Hand the bundle to the user who is actually logged in at the console.
mkdir -p "$STAGING_DIR/scripts"
cat > "$STAGING_DIR/scripts/postinstall" <<POSTINSTALL
#!/bin/bash
APP="\$2/AICamera.app"
CONSOLE_USER=\$(stat -f "%Su" /dev/console)
if [ -d "\$APP" ] && [ -n "\$CONSOLE_USER" ] && [ "\$CONSOLE_USER" != "root" ]; then
    chown -R "\$CONSOLE_USER" "\$APP"
    chmod -R u+w "\$APP"
fi

# Deferred packages: fetched in the background so this installer step (and
# the visible install progress bar) finishes immediately. Not needed for the
# app to run today — only for features that don't exist yet — so a slow or
# failed network fetch here must never block or fail the install itself.
# python3.12, not the pip/pip3 launcher scripts: those scripts' shebang line
# hardcodes the absolute path of the dev machine this venv was built on, so
# they crash with "bad interpreter" on every machine that isn't that one.
# Running pip as a module (python3.12 -m pip) sidesteps the broken shebang
# entirely — verified against the actual shipped payload before this went
# out (dry-run install resolved and matched exactly).
PY="\$APP/Contents/Resources/app/python_env/bin/python3.12"
LOG_DIR="/Users/\$CONSOLE_USER/Library/Application Support/AICamera/logs"
# As the console user, never as root: a root-owned App Support/AICamera makes the
# app die on its first line (PermissionError creating db/) before it can log a thing.
sudo -u "\$CONSOLE_USER" mkdir -p "\$LOG_DIR" 2>/dev/null
chown -R "\$CONSOLE_USER" "/Users/\$CONSOLE_USER/Library/Application Support/AICamera" 2>/dev/null
if [ -x "\$PY" ]; then
    sudo -u "\$CONSOLE_USER" nohup "\$PY" -m pip install --quiet $DEFERRED_PACKAGES \\
        >> "\$LOG_DIR/deferred_packages_install.log" 2>&1 &
    disown
fi

exit 0
POSTINSTALL
chmod +x "$STAGING_DIR/scripts/postinstall"

echo "[3/4] Building component package..."
rm -f "$COMPONENT_PKG"
pkgbuild --root "$STAGING_DIR/payload" \
         --scripts "$STAGING_DIR/scripts" \
         --install-location "$INSTALL_LOCATION" \
         --identifier "$IDENTIFIER" \
         --version "$VERSION" \
         "$COMPONENT_PKG"

echo "[4/4] Building final product installer..."
rm -f "$FINAL_PKG"
productbuild --package "$COMPONENT_PKG" "$FINAL_PKG"

rm -f "$COMPONENT_PKG"
rm -rf "$STAGING_DIR"

echo "✅ PKG ready at: $FINAL_PKG"
