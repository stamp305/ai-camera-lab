"""Live and playback camera video for the admin, sent on demand to the media server.

    admin  --live_start {sid, camera, channel, subtype, mode, transport, ...}-->  this branch

    transport "socket" (default on the admin side): no port is opened anywhere new.
      this branch: ffmpeg  NVR (RTSP, LAN only)  -> H.264 MPEG-TS on stdout
                   --live_chunk {sid, b} over the SAME Socket.IO link (TLS, 443)--> admin
      admin -> MediaMTX (loopback) -> LL-HLS for the viewer (every read checked by the admin)
    transport "webrtc":
      this branch: ffmpeg --WHIP--> MediaMTX (admin side); WebRTC media is DTLS-SRTP
      (needs UDP to the media server)

Secure by design / PDPA:
  * The NVR login never leaves this machine. The admin names a camera (camera_master.id)
    and a channel; the RTSP URL is built here from this branch's own database, and it is
    handed to ffmpeg through a 0600 file in a 0700 folder (ffconcat), not on the command
    line, so `ps` does not show it. Nothing logs it; ffmpeg's errors are scrubbed.
  * Video goes only to the media host this branch already trusts: the host and scheme of
    the admin link the live_start came in on, whatever a message names.
  * Nothing is recorded here or there: this only relays. Each stream stops at its
    deadline (<= 30 min, pushed back by live_extend when another viewer joins a shared
    stream), on live_stop, on disconnect, and when too many are asked for.
  * Playback reads what the NVR already keeps; it never widens retention.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

# NVR and shop-uplink budget: a 4x4 grid of sub-streams (~0.5 Mbps each), but at most
# MAX_MAIN full pictures (1x1 view and playback, ~2-4 Mbps each) at a time.
MAX_STREAMS = int(os.getenv("AICAM_LIVE_MAX_STREAMS", "16"))
MAX_MAIN = int(os.getenv("AICAM_LIVE_MAX_MAIN", "2"))
MAX_PB_GRID = int(os.getenv("AICAM_LIVE_MAX_PB_GRID", "4"))     # 2x2 playback decodes 2.5K each
MAX_SECONDS = 30 * 60
MAX_PLAYBACK_SPAN = timedelta(hours=24)
NVR_HTTP_PORT = 80
RTSP_PORT = 554
_SID_RE = re.compile(r"^[0-9a-f]{32}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9]{16,79}$")
_SCRUB_RE = re.compile(r"(rtsp|srt|https?)://\S+", re.I)
_TIME_FMT = "%Y-%m-%dT%H:%M:%S"

_lock = threading.Lock()
_streams: dict[str, dict] = {}
_codec_cache: dict[tuple, str] = {}
_ended: dict[str, dict] = {}       # last few ended streams (scrubbed), for status
_watchdog_started = False


_usable: dict[tuple, tuple[bool, bool]] = {}


def _ffmpeg_caps(path: str) -> tuple[bool, bool]:
    """(usable, has_whip) for this ffmpeg. Usable = it starts and has the Mac's H.264 encoder
    (all the socket transport needs); WHIP (FFmpeg 8+) only matters for the webrtc transport.
    An ffmpeg missing a library (the one once bundled in the app) is not usable. Remembered
    per (path, file time), so an upgrade is seen again."""
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return False, False
    if key not in _usable:
        usable = whip = False
        try:
            enc = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
            usable = enc.returncode == 0 and "h264_videotoolbox" in enc.stdout
            if usable:
                mux = subprocess.run([path, "-hide_banner", "-muxers"], capture_output=True, text=True, timeout=10)
                whip = mux.returncode == 0 and bool(re.search(r"^\s*E\s+whip\b", mux.stdout, re.M))
        except Exception:
            usable = whip = False
        _usable[key] = (usable, whip)
    return _usable[key]


def _ffmpeg_usable(path: str) -> bool:
    return _ffmpeg_caps(path)[0]


def _tool(name: str) -> Optional[str]:
    env = os.getenv(f"AICAM_{name.upper()}")
    seen = []
    for p in (env, shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if p and p not in seen and os.path.isfile(p) and os.access(p, os.X_OK):
            seen.append(p)
            if name != "ffmpeg" or _ffmpeg_usable(p):
                return p
    return None


def _scrub(text: str) -> str:
    return _SCRUB_RE.sub(r"\1://***", text or "")[-300:]


# ---- cameras, from this branch's own database -------------------------------------------

def _db():
    from data_service.db.session import get_db
    return get_db()


# Zones each analytics service draws on a channel (normalised 0..1 points), shown on the
# portal's live view. Read from the services' own /polygons on loopback; only the shape,
# colour and a short label leave this machine (no IP).
ZONE_SERVICES = (("in_pass_service", "In-Pass", 8004), ("hotzone_service", "Hot Zone", 8008),
                 ("rate_service", "Rate", 8006), ("face_service", "Face", 8009))
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_LABEL_BAD = re.compile(r"[\x00-\x1f<>]")
_zones_cache: list = [0.0, {}]
ZONES_TTL = 30


def _zone_label(svc: str, z: dict) -> str:
    """The same words the branch's own Display page writes on each zone."""
    kind = str(z.get("type") or "")
    if svc == "in_pass_service":
        d = str(z.get("direction") or "")
        return ("Walk In" if kind == "in" else "Walk Pass") + (f" ({d})" if d in ("up", "down", "left", "right") else "")
    if svc == "hotzone_service":
        name = _LABEL_BAD.sub("", str(z.get("name") or "")).strip()[:40]
        try:
            dur = int(float(z.get("duration") or 0))
        except (TypeError, ValueError):
            dur = 0
        return f"{name}{f' ({dur}s)' if 0 < dur < 100000 else ''}".strip()
    if svc == "rate_service":
        return "Employee" if kind == "employee" else "Interest"
    if svc == "face_service":
        return "Attendance"
    return ""


def _zones_by_channel(ip_to_cam: dict) -> dict:
    now = time.monotonic()
    if now < _zones_cache[0]:
        return _zones_cache[1]
    import json as _json
    import urllib.request
    try:
        import port_store
    except Exception:
        port_store = None
    out: dict = {}
    missed = False
    for svc, title, default in ZONE_SERVICES:
        try:
            port = port_store.get_port(svc, default) if port_store else default
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/polygons", timeout=2) as r:
                rows = _json.loads(r.read(512 * 1024) or b"[]")
        except Exception:
            missed = True                   # not installed, or still starting: ask again soon
            continue
        for z in rows if isinstance(rows, list) else []:
            if not isinstance(z, dict):
                continue
            cam = ip_to_cam.get(str(z.get("ip") or ""))
            try:
                ch = int(z.get("channel"))
                pts = [[round(min(max(float(p["x"]), 0.0), 1.0), 4), round(min(max(float(p["y"]), 0.0), 1.0), 4)]
                       for p in (z.get("points") or [])[:64]]
            except (TypeError, ValueError, KeyError):
                continue
            if cam is None or len(pts) < 2:
                continue
            color = str(z.get("color") or "")
            out.setdefault((cam, ch), []).append({
                "label": _zone_label(svc, z), "color": color if _COLOR_RE.match(color) else "#f57c00",
                "points": pts})
    _zones_cache[:] = [now + (5 if missed else ZONES_TTL), out]
    return out


def list_cameras() -> list[dict]:
    """Every channel of every NVR this branch has: channel 1 up to the NVR's own channel
    count (past it is the NVR's screen, not a camera). `active` = the shop also uses it
    for analytics; `zones` = what its analytics services draw on it. No IP, no login: only ids,
    names and zone shapes leave this machine."""
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, ip, name, channel_count FROM camera_master ORDER BY id")
        nvrs = cur.fetchall()
        cur.execute("SELECT camera_master_id, channel, status FROM camera_channel")
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    # rows left without an NVR (camera_master_id NULL) belong to no camera: skip them
    used = {(int(r["camera_master_id"]), int(r["channel"])): int(r.get("status") or 0) == 1
            for r in rows if r.get("camera_master_id") is not None and r.get("channel") is not None}
    ip_to_cam: dict = {}
    for m in nvrs:
        ip_to_cam.setdefault(str(m.get("ip") or ""), int(m["id"]))
    try:
        zones = _zones_by_channel(ip_to_cam)
    except Exception:
        zones = {}
    out = []
    for m in nvrs:
        cam = int(m["id"])
        count = int(m.get("channel_count") or 0)
        channels = range(1, count + 1) if count else sorted(ch for (c, ch) in used if c == cam)
        for ch in channels:
            out.append({"camera": cam, "nvr": m.get("name") or "", "channel": int(ch), "active": used.get((cam, int(ch)), False),
                        "zones": zones.get((cam, int(ch)), [])})
    return out


def _nvr(camera: int, channel: int) -> dict:
    """The NVR row for a camera/channel the branch has (any channel within the NVR's count)."""
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT m.ip, m.username, m.password, m.channel_count, "
                    "(SELECT COUNT(*) FROM camera_channel ch WHERE ch.camera_master_id = m.id AND ch.channel = ?) AS added "
                    "FROM camera_master m WHERE m.id = ?", (channel, camera))
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    if not row:
        raise LiveError("unknown_camera")
    count = int(row.get("channel_count") or 0)
    if not ((count and 1 <= channel <= count) or int(row.get("added") or 0)):
        raise LiveError("unknown_camera")
    return row


def _rtsp_url(camera: int, channel: int, subtype: int, mode: str,
              start: Optional[datetime], end: Optional[datetime]) -> str:
    from data_service.configs.crypto_util import decrypt_password
    row = _nvr(camera, channel)
    auth = f"{quote(row['username'] or '', safe='')}:{quote(decrypt_password(row['password'] or ''), safe='')}"
    base = f"rtsp://{auth}@{row['ip']}:{RTSP_PORT}/cam"
    if mode == "playback":
        fmt = "%Y_%m_%d_%H_%M_%S"
        return (f"{base}/playback?channel={channel}&subtype={subtype}"
                f"&starttime={start.strftime(fmt)}&endtime={end.strftime(fmt)}")
    return f"{base}/realmonitor?channel={channel}&subtype={subtype}"


class LiveError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


ZONE_TYPES = ("IT", "MONO")      # the camera areas of one store, as the reports name them (BRANCH_TYPE)


def zone_of(nvr_name: str) -> str:
    """IT / MONO for an NVR named so (or starting with it and a space), else "" — the rule the
    reports use to send each area as its own branch."""
    name = str(nvr_name or "").strip().lower()
    for zone in ZONE_TYPES:
        if name == zone.lower() or name.startswith(zone.lower() + " "):
            return zone
    return ""


def store_zones() -> list[str]:
    """The areas this shop has an NVR for, e.g. ["IT", "MONO"]; empty when none is named so."""
    try:
        conn = _db()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT DISTINCT name FROM camera_master")
            found = {zone_of(row.get("name")) for row in cur.fetchall()}
            cur.close()
        finally:
            conn.close()
    except Exception:
        return []
    return [zone for zone in ZONE_TYPES if zone in found]


def store_info() -> dict:
    """Code (store.id), name, region and province of this shop, for the portal's filters and LAN check."""
    try:
        conn = _db()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT id, name, region, province FROM store ORDER BY id LIMIT 1")
            row = cur.fetchone() or {}
            cur.close()
        finally:
            conn.close()
    except Exception:
        return {}
    region = (row.get("region") or "").strip()
    if region.startswith("ภาค"):
        region = region[len("ภาค"):]
    return {"code": str(row.get("id") or "").strip()[:100], "name": (row.get("name") or "").strip()[:120], "region": region.replace("ฯ", "")[:60],
            "province": (row.get("province") or "").strip()[:60], "zones": store_zones()}


_days_cache: dict[tuple, tuple[float, list]] = {}
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _find_files(camera: int, channel: int, start: str, end: str) -> list[tuple[str, str]]:
    """(StartTime, EndTime) of every recording file the NVR holds for this channel between
    start and end ("YYYY-MM-DD HH:MM:SS"). One mediaFileFind session (HTTP digest, LAN only;
    the login stays here)."""
    import requests
    from requests.auth import HTTPDigestAuth
    from data_service.configs.crypto_util import decrypt_password
    row = _nvr(camera, channel)
    base = f"http://{row['ip']}:{NVR_HTTP_PORT}/cgi-bin/mediaFileFind.cgi"
    s = requests.Session()
    s.auth = HTTPDigestAuth(row["username"] or "", decrypt_password(row["password"] or ""))
    files: list[tuple[str, str]] = []
    obj = None
    try:
        r = s.get(base, params={"action": "factory.create"}, timeout=8)
        if r.status_code != 200 or "result=" not in r.text:
            raise LiveError("nvr_refused")
        obj = r.text.strip().split("=", 1)[1]
        # Dahua reads a space in the times only as %20: requests' default "+" makes it ignore
        # the whole time condition and answer with files from some other window.
        from urllib.parse import quote, urlencode
        query = urlencode({"action": "findFile", "object": obj, "condition.Channel": channel,
                           "condition.StartTime": start, "condition.EndTime": end,
                           "condition.Types[0]": "dav"}, quote_via=quote, safe=":[]")
        r = s.get(f"{base}?{query}", timeout=15)
        if r.status_code == 200 and "OK" in r.text:
            # The NVR hands the list out in small batches (often fewer than asked for), so keep
            # asking until it says found=0; a short batch does not mean the list is over.
            for _ in range(400):            # 400 batches is far more than a month holds
                r = s.get(base, params={"action": "findNextFile", "object": obj, "count": 100}, timeout=15)
                found = re.search(r"found=(\d+)", r.text)
                items: dict[str, dict] = {}
                for idx, field, value in re.findall(r"items\[(\d+)\]\.(StartTime|EndTime)=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", r.text):
                    items.setdefault(idx, {})[field] = value
                batch = [(v["StartTime"], v["EndTime"]) for v in items.values() if "StartTime" in v and "EndTime" in v]
                files += batch
                if not found or int(found.group(1)) == 0 or not batch:
                    break
    except LiveError:
        raise
    except Exception as exc:
        logger.warning(f"[LIVE] record search failed ch{channel}: {type(exc).__name__}")
        raise LiveError("nvr_unreachable")
    finally:
        if obj:
            for action in ("close", "destroy"):
                try:
                    s.get(base, params={"action": action, "object": obj}, timeout=5)
                except Exception:
                    pass
    return files


def record_days(camera: int, channel: int, month: str) -> list[str]:
    """The days of `month` (YYYY-MM) on which the NVR holds video for this channel.
    Cached 10 min so a calendar flicked back and forth does not hammer the NVR."""
    m = _MONTH_RE.match(month or "")
    if not m:
        raise LiveError("bad_request")
    key = (camera, channel, month)
    hit = _days_cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    import calendar
    year, mon = int(m.group(1)), int(m.group(2))
    last = calendar.monthrange(year, mon)[1]
    files = _find_files(camera, channel, f"{year:04d}-{mon:02d}-01 00:00:00", f"{year:04d}-{mon:02d}-{last:02d} 23:59:59")
    out = sorted({a[:10] for a, _ in files if a.startswith(month)})
    _days_cache[key] = (time.monotonic(), out)
    return out


_spans_cache: dict[tuple, tuple[float, list]] = {}
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def record_spans(camera: int, channel: int, day: str) -> list[list[int]]:
    """The parts of `day` (YYYY-MM-DD) that have video, as [start, end] seconds of the day,
    neighbouring files merged. Cached 1 min for today (still recording), 10 min otherwise."""
    if not _DAY_RE.match(day or ""):
        raise LiveError("bad_request")
    key = (camera, channel, day)
    ttl = 60 if day == datetime.now().strftime("%Y-%m-%d") else 600
    hit = _spans_cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    files = _find_files(camera, channel, f"{day} 00:00:00", f"{day} 23:59:59")
    d0 = datetime.strptime(day, "%Y-%m-%d")
    spans = []
    for a, b in files:
        try:
            s0 = (datetime.strptime(a, "%Y-%m-%d %H:%M:%S") - d0).total_seconds()
            s1 = (datetime.strptime(b, "%Y-%m-%d %H:%M:%S") - d0).total_seconds()
        except ValueError:
            continue
        s0, s1 = max(0, int(s0)), min(86400, int(s1))
        if s1 > s0:
            spans.append([s0, s1])
    spans.sort()
    merged: list[list[int]] = []
    for s0, s1 in spans:
        if merged and s0 <= merged[-1][1] + 5:      # a file boundary is not a gap
            merged[-1][1] = max(merged[-1][1], s1)
        else:
            merged.append([s0, s1])
    _spans_cache[key] = (time.monotonic(), merged)
    return merged


# ---- ffmpeg -------------------------------------------------------------------------------

def _private_input(url: str) -> str:
    """An ffconcat file naming the RTSP URL: 0700 folder, 0600 file, deleted on stop."""
    folder = tempfile.mkdtemp(prefix="aicam-live-")
    os.chmod(folder, 0o700)
    path = os.path.join(folder, "in.ffconcat")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        escaped = url.replace("'", "'\\''")
        f.write(f"ffconcat version 1.0\nfile '{escaped}'\n")
        if url.startswith("rtsp://"):
            f.write("option rtsp_transport tcp\n")
            f.write("option timeout 10000000\n")     # a channel with no camera gives up after 10 s
    return path


def _input_args(list_path: str) -> list[str]:
    return ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file,rtsp,rtp,tcp,udp,http", "-i", list_path]


def _loadfile_url(camera: int, channel: int, begin: datetime, end: datetime) -> str:
    """The NVR's own recording for [begin, end] over HTTP (Dahua loadfile.cgi): it arrives
    far faster than real time, which fast-forward and reverse play need. Main stream only:
    this NVR answers 400 for the sub-stream."""
    from urllib.parse import quote as q
    from data_service.configs.crypto_util import decrypt_password
    row = _nvr(camera, channel)
    fmt = "%Y-%m-%d %H:%M:%S"
    auth = f"{q(row['username'] or '', safe='')}:{q(decrypt_password(row['password'] or ''), safe='')}"
    return (f"http://{auth}@{row['ip']}:{NVR_HTTP_PORT}/cgi-bin/loadfile.cgi?action=startLoad&channel={channel}"
            f"&startTime={q(begin.strftime(fmt))}&endTime={q(end.strftime(fmt))}&subtype=0")


SPEEDS = (1, 2, 5, 10)
REVERSE_CHUNK = 6          # seconds of output per reversed chunk (6 x speed seconds of video)


# Bitrates (kbit/s), 80% of the first ones: pictures flowed choppily over a busy link. The full
# picture is 720p, not the camera's 2.5K (a 1x1 tile on a laptop does not show more).
KBPS = {"sub": "560k", "main": "2000k", "fast": "2000k", "reverse": "1600k", "grid": "480k", "still": "200k"}
MAIN_W, MAIN_H = 1280, 720


def _encoder(bitrate: str) -> list[str]:
    return ["-c:v", "h264_videotoolbox", "-realtime", "1", "-profile:v", "baseline", "-bf", "0",
            "-g", "50", "-b:v", bitrate]


def _reverse_feed(entry: dict, camera: int, channel: int, begin: datetime, end: datetime, speed: int):
    """Reverse play: take the recording back from `end` in chunks of REVERSE_CHUNK*speed seconds,
    reverse each (fps cut to 25/speed first, so a chunk is ~150 frames at 960 wide), and feed
    them one after another into the publisher's stdin with running timestamps. One chunk is
    made ahead while the previous one plays, so the picture does not stall between chunks."""
    import queue
    ffmpeg = _tool("ffmpeg")
    pipe = entry["proc"].stdin
    chunks: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=1)
    stop = entry["stop"]

    def produce():
        t, offset = end, 0.0
        try:
            while t > begin and not stop.is_set():
                w = max(begin, t - timedelta(seconds=REVERSE_CHUNK * speed))
                list_path = _private_input(_loadfile_url(camera, channel, w, t))
                try:
                    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-hwaccel", "videotoolbox",
                           *_input_args(list_path), "-an",
                           "-vf", f"fps={25 / speed},scale=960:-2,reverse,setpts=(PTS-STARTPTS)/{speed}",
                           "-output_ts_offset", f"{offset:.3f}", "-c:v", "mpeg2video", "-q:v", "4",
                           "-f", "mpegts", "pipe:1"]
                    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
                    entry["producer"] = p
                    data, _ = p.communicate(timeout=120)
                finally:
                    _remove_input(list_path)
                if stop.is_set():
                    break
                if data:
                    chunks.put(data)
                offset += (t - w).total_seconds() / speed
                t = w
        except Exception as exc:
            entry["err"] = f"reverse chunk failed: {type(exc).__name__}"
        finally:
            chunks.put(None)

    threading.Thread(target=produce, name="live-reverse", daemon=True).start()
    try:
        while not stop.is_set():
            data = chunks.get()
            if data is None:
                break
            pipe.write(data)
            pipe.flush()
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _codec(key: tuple, list_path: str) -> str:
    """The source's video codec (cached per camera/channel/subtype): hevc must be re-encoded."""
    if key in _codec_cache:
        return _codec_cache[key]
    probe = _tool("ffprobe")
    if not probe:
        return "unknown"
    try:
        r = subprocess.run([probe, "-v", "error", *_input_args(list_path),
                            "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                            "-of", "default=nw=1:nk=1"],
                           capture_output=True, text=True, timeout=20)
        codec = (r.stdout.strip().splitlines() or ["unknown"])[0] if r.returncode == 0 else "unknown"
    except Exception:
        codec = "unknown"
    if codec != "unknown":
        _codec_cache[key] = codec
    return codec


def _media_host(origin: Optional[str] = None) -> tuple[str, str]:
    """Scheme and host of the admin link the request came in on (`origin`, set by
    client_logic per socket, never by the message): the only place video may go."""
    url = origin or os.getenv("SOCKET_SERVER_HOST") or ""
    if not url:
        try:
            from _secrets import SOCKET_SERVERS  # type: ignore
            url = str((SOCKET_SERVERS[0] or {}).get("url") or "")
        except Exception:
            url = ""
    parts = urlsplit(url)
    if not parts.hostname or parts.scheme not in ("http", "https"):
        raise LiveError("no_media_host")
    return parts.scheme, parts.hostname


def _parse_time(value) -> datetime:
    try:
        return datetime.strptime(str(value), _TIME_FMT)
    except Exception:
        raise LiveError("bad_time")


CHUNK_BYTES = 64 * 1024
BATCH_BYTES = 32 * 1024
BATCH_SECONDS = 0.15
# A stream whose video is stuck on the way to the admin for longer than this is stopped here:
# the shop's uplink cannot carry it, and everything else on the same link (updates, commands)
# would wait behind it. Measured with an ack asked for once a second.
try:
    MAX_BACKLOG = min(max(float(os.getenv("AICAM_LIVE_MAX_BACKLOG_S", "4")), 1.0), 60.0)
except ValueError:
    MAX_BACKLOG = 4.0
PROBE_EVERY = 1.0
# The admin link dropping is not the end of a stream: for this long ffmpeg keeps running and
# its output is thrown away; when the same link is back the video goes on. The admin keeps
# the viewers' sessions a little longer (30 s) for the same reason.
RECONNECT_GRACE = 25


def start(data: dict, origin: Optional[str] = None, emit=None) -> dict:
    """`emit(event, payload)` sends on the admin link the request came in on (socket transport)."""
    if not isinstance(data, dict):
        raise LiveError("bad_request")
    sid = str(data.get("sid") or "")
    token = str(data.get("publish_token") or "")
    transport = data.get("transport", "webrtc")
    if transport not in ("webrtc", "socket") or not _SID_RE.match(sid):
        raise LiveError("bad_request")
    if transport == "webrtc" and not _SECRET_RE.match(token):
        raise LiveError("bad_request")
    if transport == "socket" and emit is None:
        raise LiveError("no_socket")
    try:
        camera, channel = int(data["camera"]), int(data["channel"])
        subtype = int(data.get("subtype", 1))
        media_port = int(data.get("media_port", 8889))
        seconds = min(int(data.get("seconds", MAX_SECONDS)), MAX_SECONDS)
    except Exception:
        raise LiveError("bad_request")
    mode = data.get("mode", "live")
    if mode not in ("live", "playback") or subtype not in (0, 1) or not (0 < media_port < 65536) or seconds <= 0:
        raise LiveError("bad_request")
    begin = end = None
    speed, direction = 1, "fwd"
    if mode == "playback":
        begin, end = _parse_time(data.get("start")), _parse_time(data.get("end"))
        if not (begin < end <= datetime.now() + timedelta(minutes=1)) or end - begin > MAX_PLAYBACK_SPAN:
            raise LiveError("bad_time")
        try:
            speed = int(data.get("speed", 1))
        except (TypeError, ValueError):
            raise LiveError("bad_request")
        direction = data.get("direction", "fwd")
        if speed not in SPEEDS or direction not in ("fwd", "rev"):
            raise LiveError("bad_request")
    fast = mode == "playback" and (speed > 1 or direction == "rev")
    # How much of the branch a stream may use. Live: sub-stream (cheap) or main (1x1 / enlarged).
    # Playback: the NVR records the 2.5K main stream only, so each one is decoded here:
    #   full  = the whole picture (1x1 / enlarged)          -> counts as main (MAX_MAIN)
    #   grid  = scaled to 640 on the GPU (2x2)               -> at most MAX_PB_GRID at once
    #   still = keyframes only, ~1 picture / 2 s (4x4)       -> cheap, only MAX_STREAMS applies
    quality = "full"
    if mode == "playback":
        quality = data.get("quality", "full")
        if quality not in ("full", "grid", "still") or (fast and quality != "full"):
            raise LiveError("bad_request")
    cls = ("main" if subtype == 0 else "sub") if mode == "live" else ("main" if quality == "full" else quality)

    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        raise LiveError("no_ffmpeg")
    if transport == "webrtc" and not _ffmpeg_caps(ffmpeg)[1]:
        raise LiveError("no_whip")              # this ffmpeg is older than 8: socket transport only
    scheme, host = _media_host(origin) if transport == "webrtc" else ("", "")
    with _lock:
        _reap_locked()
        if sid in _streams:
            raise LiveError("duplicate")
        if len(_streams) >= MAX_STREAMS:
            raise LiveError("too_many_streams")
        if cls == "main" and sum(1 for e in _streams.values() if e.get("cls") == "main") >= MAX_MAIN:
            raise LiveError("too_many_main")
        if cls == "grid" and sum(1 for e in _streams.values() if e.get("cls") == "grid") >= MAX_PB_GRID:
            raise LiveError("too_many_playback")
        _streams[sid] = {"starting": True, "cls": cls}   # hold the slot while ffmpeg starts

    list_path = None
    head = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", os.getenv("AICAM_LIVE_FFMPEG_LOG", "warning")]
    if transport == "webrtc":
        out = f"{scheme}://{host}:{media_port}/s/{sid}/whip"
        tail = ["-f", "whip", "-authorization", token, out]
    else:
        # LL-HLS on the admin side cuts on keyframes: one every second keeps it ~1 s behind live.
        tail = ["-force_key_frames", "expr:gte(t,n_forced*1)", "-f", "mpegts", "-flush_packets", "1", "pipe:1"]
    out_pipe = subprocess.PIPE if transport == "socket" else subprocess.DEVNULL
    try:
        if fast and direction == "fwd":
            # Fast-forward: the recording arrives faster than real time; read it at `speed` x,
            # squeeze time by `speed` and keep 25 fps, so the viewer gets speed x in real time.
            list_path = _private_input(_loadfile_url(camera, channel, begin, end))
            codec, transcode = "hevc", True
            cmd = [*head, "-hwaccel", "videotoolbox", "-readrate", str(speed), *_input_args(list_path), "-an",
                   "-vf", f"setpts=PTS/{speed},fps=25,scale=1280:-2", *_encoder(KBPS["fast"]), *tail]
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out_pipe,
                                    stderr=subprocess.PIPE, start_new_session=True)
        elif fast:
            # Reverse: _reverse_feed() writes reversed chunks into this publisher's stdin.
            codec, transcode = "hevc", True
            cmd = [*head, "-re", "-f", "mpegts", "-i", "pipe:0", "-an", *_encoder(KBPS["reverse"]), *tail]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out_pipe,
                                    stderr=subprocess.PIPE, start_new_session=True)
        else:
            list_path = _private_input(_rtsp_url(camera, channel, subtype, mode, begin, end))
            # Always re-encode to H.264 on the Mac's hardware encoder (a few % CPU): probing the
            # codec first cost up to 20 s per channel without a camera, so a 4x4 grid took
            # minutes to start and its first streams were cut as "nobody watching".
            codec, transcode = _codec_cache.get((camera, channel, subtype), "unknown"), True
            if mode == "playback" and quality == "grid":
                # decode and scale on the GPU (measured on an M1: 8 at once still leaves ~1/3 idle)
                pre = ["-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld", "-re"]
                video = ["-vf", "scale_vt=w=640:h=360", *_encoder(KBPS["grid"])]
            elif mode == "playback" and quality == "still":
                # keyframes only (~0.5 fps): 12 at once still leaves ~2/3 idle; every frame is a
                # keyframe so a viewer joining sees a picture at once
                pre = ["-skip_frame", "nokey", "-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld", "-re"]
                video = ["-vf", "scale_vt=w=480:h=270", *_encoder(KBPS["still"])[:-4], "-g", "1", "-b:v", KBPS["still"]]
            elif subtype == 1 and mode == "live":
                video, pre = _encoder(KBPS["sub"]), []
            else:
                # The full picture (live main, playback): decoded and scaled to 720p on the GPU.
                pre = ["-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld"]
                if mode == "playback":
                    pre.append("-re")
                video = ["-vf", f"scale_vt=w={MAIN_W}:h={MAIN_H}", *_encoder(KBPS["main"])]
            cmd = [*head, *pre, *_input_args(list_path), "-an", *video, *tail]
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out_pipe,
                                    stderr=subprocess.PIPE, start_new_session=True)
    except LiveError:
        _drop(sid, list_path)
        raise
    except Exception as exc:
        _drop(sid, list_path)
        logger.warning(f"[LIVE] start failed {sid[:8]}: {type(exc).__name__}")
        raise LiveError("start_failed")

    entry = {"proc": proc, "list": list_path, "deadline": time.monotonic() + seconds,
             "camera": camera, "channel": channel, "mode": mode, "subtype": subtype, "cls": cls, "err": "",
             "stop": threading.Event(), "producer": None, "origin": origin, "transport": transport}
    threading.Thread(target=_drain_stderr, args=(proc, entry), daemon=True).start()
    if transport == "socket":
        threading.Thread(target=_pump, args=(sid, proc, entry, emit), name="live-pump", daemon=True).start()
    if fast and direction == "rev":
        threading.Thread(target=_reverse_feed, args=(entry, camera, channel, begin, end, speed),
                         name="live-reverse-feed", daemon=True).start()
    with _lock:
        _streams[sid] = entry
    _ensure_watchdog()
    logger.info(f"[LIVE] start {sid[:8]} ch{channel} sub{subtype} {mode} {direction} x{speed} "
                f"{'transcode' if transcode else 'copy'} {transport} ({len(_streams)}/{MAX_STREAMS})")
    return {"ok": True, "codec": codec, "transcode": transcode, "speed": speed, "direction": direction,
            "transport": transport}


def _pump(sid: str, proc, entry: dict, emit) -> None:
    """Socket transport: ffmpeg's MPEG-TS goes to the admin on the link the request came in on.
    ffmpeg flushes every few KB; each Socket.IO event costs the admin far more than its bytes,
    so pieces are gathered and sent once BATCH_BYTES are in or BATCH_SECONDS passed (well under
    the 200 ms LL-HLS part). Once a second one chunk asks for an ack; if none has come back
    after MAX_BACKLOG seconds the video is piling up on the way (uplink too slow): the stream
    is stopped and the admin told why. While the link is down (detach(), or a send refused)
    nothing is sent or queued, ffmpeg's output is dropped, and sending resumes when the link is
    back; the watchdog ends a stream detached for RECONNECT_GRACE. Stops with ffmpeg or stop()."""
    sent, buf, since = 0, bytearray(), time.monotonic()
    probe = entry.setdefault("probe", {"out": None, "last": 0.0})

    def answered(*_):
        probe["out"] = None

    def send(piece: bytes) -> bool:
        now = time.monotonic()
        try:
            if probe["out"] is None and now - probe["last"] >= PROBE_EVERY:
                probe["out"] = probe["last"] = now
                emit("live_chunk", {"sid": sid, "b": piece}, callback=answered)
            else:
                emit("live_chunk", {"sid": sid, "b": piece})
            return True
        except Exception:
            # The link refused it (down, or reconnecting): wait for it like a disconnect.
            if not entry.get("detached_at"):
                entry["detached_at"] = time.monotonic()
            probe["out"] = None
            return False

    try:
        while not entry["stop"].is_set():
            data = proc.stdout.read1(CHUNK_BYTES)
            if not data:
                break
            if entry.get("detached_at"):
                buf.clear()                       # nothing piles up while the link is down
                continue
            out = probe["out"]
            if out is not None and time.monotonic() - out > MAX_BACKLOG:
                entry["err"] = "link_too_slow"
                logger.warning(f"[LIVE] {sid[:8]} stopped: video backed up over {MAX_BACKLOG:.0f} s (uplink too slow)")
                try:
                    emit("live_gone", {"sid": sid, "why": "link_too_slow"})
                except Exception:
                    pass
                if proc.poll() is None:
                    proc.terminate()
                break
            buf += data
            if len(buf) >= BATCH_BYTES or time.monotonic() - since >= BATCH_SECONDS:
                while buf:
                    piece, buf = bytes(buf[:CHUNK_BYTES]), buf[CHUNK_BYTES:]
                    if not send(piece):
                        buf.clear()
                        break
                    sent += len(piece)
                since = time.monotonic()
        if buf and not entry["stop"].is_set() and entry.get("err") != "link_too_slow" and not entry.get("detached_at"):
            send(bytes(buf[:CHUNK_BYTES]))
    except Exception as exc:
        entry["err"] = f"send failed: {type(exc).__name__}"
        if proc.poll() is None:
            proc.terminate()
    entry["sent"] = sent


def detach(origin: Optional[str] = None) -> int:
    """The admin link `origin` dropped: its socket streams wait RECONNECT_GRACE for it to come
    back (WebRTC streams go to the media server directly and are not affected). Returns how many."""
    now = time.monotonic()
    n = 0
    with _lock:
        for entry in _streams.values():
            if entry.get("starting") or (origin is not None and entry.get("origin") != origin):
                continue
            if entry.get("transport") == "socket" and not entry.get("detached_at"):
                entry["detached_at"] = now
                entry.setdefault("probe", {"out": None, "last": 0.0})["out"] = None
                n += 1
    if n:
        logger.info(f"[LIVE] link down: {n} stream(s) wait up to {RECONNECT_GRACE} s for it")
    return n


def reattach(origin: Optional[str] = None) -> int:
    """The admin link `origin` is back: its waiting streams send again."""
    n = 0
    with _lock:
        for entry in _streams.values():
            if entry.get("detached_at") and (origin is None or entry.get("origin") == origin):
                entry["detached_at"] = None
                entry.setdefault("probe", {"out": None, "last": 0.0})["out"] = None
                n += 1
    if n:
        logger.info(f"[LIVE] link back: {n} stream(s) sending again")
    return n


def _drain_stderr(proc, entry):
    try:
        tail: list[str] = []
        for line in proc.stderr:
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            tail = (tail + [_scrub(line.strip())])[-5:]
            entry["err"] = " | ".join(tail)[-600:]
    except Exception:
        pass


def _drop(sid: str, list_path: Optional[str]):
    with _lock:
        _streams.pop(sid, None)
    _remove_input(list_path)


def _remove_input(list_path: Optional[str]):
    if list_path:
        shutil.rmtree(os.path.dirname(list_path), ignore_errors=True)


def _kill(entry: dict):
    if entry.get("stop"):
        entry["stop"].set()
    producer = entry.get("producer")
    if producer and producer.poll() is None:
        producer.kill()
    proc = entry.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    _remove_input(entry.get("list"))


def stop(sid: str, why: str = "stop") -> bool:
    with _lock:
        entry = _streams.pop(sid, None)
    if not entry or entry.get("starting"):
        return False
    _kill(entry)
    logger.info(f"[LIVE] stop {sid[:8]} ({why})")
    return True


def stop_all(why: str = "disconnect", origin: Optional[str] = None):
    """Stop every stream, or only those sent for the admin link `origin` (its socket dropped)."""
    with _lock:
        sids = [s for s, e in _streams.items()
                if not e.get("starting") and (origin is None or e.get("origin") == origin)]
    for sid in sids:
        stop(sid, why)


def _reap_locked():
    """Forget streams whose ffmpeg already exited (caller holds _lock)."""
    for sid, e in list(_streams.items()):
        proc = e.get("proc")
        if proc is not None and proc.poll() is not None:
            _streams.pop(sid, None)
            _remove_input(e.get("list"))
            _ended[sid[:8]] = {"rc": proc.returncode, "err": e.get("err", ""), "channel": e.get("channel")}
            while len(_ended) > 10:
                _ended.pop(next(iter(_ended)))
            logger.info(f"[LIVE] ended {sid[:8]} rc={proc.returncode} {e.get('err', '')}")


def _ensure_watchdog():
    global _watchdog_started
    with _lock:
        if _watchdog_started:
            return
        _watchdog_started = True

    def loop():
        while True:
            time.sleep(5)
            now = time.monotonic()
            with _lock:
                _reap_locked()
                late = [s for s, e in _streams.items() if not e.get("starting") and e["deadline"] <= now]
                gone = [s for s, e in _streams.items() if not e.get("starting") and e.get("detached_at")
                        and now - e["detached_at"] > RECONNECT_GRACE]
            for sid in late:
                stop(sid, "deadline")
            for sid in gone:
                stop(sid, "disconnect")

    threading.Thread(target=loop, name="live-watchdog", daemon=True).start()


# ---- Socket.IO handlers (the return value is the ack) -------------------------------------

def on_live_cameras(data):  # pragma: no cover - thin wrapper
    try:
        return {"ok": True, "cameras": list_cameras(), "max_streams": MAX_STREAMS, "max_main": MAX_MAIN,
                "store": store_info()}
    except Exception as exc:
        logger.warning(f"[LIVE] camera list failed: {type(exc).__name__}")
        return {"ok": False, "error": "camera_list_failed"}


def on_live_start(data, origin: Optional[str] = None, emit=None):  # pragma: no cover - thin wrapper
    try:
        return start(data, origin, emit)
    except LiveError as exc:
        return {"ok": False, "error": exc.code}


def on_live_record_days(data):  # pragma: no cover - thin wrapper
    try:
        d = data if isinstance(data, dict) else {}
        return {"ok": True, "days": record_days(int(d.get("camera")), int(d.get("channel")), str(d.get("month") or ""))}
    except LiveError as exc:
        return {"ok": False, "error": exc.code}
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_request"}


# ---- siren setup, driven from the admin portal ---------------------------------------------
# The setup page (siren_setup/app.py) runs on this machine at 127.0.0.1:8767 and keeps the
# siren keys here. The portal reaches it only through this link, only on these paths, and
# never gets a key back (the setup page never sends one).
SIREN_SETUP = "127.0.0.1:8767"
SIREN_PATHS = {("GET", "/api/state"), ("GET", "/api/live"), ("GET", "/api/qr/status"),
               ("POST", "/api/qr"), ("POST", "/api/scan"), ("POST", "/api/ring"),
               ("POST", "/api/check"), ("POST", "/api/settings")}


def on_siren_call(data):  # pragma: no cover - thin wrapper
    import json as _json
    import urllib.error
    import urllib.request
    d = data if isinstance(data, dict) else {}
    method, path, body = str(d.get("method") or ""), str(d.get("path") or ""), d.get("body") or {}
    if (method, path) not in SIREN_PATHS or not isinstance(body, dict):
        return {"ok": False, "error": "bad_request"}
    req = urllib.request.Request(f"http://{SIREN_SETUP}{path}", method=method,
                                 data=_json.dumps(body).encode() if method == "POST" else None,
                                 headers={"Host": SIREN_SETUP, "X-Siren-Setup": "1", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return {"ok": True, "status": r.status, "body": _json.loads(r.read() or b"{}")}
    except urllib.error.HTTPError as exc:
        return {"ok": True, "status": exc.code, "body": {"error": "setup_refused"}}
    except (urllib.error.URLError, OSError):
        return {"ok": False, "error": "siren_setup_down"}
    except ValueError:
        return {"ok": False, "error": "bad_answer"}


def on_live_record_spans(data):  # pragma: no cover - thin wrapper
    try:
        d = data if isinstance(data, dict) else {}
        return {"ok": True, "spans": record_spans(int(d.get("camera")), int(d.get("channel")), str(d.get("day") or ""))}
    except LiveError as exc:
        return {"ok": False, "error": exc.code}
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_request"}


def on_live_stop(data):  # pragma: no cover - thin wrapper
    sid = str((data or {}).get("sid") or "") if isinstance(data, dict) else ""
    if not _SID_RE.match(sid):
        return {"ok": False, "error": "bad_request"}
    return {"ok": True, "stopped": stop(sid, "admin")}


def extend(sid: str, seconds: int) -> bool:
    """A viewer joined a shared stream: keep it running up to MAX_SECONDS from now."""
    with _lock:
        entry = _streams.get(sid)
        if not entry or entry.get("starting"):
            return False
        entry["deadline"] = time.monotonic() + min(max(int(seconds), 0), MAX_SECONDS)
    return True


def on_live_extend(data):  # pragma: no cover - thin wrapper
    d = data if isinstance(data, dict) else {}
    sid = str(d.get("sid") or "")
    try:
        seconds = int(d.get("seconds", MAX_SECONDS))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_request"}
    if not _SID_RE.match(sid) or seconds <= 0:
        return {"ok": False, "error": "bad_request"}
    return {"ok": extend(sid, seconds)}


def on_live_status(data):  # pragma: no cover - thin wrapper
    with _lock:
        _reap_locked()
        return {"ok": True, "streams": {s[:8]: {"channel": e.get("channel"), "mode": e.get("mode"),
                                                "err": e.get("err", "")}
                                         for s, e in _streams.items() if not e.get("starting")},
                "ended": dict(_ended)}


# ---- which NVR channel an alert came from ----------------------------------------------------
_CAM_KEY_RE = re.compile(r"^cam_(\d{1,3})_(\d{1,3})_(\d{1,3})_(\d{1,3})_(\d{1,3})$")


def resolve(key: str) -> Optional[dict]:
    """{camera, channel, nvr} for an alert's camera key (`cam_<NVR address>_<channel>`), so the
    admin can open the NVR's video around the alert. The address stays on this machine."""
    m = _CAM_KEY_RE.match(key or "")
    if not m:
        return None
    ip, channel = ".".join(m.group(i) for i in range(1, 5)), int(m.group(5))
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name, channel_count FROM camera_master WHERE ip = ? ORDER BY id LIMIT 1", (ip,))
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    if not row:
        return None
    count = int(row.get("channel_count") or 0)
    if channel < 1 or (count and channel > count):
        return None
    return {"camera": int(row["id"]), "channel": channel, "nvr": str(row.get("name") or "")[:60]}


def on_live_resolve(data):  # pragma: no cover - thin wrapper
    key = str((data or {}).get("key") or "") if isinstance(data, dict) else ""
    try:
        found = resolve(key)
    except Exception as exc:
        logger.warning(f"[LIVE] camera lookup failed: {type(exc).__name__}")
        return {"ok": False, "error": "lookup_failed"}
    return {"ok": True, **found} if found else {"ok": False, "error": "unknown_camera"}
