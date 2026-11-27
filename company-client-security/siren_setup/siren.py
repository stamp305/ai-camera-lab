"""The store's own siren on an intrusion alert: a Tuya Wi-Fi siren driven over the store's network.

Nothing goes through Tuya's cloud: this machine talks to the siren directly on the LAN
(Tuya local protocol 3.5, TCP 6668), the same way the Smart Life app does when it is on
the same Wi-Fi. The siren can be kept off the internet at the store's router and still ring.

Set up per store in the file SIREN_STORE names, by default
~/Library/Application Support/AICamera/siren/monitor_siren.json (no file, no siren):

    {"devices": [{"id": "<device id>", "key": "<local key>", "ip": "<optional>"}],
     "volume": "mute", "seconds": 2}

That file never lives under log/: the admin server pulls a branch's log folder, and the
local key in it opens the siren to anyone on the store's network. Nothing here ever logs
or reports a key, a device id or an address — an alert tells the server only that the
siren rang or did not, and why in one short word (on_intrusion below).

id and key come once from the Smart Life account the siren was paired with. Without "ip"
the siren is found on the LAN (it answers the "who is there" request the Smart Life app
sends to UDP 7000, broadcast and to each address of the store's /24), so a new address
from DHCP does not matter, nor a network that drops broadcasts.

volume is low / middle / high / mute and starts at mute: the siren takes the command, and
it is reported, without a sound until someone chooses otherwise. In trial mode
(log/monitor_shadow) nothing is rung at all and nothing is reported.

The siren is a Tuya "sgbj" alarm: DP 5 volume, DP 7 seconds to ring, DP 13 ringing.
"""
import hashlib
import hmac
import json
import logging
import os
import socket
import struct
import threading
import time
from typing import Dict, List, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import server_feed

logger = logging.getLogger(__name__)

# Holds the siren's local key: kept out of log/ (the admin can pull logs) in a 0700 folder.
CONFIG = os.environ.get("SIREN_STORE") or os.path.expanduser(
    "~/Library/Application Support/AICamera/siren/monitor_siren.json")

PORT = 6668
TIMEOUT = 5.0
FIND_SECONDS = 20.0          # older sirens broadcast about every 5 s; 3.5 ones answer when asked
ASK_EVERY = 6.0
COOLDOWN = 300.0             # one ring per 5 minutes, however many cameras alert
VOLUMES = ("low", "middle", "high", "mute")
DP_VOLUME, DP_SECONDS, DP_RINGING, DP_SOUND = "5", "7", "13", "21"
SOUNDS = tuple(f"mode_{i}" for i in range(1, 6))
SOUND = "mode_2"             # the alert tone: the siren's second sound unless the config picks another
# Setting DP_SOUND starts the siren ringing (seen on the real one), so it is only ever set as part
# of a ring, after the volume and length.

_PREFIX, _SUFFIX = 0x6699, b"\x00\x00\x99\x66"
_HEAD = ">IHIII"             # prefix, 0, seq, command, length
_HEAD_LEN = struct.calcsize(_HEAD)
_VERSION_HEADER = b"3.5" + b"\x00" * 12
_NEG_START, _NEG_RESP, _NEG_FINISH = 3, 4, 5
_CONTROL, _QUERY, _REQ_DEVINFO = 13, 16, 0x25
_UDP_KEY = hashlib.md5(b"yGAdlopoPVldABfn").digest()


class SirenError(Exception):
    pass


# -- Tuya 3.5 framing -----------------------------------------------------------------------
def pack(seq: int, cmd: int, payload: bytes, key: bytes) -> bytes:
    head = struct.pack(_HEAD, _PREFIX, 0, seq, cmd, len(payload) + 12 + 16)
    iv = os.urandom(12)
    return head + iv + AESGCM(key).encrypt(iv, payload, head[4:]) + _SUFFIX


def unpack(frame: bytes, key: bytes):
    """(command, plaintext) of one 6699 frame."""
    prefix, _, _, cmd, length = struct.unpack(_HEAD, frame[:_HEAD_LEN])
    if prefix != _PREFIX:
        raise SirenError("not a 3.5 frame")
    body = frame[_HEAD_LEN:_HEAD_LEN + length]
    try:
        return cmd, AESGCM(key).decrypt(body[:12], body[12:], frame[4:_HEAD_LEN])
    except Exception as e:  # noqa: BLE001 — InvalidTag: wrong key
        raise SirenError("the siren did not take the key") from e


def _json(plain: bytes) -> dict:
    """The JSON object inside a decrypted payload (after a return code / version header,
    before block padding)."""
    start, end = plain.find(b"{"), plain.rfind(b"}")
    return json.loads(plain[start:end + 1].decode("utf-8")) if 0 <= start < end else {}


class Link:
    """One TCP session with the siren (session key agreed as Tuya 3.5 does)."""

    def __init__(self, ip: str, key: str):
        self.real_key = key.encode("utf-8")
        self.key = self.real_key
        self.seq = 1
        self.sock = socket.create_connection((ip, PORT), timeout=TIMEOUT)
        try:
            self._agree()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, cmd: int, payload: bytes) -> None:
        self.sock.sendall(pack(self.seq, cmd, payload, self.key))
        self.seq += 1

    def _read_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            part = self.sock.recv(n - len(data))
            if not part:
                raise SirenError("the siren closed the connection")
            data += part
        return data

    def _receive(self):
        head = self._read_exact(_HEAD_LEN)
        length = struct.unpack(_HEAD, head)[4]
        return unpack(head + self._read_exact(length + 4), self.key)

    def _agree(self) -> None:
        local = os.urandom(16)
        self._send(_NEG_START, local)
        try:
            cmd, plain = self._receive()
        except (OSError, SirenError) as e:     # a siren given the wrong key just hangs up
            raise SirenError("no answer to the handshake; is the key right?") from e
        reply = plain[4:]                       # return code first
        if cmd != _NEG_RESP or len(reply) < 48:
            raise SirenError("the siren did not answer the handshake")
        remote = reply[:16]
        if not hmac.compare_digest(reply[16:48], hmac.new(self.real_key, local, hashlib.sha256).digest()):
            raise SirenError("the siren did not take the key")
        self._send(_NEG_FINISH, hmac.new(self.real_key, remote, hashlib.sha256).digest())
        mixed = bytes(a ^ b for a, b in zip(local, remote))
        self.key = AESGCM(self.real_key).encrypt(local[:12], mixed, None)[:16]

    def status(self) -> Dict[str, object]:
        """Every DP, from the answer to the query (not a partial update pushed in the meantime)."""
        self._send(_QUERY, b"{}")
        return self._wait_dps(_QUERY)

    def set(self, dps: Dict[str, object]) -> None:
        body = {"protocol": 5, "t": int(time.time()), "data": {"dps": dps}}
        self._send(_CONTROL, _VERSION_HEADER + json.dumps(body, separators=(",", ":")).encode("utf-8"))
        self._wait_dps()

    def _wait_dps(self, cmd: Optional[int] = None) -> Dict[str, object]:
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            got, plain = self._receive()
            if cmd is not None and got != cmd:
                continue
            reply = _json(plain)
            dps = reply.get("dps") or (reply.get("data") or {}).get("dps")
            if dps is not None:
                return dps
        raise SirenError("no answer from the siren")


# -- finding the siren on the LAN -----------------------------------------------------------
def _broadcast_info(packet: bytes) -> dict:
    """The JSON a Tuya device broadcasts: 6699 frame (3.5), 55AA frame or bare ECB (older)."""
    try:
        if packet[:4] == b"\x00\x00\x66\x99":
            return _json(unpack(packet, _UDP_KEY)[1])
        if packet[:4] == b"\x00\x00\x55\xaa":
            packet = packet[20:-8]
        if packet[:1] == b"{":
            return _json(packet)
        dec = Cipher(algorithms.AES(_UDP_KEY), modes.ECB()).decryptor()
        return _json(dec.update(packet) + dec.finalize())
    except Exception:  # noqa: BLE001 — someone else's packet
        return {}


def _own_ip() -> str:
    """This machine's address on the store's network. The route to a private address picks
    the LAN interface (nothing is sent); a network with no gateway at all (a hotspot or
    router with no internet may give none) has no route, so the host's own name is asked."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()
    try:
        names = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        names = []
    lan = [ip for ip in names if not ip.startswith(("127.", "169.254."))]
    return lan[0] if lan else "0.0.0.0"


def _ask_who_is_there() -> None:
    """A 3.5 siren only says where it is when asked: the request the Smart Life app
    broadcasts to UDP 7000. It is also sent to every address of this machine's /24, one by
    one: a phone hotspot (and some routers) drops broadcasts between its clients, but lets
    the same request through when it is addressed to the siren."""
    own = _own_ip()
    packet = pack(0, _REQ_DEVINFO, json.dumps({"from": "app", "ip": own}, separators=(",", ":")).encode("utf-8"),
                  _UDP_KEY)
    targets = ["255.255.255.255"]
    if not own.startswith(("0.", "127.")):
        prefix = own.rsplit(".", 1)[0]
        targets += [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != own]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            try:
                s.sendto(packet, (target, 7000))
            except OSError as e:
                if target == targets[0]:
                    logger.warning("Siren: cannot ask the network who is there: %s", e)
    finally:
        s.close()


def find(device_ids: List[str], seconds: float = FIND_SECONDS, heard: Optional[dict] = None) -> Dict[str, str]:
    """{device id: ip} for the ones heard within `seconds` (asking every ASK_EVERY seconds).
    `heard`, if given, collects every Tuya device that answered: {gwId: {ip, version, port, info}},
    and the whole `seconds` is spent listening so the slower ones are heard too."""
    socks = []
    for port in (6666, 6667, 7000):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            s.bind(("", port))
        except OSError as e:
            logger.warning("Siren: cannot listen on UDP %d: %s", port, e)
            s.close()
            continue
        s.settimeout(0.5)
        socks.append(s)
    found: Dict[str, str] = {}
    deadline = time.time() + seconds
    asked = 0.0
    try:
        while socks and time.time() < deadline and (heard is not None or len(found) < len(set(device_ids))):
            if time.time() - asked >= ASK_EVERY:
                _ask_who_is_there()
                asked = time.time()
            for s in socks:
                try:
                    packet, addr = s.recvfrom(4096)
                except socket.timeout:
                    continue
                info = _broadcast_info(packet)
                if heard is not None and info.get("gwId"):
                    heard.setdefault(info["gwId"], {"ip": info.get("ip") or addr[0], "from": addr[0],
                                                    "version": info.get("version"),
                                                    "port": s.getsockname()[1], "info": info})
                if info.get("gwId") in device_ids:
                    found[info["gwId"]] = info.get("ip") or addr[0]
    finally:
        for s in socks:
            s.close()
    return found


# -- the siren the alert rings -----------------------------------------------------------------
def _short(device_id: str) -> str:
    return "…" + device_id[-4:]


def load_config(path: str = CONFIG) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.error("Siren: %s cannot be read: %s", os.path.basename(path), e)
        return None
    devices = [d for d in cfg.get("devices") or [] if d.get("id") and d.get("key")]
    if not devices:
        return None
    volume = cfg.get("volume", "mute")
    sound = cfg.get("sound", SOUND)
    return {"devices": devices,
            "volume": volume if volume in VOLUMES else "mute",
            "sound": sound if sound in SOUNDS else SOUND,
            "seconds": max(1, min(380, int(cfg.get("seconds", 2))))}


class Siren:
    def __init__(self, config_path: str = CONFIG):
        self.config_path = config_path
        self._lock = threading.Lock()
        self._last_ring = 0.0
        self._ips: Dict[str, str] = {}
        self.last: dict = {}

    def ring_async(self, reason: str) -> None:
        """From the alert: ring once (in the background) unless it rang within COOLDOWN."""
        if time.time() - self._last_ring < COOLDOWN:
            return
        threading.Thread(target=self.ring, args=(reason,), daemon=True, name="siren").start()

    def ring(self, reason: str, force: bool = False) -> dict:
        """Ring every siren in the config; what each one did, also kept in self.last."""
        cfg = load_config(self.config_path)
        if cfg is None:
            return {"rang": False, "why": "no siren set up"}
        if not self._lock.acquire(blocking=False):
            return {"rang": False, "why": "already ringing"}
        try:
            if not force and time.time() - self._last_ring < COOLDOWN:
                return {"rang": False, "why": "rang less than 5 minutes ago"}
            self._last_ring = time.time()
            volume = "mute" if server_feed.shadow() else cfg["volume"]
            missing = [d["id"] for d in cfg["devices"] if not (d.get("ip") or self._ips.get(d["id"]))]
            if missing:
                self._ips.update(find(missing))
            results = [self._ring_one(d, volume, cfg["seconds"], cfg["sound"]) for d in cfg["devices"]]
            self.last = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason,
                         "volume": volume, "seconds": cfg["seconds"], "sound": cfg["sound"], "sirens": results}
            return {"rang": any(r["ok"] for r in results), **self.last}
        finally:
            self._lock.release()

    def _ring_one(self, device: dict, volume: str, seconds: int, sound: str = SOUND) -> dict:
        name = _short(device["id"])
        ip = device.get("ip") or self._ips.get(device["id"])
        if not ip:
            logger.error("Siren %s: not heard on this network", name)
            return {"siren": name, "ok": False, "why": "not found on this network"}
        link = None
        try:
            link = Link(ip, device["key"])
            link.set({DP_VOLUME: volume, DP_SECONDS: seconds})
            link.set({DP_SOUND: sound, DP_RINGING: True})
            dps = link.status()
            ok = dps.get(DP_VOLUME) == volume and dps.get(DP_SECONDS) == seconds
            logger.warning("Siren %s: rang (volume %s, %d s, sound %s)%s", name, volume, seconds, sound,
                           "" if ok and dps.get(DP_SOUND) == sound else
                           f"; it reports volume {dps.get(DP_VOLUME)}, {dps.get(DP_SECONDS)} s, sound {dps.get(DP_SOUND)}")
            return {"siren": name, "ok": ok, "volume": dps.get(DP_VOLUME), "seconds": dps.get(DP_SECONDS),
                    "sound": dps.get(DP_SOUND), "ringing": dps.get(DP_RINGING)}
        except (OSError, SirenError, ValueError) as e:
            self._ips.pop(device["id"], None)   # look for it afresh next time
            logger.error("Siren %s: %s", name, e)
            return {"siren": name, "ok": False, "why": str(e) or type(e).__name__}
        finally:
            if link is not None:
                link.close()


SIREN = Siren()


# -- what an intrusion alert does with it -------------------------------------------------------
# The siren rings for an intrusion the admin server has taken, and only for one the second
# look did not mark UNSURE (user's decision: a sure intruder, never a maybe). Whether it rang
# goes back to the server as its own event; what goes out is "rang" or "failed" and one word
# of why. No key, no device id, no address, here or in the log.
REASONS = ("ok", "not_found", "key", "unreachable", "no_confirm", "error")
_NO_REPORT = {"no_siren", "cooldown"}   # nothing was tried: nothing to tell the server

_last: Dict[str, str] = {}              # what GET /siren shows
_last_lock = threading.Lock()


def confident(note: str = "", caption: str = "") -> bool:
    """Is this intrusion alert sure enough to ring? Not when the second look marked it UNSURE."""
    return "UNSURE" not in f"{note or ''} {caption or ''}".upper()


def _why_code(why: str) -> str:
    """One word for what went wrong, out of REASONS. The message itself is never reported:
    it can carry the siren's own words about the key."""
    why = (why or "").lower()
    if "not found" in why or "not heard" in why:
        return "not_found"
    if "key" in why:
        return "key"
    if any(word in why for word in ("no answer", "closed the connection", "timed out", "timeout",
                                    "refused", "unreachable", "errno", "no route", "3.5 frame")):
        return "unreachable"
    if not why:
        return "no_confirm"
    return "error"


def outcome(result: dict) -> tuple:
    """(what to report, reason) for one ring: ("rang"|"failed", code), or (None, code) for
    a ring that was never attempted (no siren set up, or it rang minutes ago)."""
    result = result or {}
    if result.get("rang"):
        return "rang", "ok"
    why = (result.get("why") or "").lower()
    if "no siren" in why:
        return None, "no_siren"
    if "already ringing" in why or "less than" in why:
        return None, "cooldown"
    failed = [s for s in (result.get("sirens") or []) if not s.get("ok")]
    return "failed", _why_code(failed[0].get("why", "")) if failed else _why_code(why)


def note_ring(state: str, reason: str) -> None:
    with _last_lock:
        _last.update({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "result": state, "reason": reason})


def last_ring() -> dict:
    """The last ring for GET /siren: when, what happened and one word of why. Nothing else."""
    with _last_lock:
        return dict(_last)


def ring_and_report(camera: str, send=None) -> dict:
    """Ring for this camera's alert and tell the server what happened. Blocks (finding the
    siren on the network can take FIND_SECONDS); on_intrusion gives it its own thread."""
    try:
        result = SIREN.ring(camera)
    except Exception:  # noqa: BLE001 — a siren that misbehaves must not stop the watch
        logger.exception("Siren: the ring failed")
        result = {"rang": False, "why": "error"}
    state, reason = outcome(result)
    if state is None:
        return result
    note_ring(state, reason)
    send = send or server_feed.send_event
    try:
        send({"type": "siren", "camera": camera, "result": state, "reason": reason})
    except Exception:  # noqa: BLE001
        logger.exception("Siren: could not report the ring")
    return result


def on_intrusion(camera: str) -> Optional[threading.Thread]:
    """From the intrusion alert, once the server has taken it: ring, in the background.

    Trial mode rings nothing and reports nothing. The watch loop never waits for this."""
    if server_feed.shadow():
        return None
    thread = threading.Thread(target=ring_and_report, args=(camera,), daemon=True, name="siren")
    thread.start()
    return thread
