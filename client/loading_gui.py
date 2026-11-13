# The "Starting AI Camera" window. The .app's launcher (Contents/MacOS/AICameraLauncher)
# starts it before anything else so a double-click shows something at once; a branch
# installed by git clone gets it from client_logic.py instead. Both run .loading_gui.py,
# which only imports this module, so the build can ship it compiled.
#
# bootstrap.py reports each step into the status file (JSON: state, text, port,
# progress), so the window says what is happening instead of an endless spinner.
import json
import subprocess
import tkinter as tk
import tkinter.font as tkfont
import sys
import socket
import time
import threading
import os

import runtime_paths

_SUPPORT_DIR = os.path.expanduser('~/Library/Application Support/AICamera')
_STATUS_FILE = runtime_paths.STARTUP_STATUS_FILE
_LOGS_DIR = os.path.join(_SUPPORT_DIR, 'logs')
_STARTED_AT = time.time()
# Past this with the UI still not answering, say so and offer the logs, but keep
# waiting: a first install can legitimately take this long.
_SLOW_AFTER_S = 600

# Colours taken from the design reference.
BG = '#FAFAFA'
INK = '#1E1E1E'
INK_LIGHT = '#D7D7D7'   # the "TECH" half of the wordmark
MUTED = '#6B6B6B'
TRACK = '#F2F2F2'
FILL = '#5C77FF'
WARN = '#B45309'
ERROR = '#C81E1E'

WIN_W, WIN_H = 480, 196
BAR_W, BAR_H = 380, 8

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
# The new app icon: the .app's own AppIcon.icns, or the repo's MyIcon.icns on a
# branch installed by git clone.
_ICON_CANDIDATES = (os.path.join(_APP_DIR, '..', 'AppIcon.icns'),
                    os.path.join(_APP_DIR, 'MyIcon.icns'))


def _setup_macos_app():
    # Tk always puts this process in the Dock while it starts (it does so inside
    # tk.Tk(), before any of our code can stop it), so rather than a tile that
    # flashes and vanishes, it stays for as long as this window is up.
    #
    # The .app launches this through "AICamera Startup.app" (Contents/Helpers), whose
    # Info.plist gives the tile the AICamera name and icon from its very first frame.
    # Setting the icon here as well covers a git-clone branch, which runs plain Python
    # and would otherwise show Tk's default picture.
    #
    # Pinned to the light look: the content is drawn in fixed light colours, and under
    # Dark Mode macOS would otherwise paint the title bar black above it.
    try:
        import AppKit
        app = AppKit.NSApplication.sharedApplication()
        app.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        for path in _ICON_CANDIDATES:
            if os.path.exists(path):
                image = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
                if image is not None:
                    app.setApplicationIconImage_(image)
                break
    except Exception:
        pass


root = tk.Tk()
_setup_macos_app()
root.configure(bg=BG)  # the right colour from the very first frame, never Tk's default
root.withdraw()  # build fully off-screen first — showing mid-layout on macOS
                 # causes a half-rendered/white-flash frame before the first full paint
root.title("Starting AI Camera")
root.resizable(False, False)
root.attributes('-topmost', True)

x = int((root.winfo_screenwidth() / 2) - (WIN_W / 2))
y = int((root.winfo_screenheight() / 2) - (WIN_H / 2))
root.geometry(f'{WIN_W}x{WIN_H}+{x}+{y}')


def _family():
    # The macOS system font (SF). Tk resolves it by this name even though it is
    # missing from tkfont.families(), so ask TkDefaultFont what it really uses.
    try:
        return tkfont.nametofont('TkDefaultFont').actual()['family']
    except Exception:
        return 'Helvetica Neue'


FAMILY = _family()


def _activate_macos():
    try:
        import AppKit
        NSApp = AppKit.NSApplication.sharedApplication()
        NSApp.activateIgnoringOtherApps_(True)
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(os.getpid())
        if app is not None:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    except Exception:
        pass


def _force_front(attempt=0):
    # Window is already shown (deiconified once, after layout finished) —
    # just bring it to front, don't deiconify again on every retry, since
    # that redundant re-show was contributing to the render flash.
    try:
        _activate_macos()
        root.lift()
        root.attributes('-topmost', True)
        root.focus_force()
    except tk.TclError:
        return
    if attempt < 4:
        root.after(300, lambda: _force_front(attempt + 1))


root.after(10, _force_front)

# --- the ALITECH wordmark: "ALI" in ink, "TECH" in light grey, letter-spaced ---
_mark_font = tkfont.Font(root, family=FAMILY, size=23, weight='bold')
_TRACKING = 3
_mark = tk.Canvas(root, width=WIN_W, height=34, bg=BG, highlightthickness=0, bd=0)
_mark.pack(pady=(26, 0))
_parts = [(ch, INK) for ch in "ALI"] + [(ch, INK_LIGHT) for ch in "TECH"]
_total = sum(_mark_font.measure(ch) for ch, _ in _parts) + _TRACKING * (len(_parts) - 1)
_cx = (WIN_W - _total) / 2
for ch, colour in _parts:
    _mark.create_text(_cx, 17, text=ch, anchor='w', font=_mark_font, fill=colour)
    _cx += _mark_font.measure(ch) + _TRACKING

title_lbl = tk.Label(root, text="Starting AI Camera", font=(FAMILY, 16, 'bold'), fg=INK, bg=BG)
title_lbl.pack(pady=(12, 0))

status_lbl = tk.Label(root, text="Preparing your workspace...", font=(FAMILY, 12), fg=MUTED,
                      bg=BG, wraplength=BAR_W, justify='center')
status_lbl.pack(pady=(0, 18))

# --- progress bar: a rounded 8px track with the blue fill drawn over it ---
bar = tk.Canvas(root, width=BAR_W, height=BAR_H, bg=BG, highlightthickness=0, bd=0)
bar.pack()


def _pill(x0, x1, colour, tag):
    r = BAR_H / 2
    bar.delete(tag)
    if x1 - x0 < BAR_H:
        x1 = x0 + BAR_H
    bar.create_oval(x0, 0, x0 + BAR_H, BAR_H, fill=colour, outline=colour, tags=tag)
    bar.create_oval(x1 - BAR_H, 0, x1, BAR_H, fill=colour, outline=colour, tags=tag)
    bar.create_rectangle(x0 + r, 0, x1 - r, BAR_H, fill=colour, outline=colour, tags=tag)


_pill(0, BAR_W, TRACK, 'track')

# Where bootstrap says it is (0..1), and where the fill is drawn. The fill eases
# toward the target and, between reports, keeps creeping a little so a long step
# never looks frozen — but never past 95% until the UI actually answers.
_target = 0.08
_shown = 0.0


def _draw_fill():
    _pill(0, max(BAR_H, BAR_W * _shown), FILL, 'fill')


def _animate():
    global _shown, _target
    if _shown < _target:
        _shown = min(_target, _shown + max(0.004, (_target - _shown) * 0.12))
    elif _target < 0.95:
        _target = min(0.95, _target + 0.0006)
    _draw_fill()
    root.after(30, _animate)


_draw_fill()

btn_row = tk.Frame(root, bg=BG)
btn_row.pack(pady=(14, 0))


def _open_logs():
    subprocess.Popen(['open', _LOGS_DIR])


_log_btn = tk.Button(btn_row, text="Open logs folder", command=_open_logs, highlightbackground=BG)
_close_btn = tk.Button(btn_row, text="Close", command=lambda: root.destroy(), highlightbackground=BG)

# Everything is laid out now — force Tk to finish drawing before the window
# ever becomes visible, so the first frame the user sees is already complete.
root.update_idletasks()
# Shown fully transparent, painted, then faded in: macOS draws the first frame
# of a new Tk window before its widgets, which read as a blank half-window.
root.attributes('-alpha', 0.0)
root.deiconify()
root.update()
root.after(120, lambda: root.attributes('-alpha', 1.0))
root.after(30, _animate)

_port_ready = threading.Event()

# Handed down by whoever launched this window (client_logic.py), or reported by
# bootstrap.py through the status file — either way this process never opens a
# database connection of its own.
try:
    frontend_port = int(os.environ.get('AICAMERA_FRONTEND_PORT') or 3000)
except ValueError:
    frontend_port = 3000


def _read_status():
    # The newer of the two: a launcher from an older install (the .app's
    # Contents/MacOS is not replaced by an update) still writes its crash
    # message to the old App Support path.
    best = None
    for path in (_STATUS_FILE, runtime_paths.LEGACY_STATUS_FILE):
        try:
            # A file left by an earlier run is not about this one. The launcher deletes
            # it before starting this window; this guards the git-clone path.
            mtime = os.path.getmtime(path)
            if mtime < _STARTED_AT - 2 or (best and mtime <= best[0]):
                continue
            with open(path, 'r', encoding='utf-8') as f:
                best = (mtime, json.load(f))
        except (OSError, ValueError):
            continue
    return best[1] if best else None


def _poll_port_thread():
    while True:
        try:
            s = socket.create_connection(('127.0.0.1', frontend_port), timeout=0.5)
            s.close()
            time.sleep(1)
            _port_ready.set()
            return
        except OSError:
            time.sleep(0.5)


def _show_problem(text):
    bar.pack_forget()
    status_lbl.configure(text=text, fg=ERROR)
    _log_btn.pack(side='left', padx=4)
    _close_btn.pack(side='left', padx=4)


_problem_shown = False
_closing = False


def _finish():
    # Fill the bar the rest of the way, then close. Browser launch is handled
    # by client_logic.py.
    global _target
    _target = 1.0
    if _shown >= 0.999:
        root.destroy()
        sys.exit(0)
    root.after(40, _finish)


def _check_flag():
    global frontend_port, _problem_shown, _target, _closing
    if _closing:
        return
    status = _read_status() or {}
    state = status.get('state')
    if status.get('port'):
        try:
            frontend_port = int(status['port'])
        except (TypeError, ValueError):
            pass
    try:
        reported = float(status.get('progress') or 0)
        if reported > _target:
            _target = min(0.95, reported)
    except (TypeError, ValueError):
        pass

    if state == 'error':
        if not _problem_shown:
            _problem_shown = True
            _show_problem(status.get('text') or "The app could not start.")
        return  # stays up until the user closes it
    if state == 'already_running':
        # Double-clicked while it was already up: show the web page it serves.
        subprocess.Popen(['open', f'http://127.0.0.1:{frontend_port}/login'])
        status_lbl.configure(text=status.get('text') or "Already running.")
        _closing = True
        root.after(2500, root.destroy)
        return
    if _port_ready.is_set():
        _closing = True
        status_lbl.configure(text="Ready")
        _finish()
        return

    if status.get('text') and not _problem_shown:
        status_lbl.configure(text=status['text'])
    if not _problem_shown and time.time() - _STARTED_AT > _SLOW_AFTER_S:
        _problem_shown = True
        _log_btn.pack(side='left', padx=4)
        status_lbl.configure(text="This is taking longer than usual. Still waiting... "
                                  "If it stays stuck, send the logs folder to support.",
                             fg=WARN)
    root.after(300, _check_flag)


t = threading.Thread(target=_poll_port_thread, daemon=True)
t.start()
root.after(300, _check_flag)

root.bind('<Escape>', lambda e: root.destroy())
root.mainloop()
