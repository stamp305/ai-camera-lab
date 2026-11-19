"""Draw each camera's Outdoor Area on its picture, to check by eye what the after-hours watch ignores.

Run on the branch machine, with the client running, from the client's folder:
    git clone:  /opt/miniconda3/envs/ai-camera/bin/python tools/show_outdoor_area.py
    the app:    the app has no tools/ folder; copy this file anywhere and run it with
                /Applications/AICamera.app/Contents/Resources/app/python_env/bin/python3.12
    ... show_outdoor_area.py 2 9          only channels 2 and 9 (any NVR)
    ... show_outdoor_area.py --no-open    write the pictures, do not open the folder
    ... show_outdoor_area.py --port 8000  the data service's port, when it is not found by itself

What it draws (from GET /cameras/outside-area and /cameras/image on the local data service),
on the picture the admin server was sent, at its own size (see picture()):
- red, solid:      the area in use: anyone whose feet are in it is not counted at night
- yellow, dashed:  Jev's box that is NOT used, with the reason (no outside in view, or a small
                   picture with a box Jev was not sure of)
and on top: has_outdoor, the box confidence, the picture size, why, and when it was asked.

Only reads, only from this machine (127.0.0.1). The pictures go to a folder under
~/Desktop/outdoor_area/<date-time>/ — they show the shop, so delete them when done.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import cv2
import numpy as np

HAS_OUTDOOR_MIN = 0.5      # the same rule as data_service/utils/camera_outside_area.py
SMALL = (1280, 720)
SMALL_BOX_MIN = 0.3


def data_service_port() -> int:
    """The data service's port from the shop's service table (it can be changed on the System
    page); 8000 when it cannot be read (the app, or not run from the client's folder)."""
    try:
        sys.path.insert(0, os.getcwd())
        import port_store
        return int(port_store.get_port("data_service", 8000))
    except Exception:
        return 8000


def get(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:   # loopback only
        return r.read()


def picture(base: str, ip: str, channel: int) -> bytes:
    """The picture the admin server was sent: the kept one at the NVR's own size (a client from
    d65a825 on; an older one answers with its 1280-wide page picture). Only when nothing is kept,
    a new one from the NVR, which can be its blurrier snapshot or time out."""
    try:
        return get(f"{base}/image/{ip}/{channel}?cached=true&original=true")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return get(f"{base}/image/{ip}/{channel}")


def why_not(row: dict) -> str:
    if row.get("has_outdoor") is not None and row["has_outdoor"] < HAS_OUTDOOR_MIN:
        return f"not used: has_outdoor {row['has_outdoor']} < {HAS_OUTDOOR_MIN}"
    pic = row.get("picture")
    if not pic or pic[0] < SMALL[0] or pic[1] < SMALL[1]:
        return f"not used: small picture, box {row.get('box_confidence')} < {SMALL_BOX_MIN}"
    return "not used"


def dashed_rect(img, p1, p2, colour, thick=3, dash=18):
    (x1, y1), (x2, y2) = p1, p2
    for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
        length = int(np.hypot(b[0] - a[0], b[1] - a[1]))
        for s in range(0, length, dash * 2):
            e = min(s + dash, length)
            pa = (int(a[0] + (b[0] - a[0]) * s / max(length, 1)), int(a[1] + (b[1] - a[1]) * s / max(length, 1)))
            pb = (int(a[0] + (b[0] - a[0]) * e / max(length, 1)), int(a[1] + (b[1] - a[1]) * e / max(length, 1)))
            cv2.line(img, pa, pb, colour, thick)


def label(img, lines):
    """On a black band added under the picture, so the text hides nothing (feet are at the bottom)."""
    scale = img.shape[1] / 1280 * 0.8
    step = int(36 * img.shape[1] / 1280) + 4
    band = np.zeros((step * len(lines) + 12, img.shape[1], 3), np.uint8)
    y = step
    for text in lines:
        cv2.putText(band, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2)
        y += step
    return np.vstack([img, band])


def draw(row: dict, jpeg: bytes):
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    if img.shape[1] < 1280:   # a small picture (352x288 at some NVRs): enlarge it so lines and text read
        f = 1280 / img.shape[1]
        img = cv2.resize(img, (1280, int(img.shape[0] * f)), interpolation=cv2.INTER_CUBIC)
    h, w = img.shape[:2]
    used = row.get("polygons") or []
    for poly in used:
        pts = np.array([[int(x * w), int(y * h)] for x, y in poly], np.int32)
        overlay = img.copy()
        cv2.fillPoly(overlay, [pts], (0, 0, 255))
        img = cv2.addWeighted(overlay, 0.18, img, 0.82, 0)
        cv2.polylines(img, [pts], True, (0, 0, 255), 4)
    box = row.get("box")
    if box and not used:
        dashed_rect(img, (int(box["left"] * w), int(box["top"] * h)),
                    (int(box["right"] * w), int(box["bottom"] * h)), (0, 220, 255))
    state = "IN USE (red): people standing here are ignored at night" if used else (
        why_not(row) + " (yellow)" if box else "no box kept (asked before boxes were kept: press the switch)")
    return label(img, [
        f"{row['ip']} ch{row['channel']:02d}   has_outdoor {row.get('has_outdoor')}   "
        f"box {row.get('box_confidence')}   picture {row.get('picture')}",
        state,
        f"{row.get('state')} ({row.get('reason')}), answered {row.get('answered_at')}, {row.get('model')}",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("channels", nargs="*", type=int, help="only these channels")
    ap.add_argument("--no-open", action="store_true", help="do not open the folder")
    ap.add_argument("--port", type=int, help="the data service's port (default: from the service table, else 8000)")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port or data_service_port()}/cameras"
    try:
        rows = json.loads(get(f"{base}/outside-area"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("This client has no Outdoor Area yet: update the client first.")
        else:
            print(f"The client answered {e.code}.")
        return 1
    except Exception as e:
        print(f"Could not reach the client's data service at {base} ({type(e).__name__}). "
              "Is the client running? If its port was changed, give it with --port.")
        return 1
    rows = [r for r in rows if not args.channels or r["channel"] in args.channels]
    if not rows:
        print("No camera has an Outdoor Area answer yet.")
        return 0
    out = os.path.expanduser(f"~/Desktop/outdoor_area/{datetime.datetime.now():%Y%m%d_%H%M%S}")
    os.makedirs(out, exist_ok=True)
    for r in rows:
        name = f"{r['ip'].replace('.', '_')}_ch{r['channel']:02d}"
        used = "IN USE" if r.get("polygons") else ("box not used" if r.get("box") else "-")
        print(f"{r['ip']} ch{r['channel']:02d}: has_outdoor {r.get('has_outdoor')}, box {r.get('box_confidence')}, "
              f"{used}, {r.get('state')}")
        try:
            img = draw(r, picture(base, r["ip"], r["channel"]))
        except Exception as e:
            print(f"   no picture ({type(e).__name__})")
            continue
        if img is not None:
            cv2.imwrite(os.path.join(out, name + ".jpg"), img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    print(f"\nPictures: {out}\n(they show the shop: delete the folder when done)")
    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", out], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
