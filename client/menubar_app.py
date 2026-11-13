import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pystray
from pystray import Menu, MenuItem as item
from PIL import Image, ImageDraw

import runtime_paths

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# client_logic rewrites the status file every 5 s; older than this means it stopped.
STALE_SECONDS = 30
REFRESH_SECONDS = 5

# Badge colours on the icon: everything fine / partly working / not working / no answer yet.
_GREEN, _AMBER, _RED, _GREY = (52, 199, 89), (255, 159, 10), (255, 59, 48), (142, 142, 147)


def _base_image():
    # The camera logo beside "ALITECH" in the web UI's sidebar; it is in git, so
    # every install has it. icon.png is not in git: only builds that copy one in.
    for icon_path in (os.path.join(APP_DIR, 'frontend_web', 'static', 'images', 'icons', 'logo_aicam.png'),
                      os.path.join(APP_DIR, 'icon.png')):
        if os.path.exists(icon_path):
            return Image.open(icon_path).convert("RGBA").resize((64, 64))
    # The .app has no static/ folder: the UI's files are sealed in assets.bin.
    try:
        import io
        sys.path.insert(0, os.path.join(APP_DIR, 'frontend_web'))
        import assets_vault
        data = assets_vault.read('static/images/icons/logo_aicam.png') if assets_vault.available() else None
        if data:
            return Image.open(io.BytesIO(data)).convert("RGBA").resize((64, 64))
    except Exception:
        pass

    # Fallback to green circle if icon is missing
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=(0, 200, 0))
    return image


_BASE = None


def create_image(colour=_GREY):
    """The logo with a status dot in its lower right corner."""
    global _BASE
    if _BASE is None:
        _BASE = _base_image()
    image = _BASE.copy()
    draw = ImageDraw.Draw(image)
    draw.ellipse((38, 38, 62, 62), fill=(255, 255, 255, 255))
    draw.ellipse((42, 42, 58, 58), fill=colour + (255,))
    return image


def read_status():
    """client_logic's latest status, or None when it is missing or stale."""
    try:
        with open(runtime_paths.APP_STATUS_FILE, encoding="utf-8") as f:
            status = json.load(f)
    except Exception:
        return None
    if time.time() - float(status.get("time") or 0) > STALE_SECONDS:
        return None
    return status


def summarize(status):
    """(colour, [menu lines]) for a status dict from read_status()."""
    if status is None:
        return _GREY, ["Status: starting…"]
    lines = []
    colour = _GREEN
    if not status.get("activated"):
        lines.append("Not activated yet")
        colour = _RED
    if status.get("server"):
        lines.append("Server: connected")
    else:
        lines.append("Server: not connected")
        colour = _RED if colour == _RED else _AMBER
    total = status.get("services_total")
    if total is not None:
        down = status.get("services_down") or []
        lines.append(f"Services: {total - len(down)}/{total} running")
        if down:
            colour = _RED if colour == _RED or len(down) == total else _AMBER
    if status.get("key_halt"):
        lines.append(f"Contact admin: key requests paused ({status['key_halt']})")
        colour = _RED
    return colour, lines


def _confirm(title, message, button):
    """A native yes/no dialog. True only when the user pressed `button`."""
    script = (f'display dialog {json.dumps(message)} with title {json.dumps(title)} '
              f'buttons {{"Cancel", {json.dumps(button)}}} default button "Cancel" with icon caution')
    try:
        return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=300).returncode == 0
    except Exception:
        return False


def _quit_now(icon):
    icon.stop()
    print("Stopping AI Camera processes...")
    ppid = os.getppid()
    try:
        os.kill(ppid, signal.SIGINT)
    except Exception:
        pass
    time.sleep(2)
    os._exit(0)


def on_quit(icon, item):
    # Quitting stops counting for the whole store, so it is never one click.
    def ask():
        if _confirm("Quit AI Camera",
                    "AI Camera will stop recording and counting until it is opened again.",
                    "Quit"):
            _quit_now(icon)
    threading.Thread(target=ask, daemon=True).start()


def on_restart(icon, item):
    def ask():
        if _confirm("Restart AI Camera",
                    "All services restart. Counting pauses for about a minute.",
                    "Restart"):
            import port_manager
            # Same as Restart System on the Service Port Settings page: first every
            # open page switches to the restarting notice (it polls every 3 s), then
            # the restart itself, which opens a fresh window once the app is back.
            try:
                runtime_paths.ensure_run_dir()
                with open(runtime_paths.RESTART_NOTICE_FILE, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
            time.sleep(4)
            port_manager.request_restart(APP_DIR, marker="PORT_CHANGE")
    threading.Thread(target=ask, daemon=True).start()


def _read_frontend_port_from_config():
    # client_logic passes the port it already resolved; reading the port table
    # directly is the fallback for a menubar started on its own in dev.
    from_env = os.environ.get('AICAMERA_FRONTEND_PORT')
    if from_env:
        try:
            return int(from_env)
        except ValueError:
            pass
    try:
        import port_store
        return port_store.get_port('frontend_web', 3000)
    except Exception:
        return 3000


def get_frontend_port():
    return _read_frontend_port_from_config()


def open_ui():
    url = f"http://127.0.0.1:{get_frontend_port()}/login"
    try:
        subprocess.Popen(['open', url])
    except Exception:
        import webbrowser
        webbrowser.open(url)


def launch_browser():
    if os.environ.get('AICAMERA_RESTARTED') == '1':
        print("[MENUBAR] App was restarted via updater. Not opening browser again.")
        return

    port = get_frontend_port()
    print(f"[MENUBAR] Waiting for frontend port {port} to become active...")
    for _ in range(150):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        result = s.connect_ex(('127.0.0.1', port))
        s.close()
        if result == 0:
            # Open the page after the "Starting System" window has said Ready and
            # closed, not ahead of it; 60 s at most in case it stays up on an error.
            for _ in range(120):
                if subprocess.run(["pgrep", "-f", r"\.loading_gui\.py"],
                                  capture_output=True).returncode != 0:
                    break
                time.sleep(0.5)
            print(f"[MENUBAR] Port {port} is active! Launching default browser...")
            # /login, not /system: opening /system directly renders that
            # page's HTML for a frame before the auth check kicks it back to
            # /login, which is the visible flash of the port page on every
            # cold start. Landing on /login instead skips that hop entirely
            # — the login page itself forwards an already-authenticated
            # session onward.
            open_ui()
            break
        time.sleep(0.2)


class _State:
    """What the menu shows; refreshed from the status file by the setup thread."""
    colour = _GREY
    lines = ["Status: starting…"]
    version = ""


def _status_line(n):
    return lambda _item: _State.lines[n] if n < len(_State.lines) else ""


def _build_menu():
    status_items = [item(_status_line(n), None, enabled=False,
                         visible=lambda _item, n=n: n < len(_State.lines)) for n in range(4)]
    return Menu(
        item(lambda _item: f"AI Camera {_State.version}".strip(), None, enabled=False),
        *status_items,
        Menu.SEPARATOR,
        item('Restart Services…', on_restart),
        Menu.SEPARATOR,
        item('Quit AI Camera…', on_quit),
    )


def _on_main_thread(fn):
    """Run `fn` on the AppKit main thread (async), or here on other platforms.

    pystray calls `setup` on a thread of its own, and its macOS backend touches
    NSStatusItem directly from whichever thread changes the icon. macOS 26 let
    that pass; macOS 27 traps on it (BSServiceMainRunLoopQueue
    assertBarrierOnQueue under -[NSStatusItem setMenu:]) and kills the process,
    so the menu bar icon vanished a few seconds after it appeared.
    """
    if sys.platform != "darwin":
        fn()
        return
    from PyObjCTools import AppHelper
    AppHelper.callAfter(fn)


def _refresh_forever(icon):
    _on_main_thread(lambda: setattr(icon, "visible", True))
    while True:
        status = read_status()
        colour, lines = summarize(status)
        new_image = create_image(colour) if colour != _State.colour else None
        version = str(status["version"]) if status and status.get("version") else None

        def apply(new_image=new_image, colour=colour, lines=lines, version=version):
            # _State is read by the menu's text callbacks, which AppKit runs on
            # the main thread too, so it changes here alongside the menu.
            if version:
                _State.version = version
            if new_image is not None:
                icon.icon = new_image
            _State.colour, _State.lines = colour, lines
            icon.update_menu()

        # Compare against the colour about to be applied, not the one already
        # on screen, so a pending update is not queued twice.
        _State.colour = colour
        _on_main_thread(apply)
        time.sleep(REFRESH_SECONDS)


def setup_menubar():
    # Start browser launcher thread
    threading.Thread(target=launch_browser, daemon=True).start()

    icon = pystray.Icon("AICamera", create_image(_GREY), "AI Camera", _build_menu())
    # pystray runs `setup` in its own thread once the menu bar loop is up — the
    # supported place to change the icon and menu from.
    icon.run(setup=_refresh_forever)


def main():
    setup_menubar()


if __name__ == "__main__":
    setup_menubar()
