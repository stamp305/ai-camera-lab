"""ตั้งค่าไซเรน 5 ขั้น (เว็บ): http://127.0.0.1:8767

1 เสียบไฟ  2 ตั้งค่าในแอป Smart Life  3 สแกน QR (ได้ key)  4 หาไซเรนในวงแลน  5 เลือกเสียงแจ้งเตือน (ค่าเริ่มต้นเสียงที่ 2) และระดับเสียง กดให้ดัง

key เก็บที่ setup/monitor_siren.json ในเครื่องนี้เท่านั้น (รูปแบบเดียวกับ log/monitor_siren.json ของสาขา)
หน้าเว็บไม่เคยได้รับ key ตาราง/ขั้นต่างๆ แสดงแค่ 4 ตัวท้ายของ Device ID (gwId เต็มอยู่ในรายละเอียดการค้นหา ขั้น 4)
"""
import base64
import io
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qrcode
from tuya_sharing import LoginControl, Manager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor_service import siren  # noqa: E402 — the same siren.py the branches would run

HERE = os.path.dirname(os.path.abspath(__file__))
# lab: the JSON stays next to this file (gitignored, 0600), as in AICamera_siren/setup
STORE = os.environ.get("SIREN_STORE") or os.path.join(HERE, "monitor_siren.json")
PORT = 8767
CLIENT_ID, SCHEMA = "HA_3y9q4ak7g4ephrvke", "haauthorize"
USER_CODE = os.environ.get("TUYA_USER_CODE", "")   # never in the code: set it in the environment
SIREN_CATEGORY = "sgbj"            # Tuya "siren alarm"
LISTEN_SECONDS = 8.0
QR_SECONDS = 300                   # Tuya's own QR lifetime (measured: expired between 288 and 303 s)
QR_EXPIRED = "E0020002"            # what Tuya answers once the QR has expired

_login = {"token": None, "user_code": None, "started": 0.0}
_lock = threading.Lock()
_ring_lock = threading.Lock()
_find_lock = threading.Lock()
_ips = {}                          # device id -> ip found on this network


def short(device_id):
    return "…" + device_id[-4:]


def config():
    try:
        return json.load(open(STORE))
    except (OSError, ValueError):
        return {}


def saved():
    return config().get("devices", [])


def sound():
    s = config().get("sound", siren.SOUND)
    return s if s in siren.SOUNDS else siren.SOUND


def write_config(cfg):
    with open(STORE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    os.chmod(STORE, 0o600)


# -- step 2: which network this machine is on ------------------------------------------------
def net():
    ip = siren._own_ip()
    return {"ip": ip, "lan": ip.rsplit(".", 1)[0] + ".x" if ip.count(".") == 3 and not ip.startswith("0.") else None}


# -- step 3: QR login -> keys ----------------------------------------------------------------
def qr_start():
    """Any valid-looking User Code gets a QR; whichever account scans it is the one logged in."""
    user_code = USER_CODE
    if not user_code:
        return {"ok": False, "why": "ยังไม่ได้ตั้ง TUYA_USER_CODE ในเครื่องนี้"}
    r = LoginControl().qr_code(CLIENT_ID, SCHEMA, user_code)
    if not r.get("success"):
        return {"ok": False, "why": r.get("msg") or "ขอ QR ไม่ได้"}
    token = r["result"]["qrcode"]
    with _lock:
        _login.update(token=token, user_code=user_code, started=time.time())
    img = qrcode.make(f"tuyaSmart--qrLogin?token={token}", border=2, box_size=8)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"ok": True, "seconds": QR_SECONDS,
            "png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}


def qr_status():
    with _lock:
        token, user_code, started = _login["token"], _login["user_code"], _login["started"]
    if not token:
        return {"state": "idle"}
    if time.time() - started > QR_SECONDS + 30:   # fallback if Tuya never says so
        return {"state": "expired"}
    ok, info = LoginControl().login_result(token, CLIENT_ID, user_code)
    if not ok:
        return {"state": "expired" if info.get("code") == QR_EXPIRED else "waiting"}
    with _lock:
        _login["token"] = None
    m = Manager(CLIENT_ID, user_code, info["terminal_id"], info["endpoint"],
                {"t": info.get("t", 0), "uid": info.get("uid"), "expire_time": info["expire_time"],
                 "access_token": info["access_token"], "refresh_token": info["refresh_token"]})
    m.update_device_cache()
    found = [{"id": d.id, "key": d.local_key, "name": d.name, "category": d.category}
             for d in m.device_map.values() if d.local_key]
    sirens = [d for d in found if d["category"] == SIREN_CATEGORY]
    write_config({"devices": [{"id": d["id"], "key": d["key"], "name": d["name"]} for d in sirens],
                  "volume": "mute", "seconds": 2, "sound": sound()})
    _ips.clear()
    return {"state": "done",
            "devices": [{"id": short(d["id"]), "name": d["name"], "siren": d["category"] == SIREN_CATEGORY}
                        for d in found]}


# -- step 4: find them on this network -------------------------------------------------------
def scan():
    devices = saved()
    if not devices:
        return {"ok": False, "why": "ยังไม่มีไซเรนจากขั้น 3", "rows": []}
    heard = {}
    t0 = time.time()
    found = siren.find([d["id"] for d in devices], seconds=LISTEN_SECONDS, heard=heard)
    took = round(time.time() - t0, 1)
    _ips.update(found)
    ours = {d["id"] for d in devices}
    rows = []
    for d in devices:
        ip = found.get(d["id"])
        row = {"id": short(d["id"]), "name": d.get("name", ""), "ip": ip, "ok": False}
        if ip:
            link = None
            try:
                link = siren.Link(ip, d["key"])
                dps = link.status()
                row.update(ok=True, volume=dps.get(siren.DP_VOLUME), seconds=dps.get(siren.DP_SECONDS),
                           sound=dps.get(siren.DP_SOUND), ringing=dps.get(siren.DP_RINGING), dps=dps)
            except (OSError, siren.SirenError, ValueError) as e:
                row["why"] = str(e)
            finally:
                if link is not None:
                    link.close()
        else:
            row["why"] = "ไม่เจอในวงนี้"
        rows.append(row)
    own = siren._own_ip()
    heard_rows = [{"id": short(gw), "ip": h["ip"], "from": h["from"], "version": h["version"],
                   "port": h["port"], "ours": gw in ours,
                   "info": {k: v for k, v in h["info"].items() if k != "ip"}}
                  for gw, h in heard.items()]
    return {"ok": any(r["ok"] for r in rows), "own_ip": own, "rows": rows,
            "search": {"seconds": took, "limit": LISTEN_SECONDS, "listen": [6666, 6667, 7000],
                       "asked": f"{own.rsplit('.', 1)[0]}.1–254" if own.count(".") == 3 else None,
                       "heard": heard_rows}}


# -- step 5: alert sound / volume + ring -----------------------------------------------------
def settings(choice):
    """Keep the alert sound / volume in the config; the siren gets them on its next ring (the
    branch's alert or the test button). Nothing is sent now: setting the sound rings the siren."""
    cfg = config()
    if not cfg.get("devices"):
        return {"ok": False, "why": "ยังไม่มีไซเรนจากขั้น 3"}
    if "sound" in choice:
        if choice["sound"] not in siren.SOUNDS:
            return {"ok": False, "why": "ไม่มีเสียงนี้"}
        cfg["sound"] = choice["sound"]
    if "volume" in choice:
        if choice["volume"] not in siren.VOLUMES:
            return {"ok": False, "why": "ไม่มีระดับเสียงนี้"}
        cfg["volume"] = choice["volume"]
    if "seconds" in choice:
        try:
            seconds = int(choice["seconds"])
        except ValueError:
            seconds = 0
        if not 1 <= seconds <= 380:
            return {"ok": False, "why": "เวลาดังต้องอยู่ระหว่าง 1–380 วินาที"}
        cfg["seconds"] = seconds
    write_config(cfg)
    return {"ok": True, **chosen()}


def chosen():
    """What the next alert rings with."""
    return {"sound": sound(), "volume": volume(), "seconds": config().get("seconds", 2)}


def live():
    """What each siren is set to right now (a change made in the Smart Life app shows up here).
    Read only: a status query never rings. Skipped while a ring/test is talking to the siren."""
    devices = saved()
    if not devices:
        return {"ok": False, "why": "ยังไม่มีไซเรนจากขั้น 3", "chosen": chosen()}
    missing = [d["id"] for d in devices if d["id"] not in _ips]
    if missing:
        if not _find_lock.acquire(blocking=False):
            return {"ok": False, "busy": True, "why": "กำลังหาไซเรนในวง", "chosen": chosen()}
        try:
            _ips.update(siren.find(missing, seconds=LISTEN_SECONDS))
        finally:
            _find_lock.release()
    if _ring_lock.locked():                    # a ring never waits for a read; the read steps aside
        return {"ok": False, "busy": True, "why": "กำลังสั่งไซเรนอยู่", "chosen": chosen()}
    rows = [_read_one(d) for d in devices]
    return {"ok": any(r["ok"] for r in rows), "at": time.strftime("%H:%M:%S"), "sirens": rows, "chosen": chosen()}


def _read_one(d):
    ip = _ips.get(d["id"])
    row = {"id": short(d["id"]), "name": d.get("name", ""), "ip": ip, "ok": False}
    if not ip:
        row["why"] = "ไม่เจอในวงนี้"
    else:
        link = None
        try:
            link = siren.Link(ip, d["key"])
            dps = link.status()
            row.update(ok=True, sound=dps.get(siren.DP_SOUND), volume=dps.get(siren.DP_VOLUME),
                       seconds=dps.get(siren.DP_SECONDS), ringing=dps.get(siren.DP_RINGING))
        except (OSError, siren.SirenError, ValueError) as e:
            _ips.pop(d["id"], None)            # look for it afresh next time
            row["why"] = str(e) or type(e).__name__
        finally:
            if link is not None:
                link.close()
    return row


def volume():
    v = config().get("volume", "mute")
    return v if v in siren.VOLUMES else "mute"


def ring(audible):
    """audible: the chosen alert sound, low, 1 s, then volume/length back as they were (the sound
    stays the chosen one); otherwise only a handshake + status, nothing changed."""
    if not _ring_lock.acquire(blocking=False):
        return {"ok": False, "why": "กำลังสั่งอยู่"}
    try:
        results = []
        tone = sound()
        for d in saved():
            ip = _ips.get(d["id"]) or siren.find([d["id"]], seconds=LISTEN_SECONDS).get(d["id"])
            if not ip:
                results.append({"id": short(d["id"]), "ok": False, "why": "ไม่เจอในวงนี้"})
                continue
            _ips[d["id"]] = ip
            link = None
            try:
                link = siren.Link(ip, d["key"])
                before = link.status()
                if audible:
                    link.set({siren.DP_VOLUME: "low", siren.DP_SECONDS: 1})   # before the sound: it rings
                    link.set({siren.DP_SOUND: tone, siren.DP_RINGING: True})
                    time.sleep(1.2)
                    link.set({siren.DP_RINGING: False})
                    link.set({siren.DP_VOLUME: before.get(siren.DP_VOLUME, "mute"),
                              siren.DP_SECONDS: before.get(siren.DP_SECONDS, 2)})
                now = link.status().get(siren.DP_SOUND)
                taken = now == tone or not audible
                results.append({"id": short(d["id"]), "name": d.get("name", ""), "ok": taken, "ip": ip,
                                "sound": now, **({} if taken else {"why": f"ไซเรนไม่รับเสียง {tone} (เป็น {now})"})})
            except (OSError, siren.SirenError, ValueError) as e:
                _ips.pop(d["id"], None)
                results.append({"id": short(d["id"]), "ok": False, "why": str(e)})
            finally:
                if link is not None:
                    link.close()
        return {"ok": any(r["ok"] for r in results), "sirens": results}
    finally:
        _ring_lock.release()


ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    def _trusted(self, unsafe=False):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and origin.split("//", 1)[-1] not in ALLOWED_HOSTS:
            return False
        return not unsafe or self.headers.get("X-Siren-Setup") == "1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._trusted():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, {"net": net(),
                                    "saved": [{"id": short(d["id"]), "name": d.get("name", "")} for d in saved()],
                                    "sound": sound(), "sounds": list(siren.SOUNDS),
                                    "volume": volume(), "volumes": list(siren.VOLUMES)})
        if self.path == "/api/live":
            try:
                return self._send(200, live())
            except Exception as e:  # noqa: BLE001 — show the reason on the page
                return self._send(200, {"ok": False, "why": str(e)})
        if self.path == "/api/qr/status":
            try:
                return self._send(200, qr_status())
            except Exception as e:  # noqa: BLE001 — show the reason on the page
                return self._send(200, {"state": "error", "why": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._trusted(unsafe=True):
            return self._send(403, {"error": "forbidden"})
        try:
            if self.path == "/api/qr":
                return self._send(200, qr_start())
            if self.path == "/api/scan":
                return self._send(200, scan())
            if self.path == "/api/ring":
                return self._send(200, ring(audible=True))
            if self.path == "/api/check":
                return self._send(200, ring(audible=False))
            if self.path == "/api/settings":
                body = self._body()
                return self._send(200, settings({k: str(body[k]) for k in ("sound", "volume", "seconds") if k in body}))
        except Exception as e:  # noqa: BLE001
            return self._send(200, {"ok": False, "why": str(e)})
        self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"เปิด http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
