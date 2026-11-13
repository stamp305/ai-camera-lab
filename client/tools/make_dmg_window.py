"""Give the .dmg window a laid-out Finder view — window size, icon positions and a
background — without ever mounting the image.

The usual recipe (attach a read-write image, drive Finder over AppleScript, detach)
cannot run on this dev Mac: DiskArbitration refuses to mount any image here
("no mountable file systems"), which is also why build_dmg.sh uses `makehybrid`.
Finder reads those settings from a plain `.DS_Store` file in the volume, so this
writes that file straight into the staging folder and lets `makehybrid` carry it
in. Nothing is mounted at any point.

Usage:  python3 tools/make_dmg_window.py <staging_dir> <volume_name> <app_name>
"""

import datetime
import os
import plistlib
import subprocess
import sys
import tempfile

from ds_store import DSStore
from mac_alias import (
    ALIAS_FIXED_DISK,
    ALIAS_HFS_VOLUME_SIGNATURE,
    ALIAS_KIND_FILE,
    Alias,
    TargetInfo,
    VolumeInfo,
)
from PIL import Image, ImageDraw, ImageFont

# Finder lays the window out in these coordinates, so the background art and the
# icon positions below have to agree on them. Measured off the design reference.
WIN_W, WIN_H = 660, 400
ICON_SIZE = 160
ICON_Y = 178
APP_X = 190
DROP_X = 470
ARROW_Y = 185               # the icons' artwork sits a little above their centre

FG = (51, 51, 51)           # #333333, the arrow in the reference
MUTED = (140, 140, 146)
BG = (250, 250, 250)        # #FAFAFA

BACKGROUND_REL = ".background/background.tiff"

SF = "/System/Library/Fonts/SFNS.ttf"
HELVETICA = "/System/Library/Fonts/Helvetica.ttc"


def _font(size, style="Regular"):
    """A system font at `size`.

    SFNS is a variable font whose first axis is Width, so setting axes
    positionally makes text narrow instead of bold. Select by style name.
    """
    for path in (SF, HELVETICA):
        if not os.path.exists(path):
            continue
        try:
            f = ImageFont.truetype(path, size)
            try:
                f.set_variation_by_name(style)
            except Exception:
                pass  # static face: it simply has the one weight
            return f
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_width):
    """Greedy word wrap, so a longer hint never runs off the window edges."""
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if line and draw.textlength(trial, font=font) > max_width:
            lines.append(line)
            line = w
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def _draw_background(img, scale):
    """The window art: a bold arrow between the two icons, and a small hint."""
    d = ImageDraw.Draw(img)

    def xy(x, y):
        return (x * scale, y * scale)

    def stroke(a, b, w):
        # A line with round caps: PIL has no cap style, so end each in a disc.
        d.line([xy(*a), xy(*b)], fill=FG, width=round(w * scale))
        r = w * scale / 2
        for px, py in (a, b):
            d.ellipse([px * scale - r, py * scale - r, px * scale + r, py * scale + r], fill=FG)

    # Arrow, centred between the two icon slots: 42 x 34 with a 7px stroke.
    mid_x = (APP_X + DROP_X) / 2
    left, right, y = mid_x - 17.5, mid_x + 17.5, ARROW_Y
    stroke((left, y), (right, y), 7)
    stroke((right - 13.5, y - 13.5), (right, y), 7)
    stroke((right - 13.5, y + 13.5), (right, y), 7)

    # The app is not signed with a Developer ID, so the first open is refused.
    # Saying so here is the difference between "broken" and "one extra click".
    hint = ("First launch: if macOS blocks the app, open System Settings > "
            "Privacy & Security and choose Open Anyway.")
    f = _font(11 * scale, "Regular")
    lines = _wrap(d, hint, f, (WIN_W - 80) * scale)
    y = (WIN_H - 34 - 14 * (len(lines) - 1)) * scale
    for line in lines:
        w = d.textlength(line, font=f)
        d.text(((WIN_W * scale - w) / 2, y), line, font=f, fill=MUTED)
        y += 14 * scale


def build_background(staging_dir):
    """Write .background/background.tiff with a 1x and a 2x representation."""
    bg_dir = os.path.join(staging_dir, ".background")
    os.makedirs(bg_dir, exist_ok=True)
    out = os.path.join(bg_dir, "background.tiff")

    pngs = []
    tmp = tempfile.mkdtemp()
    for scale in (1, 2):
        # Drawn at 4x and scaled down: PIL does not anti-alias lines or discs.
        big = Image.new("RGB", (WIN_W * scale * 4, WIN_H * scale * 4), BG)
        _draw_background(big, scale * 4)
        img = big.resize((WIN_W * scale, WIN_H * scale), Image.LANCZOS)
        p = os.path.join(tmp, f"bg@{scale}x.png")
        img.save(p)
        pngs.append(p)

    # tiffutil packs both representations into one file, so the background stays
    # sharp on a Retina screen. A plain PNG would be resampled and look soft.
    try:
        subprocess.run(["tiffutil", "-cathidpicheck", pngs[0], pngs[1], "-out", out],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        Image.open(pngs[0]).save(out)
        print("  ! tiffutil unavailable — background is 1x only")
    return out


def _volume_alias(volume_name, rel_path):
    """An alias pointing at rel_path inside the mounted volume.

    Built by hand: mac_alias.Alias.for_file() would have to stat the real file,
    and the volume only exists on the machine that opens the .dmg.

    What macOS needs to resolve it (tested against CFURLCreateBookmarkDataFromAliasRecord
    on a mounted volume): the volume NAME, and the target's POSIX path RELATIVE TO
    THAT VOLUME ("/.background/background.tiff"). Dates and CNIDs may be anything.
    An absolute "/Volumes/<name>/..." path fails with -35 (volume not found) —
    which is why the background never showed in the earlier builds.
    """
    # to_bytes() writes both dates into the record, so they cannot be None. The
    # value itself is never checked by Finder when it resolves the picture.
    stamp = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    mount_point = f"/Volumes/{volume_name}"
    folder, filename = os.path.split(rel_path)
    return Alias(
        volume=VolumeInfo(
            name=volume_name,
            creation_date=stamp,
            # A v2 alias packs fs_type as 2 bytes (b"H+") and fs_id as 2 zero
            # bytes — the same shape mac_alias produces for a real HFS+ volume.
            fs_type=ALIAS_HFS_VOLUME_SIGNATURE,
            disk_type=ALIAS_FIXED_DISK,
            attribute_flags=0,
            fs_id=b"\0\0",
            posix_path=mount_point,
        ),
        target=TargetInfo(
            kind=ALIAS_KIND_FILE,
            filename=filename,
            folder_cnid=0,
            cnid=0,
            creation_date=stamp,
            creator_code=b"\0\0\0\0",
            type_code=b"\0\0\0\0",
            levels_from=-1,
            levels_to=-1,
            folder_name=os.path.basename(folder) or None,
            carbon_path=f"{volume_name}:{rel_path.replace('/', ':')}",
            posix_path=f"/{rel_path}".encode("utf-8"),
        ),
    )


def write_ds_store(staging_dir, volume_name, app_name, with_background=True):
    ds_path = os.path.join(staging_dir, ".DS_Store")
    if os.path.exists(ds_path):
        os.remove(ds_path)

    window = {
        "ShowStatusBar": False,
        "ShowTabView": False,
        "ShowToolbar": False,
        "ShowPathbar": False,
        "ShowSidebar": False,
        "ContainerShowSidebar": False,
        "SidebarWidth": 0,
        "WindowBounds": f"{{{{200, 120}}, {{{WIN_W}, {WIN_H}}}}}",
    }

    icon_view = {
        "viewOptionsVersion": 1,
        # 2 = picture (needs a working alias), 1 = plain colour.
        "backgroundType": 2 if with_background else 1,
        "backgroundColorRed": BG[0] / 255,
        "backgroundColorGreen": BG[1] / 255,
        "backgroundColorBlue": BG[2] / 255,
        "gridOffsetX": 0.0,
        "gridOffsetY": 0.0,
        "gridSpacing": 100.0,
        "arrangeBy": "none",  # anything else and Finder re-flows our positions
        "showIconPreview": True,
        "showItemInfo": False,
        "labelOnBottom": True,
        "textSize": 12.0,
        "iconSize": float(ICON_SIZE),
        "scrollPositionX": 0.0,
        "scrollPositionY": 0.0,
    }
    if with_background:
        icon_view["backgroundImageAlias"] = _volume_alias(
            volume_name, BACKGROUND_REL).to_bytes()

    with DSStore.open(ds_path, "w+") as d:
        # The dict itself, NOT plistlib.dumps(...): ds_store's PlistCodec
        # serialises it. Handing it bytes encodes them a second time, and the
        # stored plist's root becomes <data> instead of <dict>. Finder then
        # sends dictionary messages to an NSData and aborts — a Finder crash on
        # every double-click, while hdiutil mounts the image happily.
        d["."]["bwsp"] = window
        d["."]["icvp"] = icon_view
        d["."]["vSrn"] = ("long", 1)
        d[app_name]["Iloc"] = (APP_X, ICON_Y)
        d["Applications"]["Iloc"] = (DROP_X, ICON_Y)
        # Nothing should invite a double-click on the background folder.
        d[".background"]["Iloc"] = (WIN_W + 400, WIN_H + 400)
    return ds_path


def verify(ds_path, app_name, with_background=True):
    """Read back what was written.

    This checks the decoded value is a dict. The earlier version ran
    plistlib.loads() on what it read, which undid the very double-encoding that
    was crashing Finder, so the build passed while shipping a broken file.
    """
    problems = []
    with DSStore.open(ds_path, "r") as d:
        for key in ("bwsp", "icvp"):
            value = d["."][key]
            if not isinstance(value, dict):
                problems.append(
                    f"{key} stored as {type(value).__name__}, not dict "
                    "(double-encoded — Finder will crash)")

        icvp = d["."]["icvp"]
        if isinstance(icvp, dict):
            if icvp.get("iconSize") != float(ICON_SIZE):
                problems.append("iconSize did not round-trip")
            raw = icvp.get("backgroundImageAlias") if with_background else None
            if with_background and not raw:
                problems.append("background alias is empty")
            elif raw:
                # The one mistake that silently loses the picture: a path that is
                # not relative to the volume. See _volume_alias().
                a = Alias.from_bytes(raw)
                path = a.target.posix_path
                path = path.decode() if isinstance(path, bytes) else path
                if path != "/" + BACKGROUND_REL:
                    problems.append(f"background alias path is {path!r}, "
                                    f"not volume-relative /{BACKGROUND_REL}")
        bwsp = d["."]["bwsp"]
        if isinstance(bwsp, dict) and f"{WIN_W}, {WIN_H}" not in bwsp.get("WindowBounds", ""):
            problems.append(f"window bounds look wrong: {bwsp.get('WindowBounds')}")

        for name in (app_name, "Applications"):
            if not isinstance(d[name]["Iloc"], tuple):
                problems.append(f"Iloc for {name!r} is not a position")
    return problems


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    staging_dir, volume_name, app_name = sys.argv[1:4]
    with_background = os.environ.get("DMG_BACKGROUND", "1") != "0"
    if not os.path.isdir(os.path.join(staging_dir, app_name)):
        print(f"  X {app_name} not found in {staging_dir}")
        return 1

    if with_background:
        bg = build_background(staging_dir)
        print(f"  . background: {os.path.relpath(bg, staging_dir)} "
              f"({os.path.getsize(bg) // 1024} KB)")
    else:
        print("  . background: off (plain white)")

    ds_path = write_ds_store(staging_dir, volume_name, app_name, with_background)
    problems = verify(ds_path, app_name, with_background)
    if problems:
        for p in problems:
            print(f"  X {p}")
        return 1
    print(f"  . .DS_Store: window {WIN_W}x{WIN_H}, icons {ICON_SIZE}px at "
          f"({APP_X},{ICON_Y}) and ({DROP_X},{ICON_Y})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
