import os
import sys
import urllib.parse

from typing import List, Optional

import asyncio
import base64
import hashlib
import re
import socket
import time
import cv2
import httpx

from data_service.db.crud.camera_crud import ensure_camera_channels, read_channel_count_by_ip
from data_service.configs.search_camera_config import (
    RTSP_URL,
    MAX_SCAN_CHANNELS,
    CONCURRENCY,
    FFPROBE_BASE_ARGS,
)
from data_service.db.crud.camera_crud import update_scan_status

import logging
logger = logging.getLogger(__name__)

AUTH = None
CLIENT = None


def _resolve_ffprobe_path() -> str:
    """Bare 'ffprobe' is never on PATH inside the packaged app — the bundled
    binary lives at python_env/bin/ffprobe alongside sys.executable, and
    nothing adds that directory to PATH at startup. Every channel probe was
    silently failing with FileNotFoundError, caught by _probe_channel's
    except-Exception and printed to a stderr no one reads, so scans always
    found 0 working channels regardless of credentials or camera brand.
    """
    bundled = os.path.join(os.path.dirname(sys.executable), "ffprobe")
    if os.path.isfile(bundled):
        return bundled
    return "ffprobe"  # dev/local run where it's actually on PATH


# =======================================================================
# Camera channel probing via OpenCV
# =======================================================================
def _build_video_capture(ip: str, username: str, password: str, channel: int) -> cv2.VideoCapture:
    os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = 'rtsp_transport;tcp|timeout;5000000'
    user_encoded = urllib.parse.quote(username, safe='')
    pass_encoded = urllib.parse.quote(password, safe='')
    url = RTSP_URL.format(ip=ip, username=user_encoded, password=pass_encoded, port=554, channel=channel)
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    del os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']
    return cap

def _scan_camera_channels_over_rtsp(ip: str, username: str, password: str) -> List[int]:
    working_channels = []
    for channel in range(1, MAX_SCAN_CHANNELS + 1):
        cap = _build_video_capture(ip, username, password, channel)
        if not cap.isOpened():
            continue
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            working_channels.append(channel)
    return working_channels

def scan_camera_channels(ip: str, username: str, password: str):
    print(f"Scanning camera channels for {ip}")
    start_time = time.time()
    working_channels = _scan_camera_channels_over_rtsp(ip, username, password)
    end_time = time.time()
    print(f"Scanning completed in {end_time - start_time:.2f} seconds")
    if working_channels:
        print(f"✅ Working cameras for {ip}: {len(working_channels)} found")
        ensure_camera_channels(ip, working_channels)
        update_scan_status(ip, True)
        print(f"✅ Camera probe completed for {ip}")
    else:
        print(f"⚠️ No working cameras found for {ip}")



# =======================================================================
# Camera channel probing via ffprobe
# =======================================================================
async def _probe_channel(sema: asyncio.Semaphore, ip: str, username: str, password: str, channel: int):
    # Build full RTSP URL with credentials for this channel
    user_encoded = urllib.parse.quote(username, safe='')
    pass_encoded = urllib.parse.quote(password, safe='')
    url = RTSP_URL.format(ip=ip, username=user_encoded, password=pass_encoded, port=554, channel=channel)
    args = [_resolve_ffprobe_path()] + FFPROBE_BASE_ARGS[1:] + [url]

    t0 = time.time()
    try:
        async with sema:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Give the whole process a hard cap too (2.5s)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                return (channel, None, "process-timeout", time.time() - t0)

        if proc.returncode == 0:
            codec = stdout.decode().strip()
            return (channel, codec or "unknown", None, time.time() - t0)
        else:
            # ffprobe repeats the URL it was given, password included.
            err = re.sub(r"rtsps?://\S+", "<camera stream>", stderr.decode().strip())
            return (channel, None, err or f"rc={proc.returncode}", time.time() - t0)
    except Exception as e:
        return (channel, None, str(e), time.time() - t0)

def get_nvr_channel_count(ip: str, username: str, password: str, timeout: float = 4.0) -> Optional[int]:
    """How many camera channels a Dahua NVR has, or None if it will not say.

    A Dahua NVR also streams its own multi-view screen (every camera in one grid,
    "zero channel") on the channel right after the last real one — 17 on a 16-channel
    NVR — so a scan of 1..32 offered that screen as a camera. Unlike snapshot.cgi,
    these configuration calls answer at once.
    """
    try:
        with httpx.Client(auth=httpx.DigestAuth(username or "", password or ""), timeout=timeout) as client:
            r = client.get(f"http://{ip}/cgi-bin/magicBox.cgi?action=getProductDefinition&name=MaxRemoteInputChannels")
            m = re.search(r"MaxRemoteInputChannels=(\d+)", r.text) if r.status_code == 200 else None
            if m and int(m.group(1)) > 0:
                return int(m.group(1))
            r = client.get(f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
            if r.status_code == 200:
                n = len(re.findall(r"ChannelTitle\[\d+\]\.Name=", r.text))
                return n or None
    except Exception as e:
        logger.warning(f"[camera scan] {ip}: could not read the NVR's channel count: {e}")
    return None


def parse_camera_states(text: str) -> dict[int, str]:
    """{channel (1-based): "absent" | "broken" | "connected"} from Dahua's getCameraState.

    The NVR answers lines like  states[3].Channel=3  and  states[3].ConnectionState=Connected.
    NoConfig / Empty means nothing is set up on the channel; Unconnect / Connecting
    means a camera is set up but is not answering.
    """
    channel_of: dict[int, int] = {}
    state_of: dict[int, str] = {}
    for idx, key, value in re.findall(r"states\[(\d+)\]\.(Channel|ConnectionState)=(\w+)", text, re.I):
        if key.lower() == "channel":
            channel_of[int(idx)] = int(value) if value.isdigit() else int(idx)
        else:
            state_of[int(idx)] = value
    out: dict[int, str] = {}
    for idx, state in state_of.items():
        channel = int(channel_of.get(idx, idx)) + 1   # the NVR counts from 0
        low = state.lower()
        if low in ("noconfig", "empty"):
            out[channel] = "absent"
        elif low in ("unconnect", "unconnected", "connecting", "disconnected"):
            out[channel] = "broken"
        elif low == "connected":
            out[channel] = "connected"
    return out


def get_nvr_channel_states(ip: str, username: str, password: str, timeout: float = 4.0) -> Optional[dict[int, str]]:
    """What the NVR itself says about each channel, or None if it will not say.

    This is what lets Camera Selection tell a camera that is set up but not
    answering ("broken") from a channel with nothing on it ("absent"): a missing
    picture alone cannot.
    """
    try:
        with httpx.Client(auth=httpx.DigestAuth(username or "", password or ""), timeout=timeout) as client:
            r = client.get(f"http://{ip}/cgi-bin/LogicDeviceManager.cgi?action=getCameraState&uuid=Unknown")
            if r.status_code == 200:
                return parse_camera_states(r.text) or None
    except Exception as e:
        logger.warning(f"[camera scan] {ip}: could not read the NVR's camera states: {e}")
    return None


async def scan_camera_channels_ffprobe(ip: str, username: str, password: str):
    """List an NVR's channels: 1..N, N being the 8, 16 or 32 chosen on Create network.

    Nothing is probed. A 5 s ffprobe, eight at a time, dropped any channel the NVR
    answered late, and a dropped channel was never looked at again. Camera
    Selection shows what each channel really gives (a picture, or why not), see
    snapshot_cache. (The name is from when this probed with ffprobe.)
    """
    try:
        count = read_channel_count_by_ip(ip)
        ensure_camera_channels(ip, range(1, count + 1))
        update_scan_status(ip, True)
        logger.info(f"[camera scan] {ip}: listed channels 1-{count}")
    except Exception as e:
        logger.error(f"[camera scan] Failed to persist scan results for {ip}: {e}")




# =======================================================================
# Camera HTTP utilities
# =======================================================================
def ping_ip(ip: str) -> bool:
    """
    Ping the given IP address to check if it's reachable.
    """
    response = os.system(f"ping -c 1 -W 1 {ip} > /dev/null 2>&1")
    return response == 0

def _parse_rtsp_status(resp: str) -> int:
    """Extract the numeric status from an RTSP status line, or 0 if unparseable."""
    if not resp.startswith("RTSP/"):
        return 0
    try:
        return int(resp.split(" ", 2)[1])
    except (IndexError, ValueError):
        return 0


def _build_digest_auth(method: str, uri: str, username: str, password: str,
                       params: dict) -> str:
    """Build an RFC 2617 Digest Authorization header for the given challenge."""
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    qop = params.get("qop")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop:
        nc = "00000001"
        cnonce = hashlib.md5(f"{nonce}{username}".encode()).hexdigest()[:16]
        response = hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()
        return (
            f'Authorization: Digest username="{username}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", qop={qop}, nc={nc}, '
            f'cnonce="{cnonce}", response="{response}"\r\n'
        )
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (
        f'Authorization: Digest username="{username}", realm="{realm}", '
        f'nonce="{nonce}", uri="{uri}", response="{response}"\r\n'
    )


def _rtsp_auth_status(
    ip: str, username: str, password: str,
    port: int = 554, timeout: float = 5.0,
) -> int:
    """
    Validate credentials with an authenticated RTSP **OPTIONS** handshake and
    return the resulting status code, WITHOUT touching any channel/media.

    Why OPTIONS (not DESCRIBE or VideoCapture): channel 1 on some devices is
    dead — DESCRIBE/media on it hangs for the full socket timeout (~30s) even
    though the credentials are valid and other channels stream fine. OPTIONS
    targets the server root, so it can't hang on a stalled channel. A User-Agent
    header is sent because Dahua RTSP servers may ignore requests without one
    (this is what made an earlier DESCRIBE attempt time out).
        200 -> credentials valid
        401 -> credentials invalid (or server doesn't gate OPTIONS by auth, in
               which case the challenge round still distinguishes)
    Raises OSError on connection failure.
    """
    uri = f"rtsp://{ip}:{port}/"

    def _send(sock: socket.socket, method: str, cseq: int, extra: str = "") -> str:
        req = (f"{method} {uri} RTSP/1.0\r\nCSeq: {cseq}\r\n"
               f"User-Agent: ai-camera-client\r\n{extra}\r\n")
        sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode(errors="replace")

    with socket.create_connection((ip, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        # 1) Unauthenticated OPTIONS -> expect 401 + WWW-Authenticate challenge.
        resp = _send(sock, "OPTIONS", 1)
        status = _parse_rtsp_status(resp)
        if status != 401:
            # Server didn't demand auth for OPTIONS; can't reject on creds here.
            return status

        m = re.search(r"WWW-Authenticate:\s*(\w+)\s+(.*)", resp, re.IGNORECASE)
        if not m:
            return status
        scheme = m.group(1).lower()
        # Parse both quoted (realm="..") and bare (qop=auth) params.
        params = {}
        for key, qval, uval in re.findall(
            r'(\w+)\s*=\s*(?:"([^"]*)"|([^",\s]+))', m.group(2)
        ):
            params[key.lower()] = qval or uval

        if scheme == "basic":
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            auth = f"Authorization: Basic {token}\r\n"
        else:  # digest
            auth = _build_digest_auth("OPTIONS", uri, username, password, params)

        # 2) Authenticated OPTIONS -> 200 means creds accepted, 401 rejected.
        resp2 = _send(sock, "OPTIONS", 2, auth)
        return _parse_rtsp_status(resp2)


def validate_username_password_for_camera(ip: str, username: str, password: str):
    """
    Validate camera credentials independently of whether channel 1 streams.

    Channel 1 on some NVRs/cameras is dead (no media) which used to hang the old
    VideoCapture-based check for 30s and report a misleading "wrong
    username/password" error — blocking the add even though the credentials were
    correct and other channels worked. We now validate via an authenticated RTSP
    OPTIONS handshake (server root, never a channel). The subsequent channel scan
    discovers the actual working channels.
    """
    try:
        status = _rtsp_auth_status(ip, username, password)
    except OSError as e:
        print(f"⚠️ Unable to reach RTSP service on {ip}: {e}")
        return False

    if status == 401:
        print(f"⚠️ RTSP credentials rejected (401) for {ip}.")
        return False
    if status == 0:
        print(f"⚠️ Unexpected/empty RTSP response from {ip}.")
        return False
    # 200 (OPTIONS accepted), or any non-401 status -> credentials accepted.
    return True

def get_image_from_camera(ip: str, channel: int, username: str, password: str) -> bytes:
    """
    Fetch an image from the camera at the specified IP and channel.
    """
    cap = _build_video_capture(ip, username, password, channel)
    if not cap.isOpened():
        raise Exception(f"Unable to open RTSP stream for {ip} channel {channel}")
    
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        raise Exception(f"Unable to read frame from RTSP stream for {ip} channel {channel}")
    
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        raise Exception(f"Failed to encode frame as JPEG for {ip} channel {channel}")
    return jpeg.tobytes()

def get_image_from_camera_digest_auth(ip: str, channel: int, username: str, password: str, timeout: float = 20.0) -> bytes:
    """
    Fetch an image from the camera using HTTP Digest Authentication.

    NOTE: Each call creates its own auth + client so that cameras with
    different credentials all work correctly (the previous global
    AUTH/CLIENT would lock to the first camera's credentials).
    """
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    ctx.options |= getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4)
    try:
        ctx.set_ciphers('DEFAULT@SECLEVEL=0')
    except ssl.SSLError:
        pass

    auth = httpx.DigestAuth(username, password)
    client = httpx.Client(auth=auth, timeout=3.0, follow_redirects=True, verify=ctx)

    url = f"http://{ip}/cgi-bin/snapshot.cgi?channel={channel}&subtype=1"
    time_start = time.time()

    try:
        while True:
            if time.time() - time_start > timeout:
                return None
            try:
                response = client.get(url)
                response.raise_for_status()
                if response.status_code == 200 and response.content:
                    return response.content
            except Exception as e:
                print(f"Error fetching image from {ip} channel {channel}: {e}")
                time.sleep(0.5)
                continue
    finally:
        client.close()