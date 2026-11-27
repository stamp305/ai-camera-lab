"""CCTV Streaming on the branch (live_agent) as the company client wires it.

  * only an ffmpeg that runs and can publish WHIP with the Mac's encoder is used
  * video goes to the host of the admin link the live_start came in on, never to a
    host a message names
  * a dropped link stops only the streams that link asked for
  * client_logic hands each socket's own address to live_agent (real python-socketio)
"""
import os
import stat
import subprocess
import threading
import types

import pytest
import socketio

import live_agent as L
from test_socket_reconnect import load, wait_until

SID = "a" * 32
TOKEN = "T" * 32


def script(tmp_path, name, muxers, encoders="", rc=0):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n"
                 f'case "$2" in -muxers) printf "%s\\n" "{muxers}";; -encoders) printf "%s\\n" "{encoders}";; esac\n'
                 f"exit {rc}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


@pytest.fixture
def only(monkeypatch):
    """Let _tool see only the files given (no Homebrew / PATH ffmpeg of this Mac)."""
    def use(*paths, env=None):
        allowed = set(paths)
        monkeypatch.setattr(L.os.path, "isfile", lambda p: p in allowed)
        monkeypatch.setattr(L.shutil, "which", lambda name: paths[0] if paths else None)
        if env:
            monkeypatch.setenv("AICAM_FFMPEG", env)
        else:
            monkeypatch.delenv("AICAM_FFMPEG", raising=False)
        L._usable.clear()
    return use


GOOD_MUX = " E  whip            WHIP(WebRTC-HTTP ingestion protocol) muxer"
GOOD_ENC = " V....D h264_videotoolbox    VideoToolbox H.264 Encoder"


def test_an_ffmpeg_that_cannot_start_is_passed_over(tmp_path, only):
    broken = script(tmp_path, "broken", "", rc=1)          # e.g. a library missing
    good = script(tmp_path, "good", GOOD_MUX, GOOD_ENC)
    only(good, broken, env=broken)                         # named first, but it fails
    assert L._tool("ffmpeg") == good


def test_an_ffmpeg_without_the_mac_encoder_is_not_used(tmp_path, only):
    no_vt = script(tmp_path, "novt", GOOD_MUX, " V....D libx264")
    only(no_vt)
    assert L._tool("ffmpeg") is None


def test_an_ffmpeg_without_whip_still_serves_the_socket_transport(tmp_path, only):
    old = script(tmp_path, "old", " E  mp4  MP4", GOOD_ENC)
    only(old, env=old)
    assert L._tool("ffmpeg") == old
    assert L._ffmpeg_caps(old) == (True, False)


def test_a_usable_ffmpeg_is_found(tmp_path, only):
    good = script(tmp_path, "good", GOOD_MUX, GOOD_ENC)
    only(good, env=good)
    assert L._tool("ffmpeg") == good


def test_media_host_is_the_link_the_request_came_in_on(monkeypatch):
    monkeypatch.delenv("SOCKET_SERVER_HOST", raising=False)
    assert L._media_host("https://app.example.th") == ("https", "app.example.th")
    with pytest.raises(L.LiveError):
        L._media_host("ftp://app.example.th")


@pytest.fixture
def popen(monkeypatch):
    calls = []

    class P:
        returncode = None
        stderr = iter(())

        def __init__(self, cmd, **kw):
            calls.append(cmd)
            import io
            self.stdout = io.BytesIO(P.feed) if kw.get("stdout") == subprocess.PIPE else None
        feed = b""

        def poll(self):
            return None

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(L.subprocess, "Popen", P)
    monkeypatch.setattr(L, "_tool", lambda name: "/x/ffmpeg")
    monkeypatch.setattr(L, "_rtsp_url", lambda *a: "rtsp://u:p@10.0.0.1:554/cam/realmonitor?channel=1")
    monkeypatch.setattr(L, "_ensure_watchdog", lambda: None)
    monkeypatch.setattr(L, "_ffmpeg_caps", lambda path: (True, True))
    L._streams.clear()
    yield calls
    for s in list(L._streams):
        L._streams.pop(s, None)


def start_msg(sid=SID, **extra):
    return {"sid": sid, "publish_token": TOKEN, "camera": 1, "channel": 2, "subtype": 1,
            "media_port": 443, **extra}


def test_video_goes_to_the_requesting_link_not_to_a_host_in_the_message(popen):
    out = L.start(start_msg(host="evil.example", whip_url="https://evil.example/x"), "https://app.example.th")
    assert out["ok"]
    cmd = popen[-1]
    assert cmd[-1] == f"https://app.example.th:443/s/{SID}/whip"
    assert "evil" not in " ".join(cmd)
    assert "rtsp://" not in " ".join(cmd)                  # the NVR login is in a 0600 file


def test_a_dropped_link_stops_only_its_own_streams(popen):
    L.start(start_msg("a" * 32), "https://one.example")
    L.start(start_msg("b" * 32), "https://two.example")
    L.stop_all("disconnect", origin="https://one.example")
    assert set(L._streams) == {"b" * 32}
    L.stop_all("disconnect")
    assert not L._streams


@pytest.fixture
def admin():
    """A Socket.IO server on loopback that, like the admin, asks the branch to start a stream
    as soon as it connects, then drops the link."""
    import asyncio
    import socket as _socket
    import uvicorn
    sio = socketio.AsyncServer(async_mode="asgi")
    got = {}

    @sio.event
    async def connect(sid, environ):
        async def ask():
            await asyncio.sleep(0.2)
            got["answer"] = await sio.call("live_start", {"sid": SID}, to=sid, timeout=5)
            await sio.disconnect(sid)
        asyncio.ensure_future(ask())

    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(socketio.ASGIApp(sio), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    assert wait_until(lambda: srv.started)
    yield f"http://127.0.0.1:{port}", got
    srv.should_exit = True
    thread.join(5)


def test_client_logic_gives_each_socket_its_own_address(admin):
    """Real python-socketio both ends: the admin's live_start reaches live_agent with this
    link's address, the answer goes back as the ack, and when the link drops this link's
    streams are told to wait for it (detach), not stopped."""
    url, got = admin
    seen, stopped = [], []
    fake = types.SimpleNamespace(
        on_live_start=lambda data, origin=None, emit=None: seen.append(origin) or {"ok": True, "sid": data["sid"]},
        stop_all=lambda why, origin=None: None,
        detach=lambda origin=None: stopped.append(origin),
        reattach=lambda origin=None: None,
        **{name: (lambda data=None: {"ok": True}) for name in (
            "on_live_cameras", "on_live_stop", "on_live_extend", "on_live_resolve", "on_live_status",
            "on_live_record_days", "on_live_record_spans", "on_siren_call")})
    import re as _re
    from test_socket_reconnect import SOURCE
    handlers = set(_re.findall(r"sio\.on\('[a-z_]+', ([A-Za-z_]+)\)", SOURCE)) | {"disconnect", "connect"}
    ns = {name: (lambda *a, **k: None) for name in handlers}
    ns.update(_secrets=types.SimpleNamespace(SOCKET_SERVERS=[{"url": url, "path": "socket.io"}]),
              _ENV_SOCKET_HOST_OVERRIDE=None, _ENV_SOCKET_PATH_OVERRIDE=None, _ENV_SOCKET_PORT_OVERRIDE=None,
              live_agent=fake, lan_probe=None)
    ns = load({"_link_up", "_reconnecting", "MultiSocketClient"}, **ns)
    multi = ns["MultiSocketClient"]()
    multi.setup_clients()
    client = multi.clients[0]
    client.reconnection = False
    client.connect(url, socketio_path="socket.io", wait_timeout=5)
    assert wait_until(lambda: stopped == [url])
    assert seen == [url]
    assert got["answer"] == {"ok": True, "sid": SID}


def test_socket_transport_sends_ts_chunks_on_the_requesting_link(popen):
    sent = []
    L.subprocess.Popen.feed = b"G" * 188 * 400          # what ffmpeg writes to stdout
    out = L.start(start_msg(transport="socket", publish_token=""), "https://app.example.th",
                  lambda ev, payload, callback=None: sent.append((ev, payload)) or (callback and callback()))
    assert out["ok"] and out["transport"] == "socket"
    cmd = popen[-1]
    assert cmd[-3:] == ["-flush_packets", "1", "pipe:1"] and "whip" not in cmd
    assert "rtsp://" not in " ".join(cmd)
    import time as _t
    for _ in range(50):
        if sum(len(p["b"]) for _, p in sent) == 188 * 400:
            break
        _t.sleep(0.02)
    assert {ev for ev, _ in sent} == {"live_chunk"}
    assert all(p["sid"] == SID and len(p["b"]) <= L.CHUNK_BYTES for _, p in sent)
    assert b"".join(p["b"] for _, p in sent) == b"G" * 188 * 400


def test_socket_transport_needs_the_link(popen):
    with pytest.raises(L.LiveError) as e:
        L.start(start_msg(transport="socket"), "https://app.example.th", None)
    assert e.value.code == "no_socket"


def test_webrtc_needs_an_ffmpeg_with_whip(popen, monkeypatch):
    monkeypatch.setattr(L, "_ffmpeg_caps", lambda path: (True, False))
    with pytest.raises(L.LiveError) as e:
        L.start(start_msg(), "https://app.example.th")
    assert e.value.code == "no_whip"


def test_an_unknown_transport_is_refused(popen):
    with pytest.raises(L.LiveError):
        L.start(start_msg(transport="ftp"), "https://app.example.th", lambda *a: None)


def test_a_stream_whose_video_backs_up_is_stopped_and_the_admin_told(popen, monkeypatch):
    """No ack for longer than MAX_BACKLOG: the shop's uplink cannot carry it."""
    monkeypatch.setattr(L, "MAX_BACKLOG", 0.2)
    monkeypatch.setattr(L, "BATCH_BYTES", 188)
    events = []

    class SlowPipe:                       # ffmpeg keeps producing, one TS packet every 50 ms
        def read1(self, n):
            import time as _t
            _t.sleep(0.05)
            return b"G" * 188
    L.subprocess.Popen.feed = b""
    out = L.start(start_msg(transport="socket", publish_token=""), "https://app.example.th",
                  lambda ev, payload, callback=None: events.append(ev))      # never acks
    entry = L._streams[SID]
    entry["proc"].stdout = SlowPipe()
    # the pump thread was started on the empty BytesIO and ended; run one on the slow pipe
    entry["stop"].clear()
    L._pump(SID, entry["proc"], entry, lambda ev, payload, callback=None: events.append(ev))
    assert out["ok"] and entry["err"] == "link_too_slow"
    assert events[-1] == "live_gone" and "live_chunk" in events


def test_an_acked_stream_keeps_going(popen, monkeypatch):
    monkeypatch.setattr(L, "MAX_BACKLOG", 0.2)
    monkeypatch.setattr(L, "BATCH_BYTES", 188)
    n = {"reads": 0}

    class Pipe:
        def read1(self, size):
            import time as _t
            _t.sleep(0.02)
            n["reads"] += 1
            return b"G" * 188 if n["reads"] < 40 else b""      # ~0.8 s of video, then ffmpeg ends
    entry = {"stop": __import__("threading").Event(), "err": "", "proc": types.SimpleNamespace(stdout=Pipe(), poll=lambda: None)}
    events = []
    L._pump(SID, entry["proc"], entry, lambda ev, payload, callback=None: events.append(ev) or (callback and callback()))
    assert entry["err"] == "" and "live_gone" not in events and events.count("live_chunk") >= 30



def test_a_dropped_link_makes_socket_streams_wait_then_go_on(popen):
    L.start(start_msg("a" * 32, transport="socket"), "https://one.example", lambda *a, **k: None)
    L.start(start_msg("b" * 32, transport="socket"), "https://two.example", lambda *a, **k: None)
    assert L.detach("https://one.example") == 1
    assert L._streams["a" * 32]["detached_at"] and not L._streams["b" * 32].get("detached_at")
    assert L.reattach("https://one.example") == 1
    assert not L._streams["a" * 32]["detached_at"]


def test_a_stream_detached_past_the_grace_is_stopped(popen, monkeypatch):
    L.start(start_msg("a" * 32, transport="socket"), "https://one.example", lambda *a, **k: None)
    L.detach("https://one.example")
    L._streams["a" * 32]["detached_at"] -= L.RECONNECT_GRACE + 1
    now = L.time.monotonic()
    with L._lock:
        gone = [s for s, e in L._streams.items() if e.get("detached_at") and now - e["detached_at"] > L.RECONNECT_GRACE]
    assert gone == ["a" * 32]


def test_while_detached_nothing_is_sent_or_piled_up_and_sending_resumes(monkeypatch):
    monkeypatch.setattr(L, "BATCH_BYTES", 188)
    import threading as _th
    reads = {"n": 0}
    entry = {"stop": _th.Event(), "err": "", "detached_at": L.time.monotonic()}
    sent = []

    class Pipe:
        def read1(self, size):
            reads["n"] += 1
            if reads["n"] == 20:
                entry["detached_at"] = None          # the link is back
            return b"G" * 188 if reads["n"] < 40 else b""
    entry["proc"] = types.SimpleNamespace(stdout=Pipe(), poll=lambda: None)
    L._pump(SID, entry["proc"], entry, lambda ev, payload, callback=None: sent.append(payload["b"]) or (callback and callback()))
    assert entry["err"] == ""
    assert 15 <= len(sent) <= 20                      # only what came after the link was back


def test_a_refused_send_waits_like_a_disconnect(monkeypatch):
    monkeypatch.setattr(L, "BATCH_BYTES", 188)
    import threading as _th
    entry = {"stop": _th.Event(), "err": ""}
    calls = {"n": 0}

    def emit(ev, payload, callback=None):
        calls["n"] += 1
        raise RuntimeError("not connected")

    class Pipe:
        n = 0
        def read1(self, size):
            Pipe.n += 1
            return b"G" * 188 if Pipe.n < 10 else b""
    entry["proc"] = types.SimpleNamespace(stdout=Pipe(), poll=lambda: None, terminate=lambda: None)
    L._pump(SID, entry["proc"], entry, emit)
    assert entry.get("detached_at") and entry["err"] == "" and calls["n"] == 1   # tried once, then waited
