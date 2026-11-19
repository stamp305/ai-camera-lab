"""DEMO rows for Alert & Log until the admin stores real alerts.

Everything here is made up: branch codes, places, camera IPs and pictures. The pictures
are drawn placeholders (SVG), never real shop footage. The message text follows the admin's
LINE alert format (camera_alert_service/alerts.py) so the page reads like the real thing.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from html import escape

BRANCHES = [
    {"code": "A002", "province": "ปราจีนบุรี", "place": "ที่ตั้งโรงหนังเก่า", "region": "ตะวันออก", "net": "192.168.20"},
    {"code": "A015", "province": "ชลบุรี", "place": "ตลาดหนองมน", "region": "ตะวันออก", "net": "192.168.1"},
    {"code": "A021", "province": "ระยอง", "place": "หน้าห้างแหลมทอง", "region": "ตะวันออก", "net": "192.168.10"},
    {"code": "U024", "province": "อุดรธานี", "place": "ตรงข้ามโรงแรมเจริญโฮเต็ล", "region": "อีสาน", "net": "192.168.20"},
    {"code": "U031", "province": "ขอนแก่น", "place": "ถนนศรีจันทร์", "region": "อีสาน", "net": "192.168.1"},
    {"code": "U040", "province": "นครราชสีมา", "place": "ใกล้ลานย่าโม", "region": "อีสาน", "net": "192.168.20"},
    {"code": "U052", "province": "อุบลราชธานี", "place": "ตลาดวารินชำราบ", "region": "อีสาน", "net": "192.168.10"},
    {"code": "N011", "province": "เชียงใหม่", "place": "หน้ากาดหลวง", "region": "เหนือ", "net": "192.168.1"},
    {"code": "N019", "province": "เชียงราย", "place": "หอนาฬิกา", "region": "เหนือ", "net": "192.168.20"},
    {"code": "N027", "province": "พิษณุโลก", "place": "ถนนบรมไตรโลกนาถ", "region": "เหนือ", "net": "192.168.10"},
    {"code": "C031", "province": "นครปฐม", "place": "ใกล้องค์พระปฐมเจดีย์", "region": "กลาง", "net": "192.168.10"},
    {"code": "C044", "province": "พระนครศรีอยุธยา", "place": "ตลาดหัวรอ", "region": "กลาง", "net": "192.168.1"},
    {"code": "C050", "province": "สุพรรณบุรี", "place": "ถนนมาลัยแมน", "region": "กลาง", "net": "192.168.20"},
    {"code": "S008", "province": "สงขลา", "place": "ถนนนิพัทธ์อุทิศ 3", "region": "ใต้", "net": "192.168.20"},
    {"code": "S014", "province": "ภูเก็ต", "place": "ย่านเมืองเก่า", "region": "ใต้", "net": "192.168.1"},
    {"code": "S022", "province": "สุราษฎร์ธานี", "place": "ท่าเรือบ้านดอน", "region": "ใต้", "net": "192.168.10"},
    {"code": "B017", "province": "กรุงเทพมหานคร", "place": "ลาดพร้าว 71", "region": "กรุงเทพและปริมณฑล", "net": "192.168.1"},
    {"code": "B023", "province": "นนทบุรี", "place": "แยกแคราย", "region": "กรุงเทพและปริมณฑล", "net": "192.168.20"},
    {"code": "B029", "province": "สมุทรปราการ", "place": "ปากน้ำ", "region": "กรุงเทพและปริมณฑล", "net": "192.168.10"},
]

# What the branch's camera checks report (admin camera_alert_service/alerts.py), without the
# bracketed notes: angle / swap from the angle check, the rest from the daily check.
CAMERA_KINDS = {
    "angle": "มุมกล้องเปลี่ยนไปจากเดิม",
    "swap": "กล้องสลับตำแหน่งกัน",
    "rtsp_down": "เชื่อมต่อกล้องไม่ได้",
    "no_frames": "ไม่ได้รับภาพจากกล้อง",
    "black": "ภาพดำ / ไม่มีภาพ",
    "degraded": "คุณภาพภาพลดลง",
}


def demo_branches() -> list[dict]:
    """DEMO branches for the CCTV filters: listed only, they carry no video."""
    return [{"client_id": f"DEMO-{b['code']}", "name": f"{b['code']} {b['place']}", "province": b["province"],
             "region": b["region"], "live": True, "demo": True} for b in BRANCHES]


def is_demo_branch(client_id: str) -> bool:
    return client_id.startswith("DEMO-")


def demo_cameras(client_id: str) -> list[dict]:
    n = 6 + int(hashlib.sha256(client_id.encode()).hexdigest()[:2], 16) % 11      # 6..16 channels
    return [{"camera": 1, "nvr": "NVR", "channel": ch, "active": ch != 4} for ch in range(1, n + 1)]


# One test branch that is the real lab Mac mini: its rows jump to real NVR footage.
# "__REAL__" is replaced by the first online branch's id when the API answers.
LAB = {"code": "LAB1", "province": "กรุงเทพมหานคร", "place": "สาขาทดสอบ (Mac mini)", "region": "กรุงเทพและปริมณฑล",
       "net": "192.168.2", "client": "__REAL__"}
ALL_BRANCHES = BRANCHES + [LAB]


def _label(b: dict) -> str:
    return f"{b['code']} จ.{b['province']} ({b['place']})"


def _rows() -> dict[str, list[dict]]:
    now = datetime.now().replace(second=0, microsecond=0)
    intrusion, camera = [], []
    for i in range(120):
        b = BRANCHES[(i * 7 + 3) % len(BRANCHES)]
        ch = (3, 7, 5, 12, 2, 9, 1, 14)[i % 8]
        day = now - timedelta(days=i // 2, hours=0)
        at = day.replace(hour=(21 + (i * 5) % 10) % 24, minute=(i * 13) % 60)
        if at > now:
            at -= timedelta(days=1)
        ip = f"{b['net']}.{240 + i % 2}"
        msg = f"🚨 พบคนในร้านหลังเวลาปิด: {_label(b)}\n- กล้อง {ch} ({ip}) เวลา {at:%Y-%m-%d %H:%M}"
        intrusion.append({"id": f"in{i:03d}", "kind": "intrusion", "at": at.strftime("%Y-%m-%d %H:%M"),
                          "branch": _label(b), "code": b["code"], "region": b["region"], "province": b["province"],
                          "place": b["place"], "camera": f"กล้อง {ch} ({ip})", "detail": "พบคนในร้านหลังเวลาปิด",
                          "message": msg, "images": [f"in{i:03d}-0"], "client_id": f"DEMO-{b['code']}", "channel": ch})
    kinds = ["angle", "degraded", "angle", "black", "swap", "rtsp_down", "degraded", "no_frames", "angle"]
    for i in range(90):
        b = BRANCHES[(i * 5) % len(BRANCHES)]
        ch = (3, 1, 7, 4, 6, 11, 2, 9)[i % 8]
        kind = kinds[i % len(kinds)]
        at = (now - timedelta(days=i // 2)).replace(hour=7 if i % 2 == 0 else 13, minute=0)
        if at > now:
            at -= timedelta(days=1)
        ip = f"{b['net']}.{241 - i % 2}"
        cam = f"กล้อง {ch} ({ip})"
        if kind == "swap":
            other = f"กล้อง {ch + 1} ({ip})"
            msg = f"[SWAP] แจ้งเตือน AI Camera\n{_label(b)}:\n🔄 {cam} สลับกับ {other}"
        elif kind == "angle":
            msg = f"[ALERT] แจ้งเตือน AI Camera\n{_label(b)}:\n⚠️ {cam} มุมกล้องเปลี่ยนไปจากเดิม"
        else:
            msg = f"⚠️ ตรวจกล้องประจำวัน AI Camera: {_label(b)}\n- {cam}: {CAMERA_KINDS[kind]}"
        images = ([f"cq{i:03d}-before", f"cq{i:03d}-now"] if kind in ("angle", "swap")
                  else ([] if kind in ("rtsp_down", "no_frames") else [f"cq{i:03d}-now"]))
        camera.append({"id": f"cq{i:03d}", "kind": kind, "at": at.strftime("%Y-%m-%d %H:%M"), "branch": _label(b),
                       "code": b["code"], "region": b["region"], "province": b["province"], "place": b["place"],
                       "camera": cam, "detail": CAMERA_KINDS[kind], "message": msg, "images": images,
                       "client_id": f"DEMO-{b['code']}", "channel": ch})
    _lab_rows(now, intrusion, camera)
    for group in (intrusion, camera):
        group.sort(key=lambda r: r["at"], reverse=True)
    return {"intrusion": intrusion, "camera": camera}


# Real frames grabbed from the lab NVR (tools/capture_frames.py): data/alert_images/, 0600,
# gitignored. They show real people, so they are served only to signed-in admins (audited).
IMG_DIR = Path(__file__).resolve().parent.parent / "data" / "alert_images"
_manifest_cache: tuple[float, dict] = (0.0, {})


def _manifest() -> dict:
    global _manifest_cache
    path = IMG_DIR / "manifest.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if mtime != _manifest_cache[0]:
        _manifest_cache = (mtime, json.loads(path.read_text("utf-8")))
    return _manifest_cache[1]


def _lab_rows(now: datetime, intrusion: list, camera: list) -> None:
    """Rows for the test branch. With captured frames: their fixed times, so the picture and
    "ดูคลิป" show the same moment; without: times relative to now."""
    b = LAB
    lab = _manifest().get("lab")
    if lab:
        ip = f"{b['net']}.240"
        for r in lab:
            ch, kind, when = r["channel"], r["kind"], r["at"]
            cam = f"กล้อง {ch} ({ip})"
            parts = list(r.get("images", {}))
            images = [f"{r['id']}-{p}" for p in parts]
            if kind == "intrusion":
                msg = f"🚨 พบคนในร้านหลังเวลาปิด: {_label(b)}\n- {cam} เวลา {when}"
                intrusion.append({"id": r["id"], "kind": kind, "at": when, "branch": _label(b), "code": b["code"],
                                  "region": b["region"], "province": b["province"], "place": b["place"], "camera": cam,
                                  "detail": "พบคนในร้านหลังเวลาปิด", "message": msg, "images": images,
                                  "client_id": b["client"], "channel": ch})
            else:
                msg = (f"[ALERT] แจ้งเตือน AI Camera\n{_label(b)}:\n⚠️ {cam} มุมกล้องเปลี่ยนไปจากเดิม" if kind == "angle"
                       else f"⚠️ ตรวจกล้องประจำวัน AI Camera: {_label(b)}\n- {cam}: {CAMERA_KINDS[kind]}")
                camera.append({"id": r["id"], "kind": kind, "at": when, "branch": _label(b), "code": b["code"],
                               "region": b["region"], "province": b["province"], "place": b["place"], "camera": cam,
                               "detail": CAMERA_KINDS[kind], "message": msg, "images": images,
                               "client_id": b["client"], "channel": ch})
        return
    def at_(delta_h: float, hh: int | None = None, mm: int = 0) -> datetime:
        t = now - timedelta(hours=delta_h)
        return t.replace(hour=hh, minute=mm) if hh is not None else t.replace(minute=(t.minute // 5) * 5)
    for i, (ch, when) in enumerate(((3, at_(1.2)), (7, at_(3.4)), (1, at_(24, 15, 40)))):
        ip = f"{b['net']}.240"
        intrusion.append({"id": f"lb{i:03d}", "kind": "intrusion", "at": when.strftime("%Y-%m-%d %H:%M"), "branch": _label(b),
                          "code": b["code"], "region": b["region"], "province": b["province"], "place": b["place"],
                          "camera": f"กล้อง {ch} ({ip})", "detail": "พบคนในร้านหลังเวลาปิด",
                          "message": f"🚨 พบคนในร้านหลังเวลาปิด: {_label(b)}\n- กล้อง {ch} ({ip}) เวลา {when:%Y-%m-%d %H:%M}",
                          "images": [f"lb{i:03d}-0"], "client_id": b["client"], "channel": ch})
    seven = now.replace(hour=7, minute=0) if now.hour >= 8 else (now - timedelta(days=1)).replace(hour=7, minute=0)
    for i, (ch, kind, when) in enumerate(((3, "angle", seven), (7, "degraded", at_(24, 13, 0)), (5, "rtsp_down", seven)), start=3):
        ip = f"{b['net']}.240"
        cam = f"กล้อง {ch} ({ip})"
        msg = (f"[ALERT] แจ้งเตือน AI Camera\n{_label(b)}:\n⚠️ {cam} มุมกล้องเปลี่ยนไปจากเดิม" if kind == "angle"
               else f"⚠️ ตรวจกล้องประจำวัน AI Camera: {_label(b)}\n- {cam}: {CAMERA_KINDS[kind]}")
        images = [f"lb{i:03d}-before", f"lb{i:03d}-now"] if kind == "angle" else ([] if kind == "rtsp_down" else [f"lb{i:03d}-now"])
        camera.append({"id": f"lb{i:03d}", "kind": kind, "at": when.strftime("%Y-%m-%d %H:%M"), "branch": _label(b),
                       "code": b["code"], "region": b["region"], "province": b["province"], "place": b["place"],
                       "camera": cam, "detail": CAMERA_KINDS[kind], "message": msg, "images": images,
                       "client_id": b["client"], "channel": ch})


def query(kind: str, regions: list[str], branches: list[str], q: str, offset: int, limit: int) -> dict:
    rows = [r for r in _rows().get(kind, [])
            if (not regions or r["region"] in regions) and (not branches or r["code"] in branches)
            and (not q or q in f"{r['branch']} {r['camera']} {r['detail']}".lower())]
    return {"rows": rows[offset:offset + limit], "total": len(rows),
            "next": offset + limit if offset + limit < len(rows) else None}


def rows(kind: str) -> list[dict]:
    return _rows().get(kind, [])


def find(row_id: str) -> dict | None:
    for group in _rows().values():
        for r in group:
            if r["id"] == row_id:
                return r
    return None


def image_file(image_id: str) -> Path | None:
    """The real frame for a picture id, if one was captured."""
    row_id, _, part = image_id.rpartition("-")
    row = find(row_id)
    if not row or image_id not in row["images"]:
        return None
    m = _manifest()
    name = None
    lab = next((r for r in m.get("lab", []) if r["id"] == row_id), None)
    if lab:
        name = lab.get("images", {}).get(part)
    elif m.get("pool") and row["kind"] != "black":
        pool = m["pool"]
        keys = sorted(pool, key=int)
        ch = str(row.get("channel"))
        if ch not in pool:                                   # a channel with no frames: borrow one
            ch = keys[int(hashlib.sha256(row_id.encode()).hexdigest()[:6], 16) % len(keys)]
        frames = pool[ch]
        name = {"intrusion": frames.get("night"), "degraded": frames.get("blur")}.get(row["kind"])
        if row["kind"] in ("angle", "swap"):
            name = frames.get("before") if part == "before" else frames.get("day")
    if not name or not re.match(r"^[a-z0-9_]+\.jpg$", name):
        return None
    path = IMG_DIR / name
    return path if path.is_file() else None


def image_svg(image_id: str) -> str | None:
    """A drawn placeholder frame for one demo picture id (row id + -0/-before/-now)."""
    row_id, _, part = image_id.rpartition("-")
    row = find(row_id)
    if not row or image_id not in row["images"]:
        return None
    seed = int(hashlib.sha256(image_id.encode()).hexdigest()[:8], 16)
    shift = 70 if part == "now" and row["kind"] in ("angle", "swap") else 0
    dark = row["kind"] == "black" and part == "now"
    blur = row["kind"] == "degraded" and part == "now"
    W, H = 960, 540
    shelves = "".join(
        f'<rect x="{80 + k * 200 + shift}" y="{150 + (seed >> k) % 40}" width="150" height="300" rx="6" fill="#3b4250"/>'
        f'<rect x="{90 + k * 200 + shift}" y="{170 + (seed >> k) % 40}" width="130" height="10" fill="#5b6475"/>'
        f'<rect x="{90 + k * 200 + shift}" y="{250 + (seed >> k) % 40}" width="130" height="10" fill="#5b6475"/>'
        for k in range(4))
    person = ""
    if row["kind"] == "intrusion":
        px = 300 + seed % 380
        person = (f'<g fill="#9aa3b2"><circle cx="{px}" cy="260" r="26"/><rect x="{px - 30}" y="290" width="60" height="120" rx="20"/></g>'
                  f'<rect x="{px - 48}" y="222" width="96" height="206" fill="none" stroke="#ff3b30" stroke-width="4"/>'
                  f'<text x="{px - 48}" y="214" fill="#ff3b30" font-size="18" font-family="Arial">person 0.91</text>')
    tag = {"before": "BEFORE (ภาพอ้างอิง)", "now": "NOW (ภาพล่าสุด)"}.get(part, "")
    body = "" if dark else f'<g{" filter=\"url(#b)\"" if blur else ""}>{shelves}{person}</g>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">'
            '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#20242c"/>'
            '<stop offset="1" stop-color="#2c313b"/></linearGradient><filter id="b"><feGaussianBlur stdDeviation="9"/></filter></defs>'
            f'<rect width="{W}" height="{H}" fill="{"#050505" if dark else "url(#g)"}"/>'
            f'<rect y="440" width="{W}" height="100" fill="#262a32"/>{body}'
            f'<text x="20" y="34" fill="#fff" font-size="20" font-family="Arial">{escape(row["camera"])}  {escape(row["at"])}</text>'
            f'<text x="20" y="{H - 20}" fill="#ffd166" font-size="18" font-family="Arial">{escape(tag)}</text>'
            f'<text x="{W - 20}" y="{H - 20}" text-anchor="end" fill="rgba(255,255,255,.55)" font-size="18" font-family="Arial">'
            'ภาพตัวอย่าง (DEMO) — ไม่ใช่ภาพจริง</text></svg>')
