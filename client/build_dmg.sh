#!/bin/bash
# Packages release/AICamera.app into a distributable .dmg with a drag-to-Applications
# layout. Run this AFTER build_mac_app.sh has produced release/AICamera.app.
#
# NOTE: on this Dev Mac (macOS 26) hdiutil cannot MOUNT a disk image — DiskArbitration
# returns "no mountable file systems" — so the usual `hdiutil create -srcfolder` and
# create/attach/copy/detach recipes both fail. `hdiutil makehybrid -hfs` builds the HFS
# filesystem in userspace without ever mounting anything, so we use that, then compress
# it with `hdiutil convert`.
#
# The same limitation rules out the usual way of arranging the window (mount, drive
# Finder over AppleScript, unmount). tools/make_dmg_window.py writes the .DS_Store and
# the background picture straight into the staging folder instead, and makehybrid
# carries them in. Without that file Finder has no saved window size, icon size or
# icon positions, and the .dmg opens as a bare list — which is what ours used to do.
set -e

APP_PATH="release/AICamera.app"
APP_NAME="AICamera.app"
VERSION="$(sed -e 's/^V\.//' VERSION | tr -d '[:space:]')"
DMG_NAME="AICamera-${VERSION}"
DMG_PATH="release/${DMG_NAME}.dmg"
RAW_DMG="release/.${DMG_NAME}-raw.dmg"
STAGING_DIR="release/.dmg-staging"
VOL_NAME="AICamera"

# Writing the .DS_Store needs ds_store + mac_alias + pillow, which must NOT go into
# python_env: that whole tree is copied into the shipped bundle. Keep them in a
# cached throwaway venv instead.
DMG_TOOLS="${AICAM_DMG_TOOLS:-${TMPDIR:-/tmp}/aicamera-dmgtools}"

if [ ! -d "$APP_PATH" ]; then
    echo "❌ $APP_PATH not found — run build_mac_app.sh first."
    exit 1
fi

cleanup() { rm -rf "$STAGING_DIR" "$RAW_DMG"; }
trap cleanup EXIT

echo "[1/4] Preparing window tools..."
if [ ! -x "$DMG_TOOLS/bin/python" ]; then
    ./python_env/bin/python3.12 -m venv "$DMG_TOOLS"
fi
if ! "$DMG_TOOLS/bin/python" -c "import ds_store, mac_alias, PIL" 2>/dev/null; then
    "$DMG_TOOLS/bin/pip" install --quiet ds_store mac_alias pillow
fi
echo "  . $DMG_TOOLS"

echo "[2/4] Staging (clone-copy, so it costs no extra disk)..."
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"
cp -Rc "$APP_PATH" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"

# The mounted disk's icon: a .VolumeIcon.icns at the root plus the "has custom
# icon" Finder flag on the root folder. makehybrid carries both into the image
# (checked on a mounted image: the volume root shows the flag).
cp MyIcon.icns "$STAGING_DIR/.VolumeIcon.icns"
if command -v SetFile >/dev/null 2>&1; then
    SetFile -c icnC "$STAGING_DIR/.VolumeIcon.icns"
    SetFile -a C "$STAGING_DIR"
else
    # Without the Xcode tools: write the folder's FinderInfo by hand, frFlags
    # (bytes 8-9) = 0x0400 kHasCustomIcon.
    xattr -wx com.apple.FinderInfo \
        "0000000000000000040000000000000000000000000000000000000000000000" "$STAGING_DIR"
fi
# No README file: the Gatekeeper instructions live on the window background now,
# so the window holds only the app and the Applications folder to drop it on.
# The background (the arrow) is found by Finder through an alias record that this
# build has to construct by hand, since it cannot mount the image to let Finder
# write one. That alias used to carry an absolute /Volumes/... path and never
# resolved; make_dmg_window.py now writes the volume-relative path macOS expects
# and refuses to build otherwise. DMG_BACKGROUND=0 still turns the picture off.
"$DMG_TOOLS/bin/python" tools/make_dmg_window.py \
    "$STAGING_DIR" "$VOL_NAME" "$APP_NAME"

echo "[3/4] Building HFS+ image..."
rm -f "$RAW_DMG"
hdiutil makehybrid -hfs -hfs-volume-name "$VOL_NAME" -o "$RAW_DMG" "$STAGING_DIR"

# makehybrid silently drops nothing, but a .dmg whose .DS_Store did not make it in
# looks fine to every command here and only misbehaves on the user's screen. HFS+
# stores names as UTF-16BE, so look for them that way.
echo "  Verifying the window files made it into the image..."
"$DMG_TOOLS/bin/python" - "$RAW_DMG" <<'PY'
import sys
raw = open(sys.argv[1], 'rb').read()
missing = [n for n in ('.DS_Store', '.background', 'background.tiff', '.VolumeIcon.icns')
           if n.encode('utf-16-be') not in raw]
if missing:
    print("  X missing from the image: " + ", ".join(missing))
    sys.exit(1)
print("  . .DS_Store and the background are in the image")
PY

echo "[4/4] Compressing to final .dmg (may take a while for a multi-GB bundle)..."
rm -f "$DMG_PATH"
hdiutil convert "$RAW_DMG" -format UDZO -imagekey zlib-level=9 -o "$DMG_PATH"

# The .dmg file's own icon (what Downloads shows). It lives in the file's extended
# attributes: it survives AirDrop, USB and Finder copies, not a browser download
# or scp — once mounted, the disk icon above shows either way.
./python_env/bin/python3.12 - "$DMG_PATH" <<'PY'
import sys, AppKit
image = AppKit.NSImage.alloc().initWithContentsOfFile_("MyIcon.icns")
ok = AppKit.NSWorkspace.sharedWorkspace().setIcon_forFile_options_(image, sys.argv[1], 0)
print("  . .dmg file icon set" if ok else "  ! could not set the .dmg file icon")
PY

echo "✅ DMG ready at: $DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"
