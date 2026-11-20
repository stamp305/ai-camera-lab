"""Live and playback camera video for the admin, sent on demand to the media server.

    admin  --live_start {sid, camera, channel, subtype, mode, ...}-->  this branch
    this branch: ffmpeg  NVR (RTSP, LAN only)  --WHIP-->  MediaMTX (admin side)
    (this branch's ffmpeg publishes with WHIP: WebRTC media is always DTLS-SRTP encrypted,
     and the WHIP request goes https whenever this branch's admin link is https)
    viewer browser  <--WebRTC--  MediaMTX           (every publish/read checked by the admin)

Secure by design / PDPA:
  * The NVR login never leaves this machine. The admin names a camera (camera_master.id)
    and a channel; the RTSP URL is built here from this branch's own database, and it is
    handed to ffmpeg through a 0600 file in a 0700 folder (ffconcat), not on the command
    line, so `ps` does not show it. Nothing logs it; ffmpeg's errors are scrubbed.
  * Video goes only to the media host this branch already trusts (the host and scheme
    of its own admin link), whatever a message names.
  * Nothing is recorded here or there: this only relays. Each stream stops at its
    deadline (<= 30 min), on live_stop, on disconnect, and when too many are asked for.
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


def _tool(name: str) -> Optional[str]:
    env = os.getenv(f"AICAM_{name.upper()}")
    if env and os.path.isfile(env):
        return env
    return shutil.which(name) or next(
        (p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}") if os.path.isfile(p)), None)


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
            "province": (row.get("province") or "").strip()[:60]}


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


def _media_host() -> tuple[str, str]:
    """Scheme and host of this branch's own admin link: the only place video may go."""
    url = os.getenv("SOCKET_SERVER_HOST") or ""
    if not url:
        try:
            from _secrets import SOCKET_SERVER_HOST as url  # type: ignore
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


def start(data: dict) -> dict:
    if not isinstance(data, dict):
        raise LiveError("bad_request")
    sid = str(data.get("sid") or "")
    token = str(data.get("publish_token") or "")
    if not _SID_RE.match(sid) or not _SECRET_RE.match(token):
        raise LiveError("bad_request")
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
    scheme, host = _media_host()
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
    out = f"{scheme}://{host}:{media_port}/s/{sid}/whip"
    head = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", os.getenv("AICAM_LIVE_FFMPEG_LOG", "warning")]
    tail = ["-f", "whip", "-authorization", token, out]
    try:
        if fast and direction == "fwd":
            # Fast-forward: the recording arrives faster than real time; read it at `speed` x,
            # squeeze time by `speed` and keep 25 fps, so the viewer gets speed x in real time.
            list_path = _private_input(_loadfile_url(camera, channel, begin, end))
            codec, transcode = "hevc", True
            cmd = [*head, "-hwaccel", "videotoolbox", "-readrate", str(speed), *_input_args(list_path), "-an",
                   "-vf", f"setpts=PTS/{speed},fps=25,scale=1280:-2", *_encoder("2500k"), *tail]
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
        elif fast:
            # Reverse: _reverse_feed() writes reversed chunks into this publisher's stdin.
            codec, transcode = "hevc", True
            cmd = [*head, "-re", "-f", "mpegts", "-i", "pipe:0", "-an", *_encoder("2000k"), *tail]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
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
                video = ["-vf", "scale_vt=w=640:h=360", *_encoder("600k")]
            elif mode == "playback" and quality == "still":
                # keyframes only (~0.5 fps): 12 at once still leaves ~2/3 idle; every frame is a
                # keyframe so a viewer joining sees a picture at once
                pre = ["-skip_frame", "nokey", "-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld", "-re"]
                video = ["-vf", "scale_vt=w=480:h=270", *_encoder("250k")[:-4], "-g", "1", "-b:v", "250k"]
            else:
                pre = ["-re"] if mode == "playback" else []
                video = _encoder("700k" if subtype == 1 else "2500k")
            cmd = [*head, *pre, *_input_args(list_path), "-an", *video, *tail]
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
    except LiveError:
        _drop(sid, list_path)
        raise
    except Exception as exc:
        _drop(sid, list_path)
        logger.warning(f"[LIVE] start failed {sid[:8]}: {type(exc).__name__}")
        raise LiveError("start_failed")

    entry = {"proc": proc, "list": list_path, "deadline": time.monotonic() + seconds,
             "camera": camera, "channel": channel, "mode": mode, "subtype": subtype, "cls": cls, "err": "",
             "stop": threading.Event(), "producer": None}
    threading.Thread(target=_drain_stderr, args=(proc, entry), daemon=True).start()
    if fast and direction == "rev":
        threading.Thread(target=_reverse_feed, args=(entry, camera, channel, begin, end, speed),
                         name="live-reverse-feed", daemon=True).start()
    with _lock:
        _streams[sid] = entry
    _ensure_watchdog()
    logger.info(f"[LIVE] start {sid[:8]} ch{channel} sub{subtype} {mode} {direction} x{speed} "
                f"{'transcode' if transcode else 'copy'} ({len(_streams)}/{MAX_STREAMS})")
    return {"ok": True, "codec": codec, "transcode": transcode, "speed": speed, "direction": direction}


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


def stop_all(why: str = "disconnect"):
    with _lock:
        sids = [s for s, e in _streams.items() if not e.get("starting")]
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
            for sid in late:
                stop(sid, "deadline")

    threading.Thread(target=loop, name="live-watchdog", daemon=True).start()


# ---- Socket.IO handlers (the return value is the ack) -------------------------------------

def on_live_cameras(data):  # pragma: no cover - thin wrapper
    try:
        return {"ok": True, "cameras": list_cameras(), "max_streams": MAX_STREAMS, "max_main": MAX_MAIN,
                "store": store_info()}
    except Exception as exc:
        logger.warning(f"[LIVE] camera list failed: {type(exc).__name__}")
        return {"ok": False, "error": "camera_list_failed"}


def on_live_start(data):  # pragma: no cover - thin wrapper
    try:
        return start(data)
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


def on_live_status(data):  # pragma: no cover - thin wrapper
    with _lock:
        _reap_locked()
        return {"ok": True, "streams": {s[:8]: {"channel": e.get("channel"), "mode": e.get("mode"),
                                                "err": e.get("err", "")}
                                         for s, e in _streams.items() if not e.get("starting")},
                "ended": dict(_ended)}
