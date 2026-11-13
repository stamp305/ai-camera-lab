import tkinter as tk
import tkinter.font as tkfont
import os
import shutil
import time
import threading
import sys
import subprocess
import webbrowser
import json
import tempfile
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------------------
# Relaunching the .app
# --------------------------------------------------------------------------------------
APP_SUPPORT_DIR = os.path.expanduser('~/Library/Application Support/AICamera')
import runtime_paths


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _running_pid():
    """The bootstrap PID recorded in the pid file, or None when it is not alive.

    The old App Support path too: the instance being replaced may be a version
    that still writes it there.
    """
    for path in (runtime_paths.PID_FILE, runtime_paths.LEGACY_PID_FILE):
        try:
            with open(path, 'r') as f:
                pid = int(f.read().strip())
            if _pid_alive(pid):
                return pid
        except (OSError, ValueError):
            continue
    return None


def relaunch_app_bundle(app_bundle_path, timeout=90):
    """Bring the app back up, and check that it really came back.

    A blind `time.sleep(3)` used to stand in for this. Stopping every service
    takes longer than that (five seconds is normal, longer on a busy store), and
    `open` on an app LaunchServices still considers running only activates the
    dying copy instead of starting a new one. The restart then did nothing at
    all: services down, no new process, and the browser left forever on "the
    system is up again" — with nothing in any log to say why.
    """
    deadline = time.time() + timeout
    old_pid = _running_pid()
    while old_pid is not None and time.time() < deadline:
        print(f"[UPDATER] Waiting for the previous instance (PID {old_pid}) to exit...")
        time.sleep(1)
        if not _pid_alive(old_pid):
            break
        old_pid = _running_pid()
    # Give LaunchServices a moment to notice the process is gone before asking
    # it to start another one.
    time.sleep(2)

    for attempt in range(1, 4):
        print(f"[UPDATER] Restarting App Bundle via open (attempt {attempt}): {app_bundle_path}")
        subprocess.Popen(["open", "-n", app_bundle_path])
        for _ in range(20):
            time.sleep(1)
            pid = _running_pid()
            if pid is not None and pid != old_pid:
                print(f"[UPDATER] Back up as PID {pid}.")
                return True
    print("[UPDATER] The app did not come back. Start AICamera from Applications by hand.")
    return False

TEMP_UPDATE_DIR = os.path.join(ROOT_DIR, "temp_update")

# Paths an incoming main update may NEVER touch (machine-local state,
# bundled binaries, secrets). Everything else — including core .py files and
# frontend_web/ — is fair game: the client process has already exited by the
# time this updater copies files, and every overwrite is backed up first.
EXCLUSION_LIST = {
    "client_config.json",
    "client_version.json",
    # Read from temp_update below, never copied: the version a payload carries
    # is compiled into _build_info.so, and a plain file in the app is one a
    # shop could edit.
    "VERSION",
    ".env",
    "_secrets.py",
    "temp_update",
    "managed_services",
    "logs",
    ".git",
    "data_service/app.db",
    "python_env",
    "bin",
    # DLC ที่ส่งให้สาขาแล้ว — เครื่องที่รันแบบ git clone เก็บ payload ไว้ที่
    # dlc_local/ (ไม่ใช่ managed_services/ ซึ่งเป็นที่ของตัวแอพ) ถ้าอัปเดตลบทิ้ง
    # ต้องส่ง DLC ใหม่ทั้งชุดทุกครั้ง
    "dlc_local",
    # รูปที่ร้านอัปโหลดเอง (รูป Floorplan) — เป็นข้อมูลของสาขา ไม่ใช่ซอร์ส
    # (static_path_config.py: STATIC_DIR -> IMAGE_DIR -> FLOORPLAN_IMAGE_DIR)
    "data_service/static",
    # โมเดลที่โหลดมาเก็บไว้แล้ว 327 MB — โหลดใหม่ทีนึงกินเวลาและต้องมีเน็ต
    "ViT-FaceMask-Finetuned",
}

BACKUP_DIR = None


def _atomic_copy(src, dest):
    """Put ``src`` at ``dest`` without ``dest`` ever being half a file.

    ``shutil.copy2`` truncates the destination and then streams into it, so a
    crash / power cut / forced reboot in the middle leaves the branch with a
    file that is neither the old version nor the new one — and the updater has
    already exited by then, so nothing ever repairs it. A .so or .py in that
    state stops the app from starting at all. Writing a sibling temp file and
    ``os.replace``-ing it in makes the swap a single rename: after any crash
    the file on disk is one whole version or the other.
    """
    dest_dir = os.path.dirname(dest) or "."
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".update-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            shutil.copyfileobj(f, out)
            out.flush()
            os.fsync(out.fileno())
        # copy2 semantics: mode bits matter here (the executable bit on the
        # helper scripts the app shells out to).
        shutil.copystat(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path, text):
    """Same guarantee as _atomic_copy for a small file we generate ourselves."""
    dest_dir = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".update-", suffix=".part")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def _backup_path():
    global BACKUP_DIR
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    BACKUP_DIR = os.path.join(ROOT_DIR, f"backup_{ts}")
    return BACKUP_DIR

def is_excluded(rel_path):
    if rel_path in EXCLUSION_LIST:
        return True
    path_parts = rel_path.split(os.sep)
    if path_parts[0] in EXCLUSION_LIST:
        return True
    # A multi-segment entry such as "data_service/static" only matched the
    # folder itself, never a file inside it — so the pictures the shop
    # uploaded were still deletable. Match the whole subtree instead.
    for excluded in EXCLUSION_LIST:
        if os.sep in excluded and rel_path.startswith(excluded + os.sep):
            return True
    if rel_path.startswith('backup_') or rel_path.startswith('.env'):
        return True
    # Model weights / runtime assets ship via separate channels, never via
    # the main-update tree — a DELETE_MANIFEST must not wipe them either.
    if rel_path.endswith(('.db', '.sqlite', '.task', '.pt', '.pth', '.onnx',
                          '.engine', '.mlpackage', '.zip')):
        return True
    return False

def _backup_file(rel_path):
    src = os.path.join(ROOT_DIR, rel_path)
    dst = os.path.join(BACKUP_DIR, rel_path)
    if os.path.exists(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

def _restore_backup():
    if not BACKUP_DIR or not os.path.exists(BACKUP_DIR):
        return
    print("[UPDATER] กำลังกู้คืนไฟล์จาก backup...")
    for dirpath, _, filenames in os.walk(BACKUP_DIR):
        for file in filenames:
            rel = os.path.relpath(os.path.join(dirpath, file), BACKUP_DIR)
            dst = os.path.join(ROOT_DIR, rel)
            _atomic_copy(os.path.join(dirpath, file), dst)
    print("[UPDATER] กู้คืนสำเร็จ")

def _cleanup_backup():
    global BACKUP_DIR
    if BACKUP_DIR and os.path.exists(BACKUP_DIR):
        try:
            shutil.rmtree(BACKUP_DIR)
            BACKUP_DIR = None
        except Exception as e:
            print(f"[UPDATER] ลบ backup ไม่สำเร็จ: {e}")

def _payload_has_build_info():
    return any(n.startswith("_build_info.") and n.endswith(".so")
               for n in os.listdir(TEMP_UPDATE_DIR)) if os.path.isdir(TEMP_UPDATE_DIR) else False


def _write_version(version_str):
    """Record the installed version.

    A payload from the build farm carries it compiled in (_build_info.so), which
    the client reads first, so nothing is written and the plain files go. A
    payload without one (an older builder) falls back to client_version.json —
    and a _build_info.so left by an earlier update is removed, or its stale
    number would win.
    """
    if not version_str:
        return
    if _payload_has_build_info():
        if "/AICamera.app/" in os.path.realpath(ROOT_DIR) + "/":
            for name in ("client_version.json", "VERSION"):
                try:
                    os.remove(os.path.join(ROOT_DIR, name))
                except OSError:
                    pass
        print(f"[UPDATER] version {version_str} (compiled into _build_info)")
        return
    for n in os.listdir(ROOT_DIR):
        if n.startswith("_build_info.") and n.endswith(".so"):
            try:
                os.remove(os.path.join(ROOT_DIR, n))
            except OSError:
                pass
    vpath = os.path.join(ROOT_DIR, "client_version.json")
    try:
        _atomic_write_text(vpath, json.dumps({"version": version_str}))
        print(f"[UPDATER] อัปเดต version เป็น {version_str}")
    except Exception as e:
        print(f"[UPDATER] เขียน version ไม่สำเร็จ: {e}")

def _process_delete_manifest():
    manifest_path = os.path.join(TEMP_UPDATE_DIR, "DELETE_MANIFEST.json")
    if not os.path.exists(manifest_path):
        return
    try:
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"[UPDATER] อ่าน DELETE_MANIFEST ไม่สำเร็จ: {e}")
        return
    files = manifest.get("files", [])
    folders = manifest.get("folders", [])
    for rel in files:
        # Safety net: a bad/manifest-injection must never delete machine-local
        # state, secrets or databases even if it lists them explicitly.
        if is_excluded(rel):
            print(f"[UPDATER] ข้ามการลบไฟล์ที่ถูกป้องกัน: {rel}")
            continue
        target = os.path.join(ROOT_DIR, rel)
        if os.path.exists(target):
            try:
                os.remove(target)
                print(f"[UPDATER] ลบไฟล์: {rel}")
            except Exception as e:
                print(f"[UPDATER] ลบไฟล์ {rel} ไม่สำเร็จ: {e}")
    for rel in folders:
        if is_excluded(rel):
            print(f"[UPDATER] ข้ามการลบโฟลเดอร์ที่ถูกป้องกัน: {rel}")
            continue
        target = os.path.join(ROOT_DIR, rel)
        if os.path.isdir(target):
            try:
                shutil.rmtree(target)
                print(f"[UPDATER] ลบโฟลเดอร์: {rel}")
            except Exception as e:
                print(f"[UPDATER] ลบโฟลเดอร์ {rel} ไม่สำเร็จ: {e}")

# The window looks like the "Starting AI Camera" one (loading_gui.py), which comes up
# right after it when the app restarts: same colours, wordmark and rounded bar.
BG = '#FAFAFA'
INK = '#1E1E1E'
INK_LIGHT = '#D7D7D7'
MUTED = '#6B6B6B'
TRACK = '#F2F2F2'
FILL = '#5C77FF'
ERROR = '#C81E1E'
WIN_W, WIN_H = 480, 176
BAR_W, BAR_H = 380, 8


def _light_macos_window():
    # Drawn in fixed light colours: under Dark Mode macOS would otherwise paint the
    # title bar black above them (loading_gui does the same).
    try:
        import AppKit
        app = AppKit.NSApplication.sharedApplication()
        app.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
    except Exception:
        pass


class UpdaterApp:
    def __init__(self, root):
        self.root = root
        _light_macos_window()
        root.configure(bg=BG)
        root.title("Updating AI Camera")
        root.resizable(False, False)
        root.lift()
        root.attributes('-topmost', True)
        x = int((root.winfo_screenwidth() / 2) - (WIN_W / 2))
        y = int((root.winfo_screenheight() / 2) - (WIN_H / 2))
        root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")
        try:
            family = tkfont.nametofont('TkDefaultFont').actual()['family']
        except Exception:
            family = 'Helvetica Neue'

        # The ALITECH wordmark: "ALI" in ink, "TECH" in light grey, letter-spaced.
        mark_font = tkfont.Font(root, family=family, size=23, weight='bold')
        mark = tk.Canvas(root, width=WIN_W, height=34, bg=BG, highlightthickness=0, bd=0)
        mark.pack(pady=(26, 0))
        parts = [(ch, INK) for ch in "ALI"] + [(ch, INK_LIGHT) for ch in "TECH"]
        cx = (WIN_W - sum(mark_font.measure(ch) for ch, _ in parts) - 3 * (len(parts) - 1)) / 2
        for ch, colour in parts:
            mark.create_text(cx, 17, text=ch, anchor='w', font=mark_font, fill=colour)
            cx += mark_font.measure(ch) + 3

        self.label_title = tk.Label(root, text="Updating AI Camera", font=(family, 16, 'bold'), fg=INK, bg=BG)
        self.label_title.pack(pady=(12, 0))
        self.label_status = tk.Label(root, text="Please wait and keep this computer on...", font=(family, 12),
                                     fg=MUTED, bg=BG, wraplength=BAR_W, justify='center')
        self.label_status.pack(pady=(0, 18))
        self.bar = tk.Canvas(root, width=BAR_W, height=BAR_H, bg=BG, highlightthickness=0, bd=0)
        self.bar.pack()
        self._pill(BAR_W, TRACK, 'track')
        self._target = 0.0
        self._shown = 0.0
        self._animate()
        self.update_thread = threading.Thread(target=self.perform_update)
        self.update_thread.daemon = True
        self.update_thread.start()

    def perform_update(self):
        need_rollback = False
        try:
            print("[UPDATER] เริ่มกระบวนการอัพเดตระบบอัตโนมัติ...")
            time.sleep(2)
            if not os.path.exists(TEMP_UPDATE_DIR):
                self.update_status("No update files found.", 100)
                time.sleep(2)
                self.finish_update()
                return
            total_files = sum(len(files) for _, _, files in os.walk(TEMP_UPDATE_DIR))
            if total_files == 0:
                self.update_status("No update files found.", 100)
                time.sleep(2)
                self.finish_update()
                return
            backup_path = _backup_path()
            # Arm rollback BEFORE the first copy so a failure mid-copy also
            # restores (previously it was armed only after the whole loop).
            need_rollback = True
            copied_files = 0
            for root_dir, dirs, files in os.walk(TEMP_UPDATE_DIR):
                for file in files:
                    src_path = os.path.join(root_dir, file)
                    rel_path = os.path.relpath(src_path, TEMP_UPDATE_DIR)
                    dest_path = os.path.join(ROOT_DIR, rel_path)
                    if is_excluded(rel_path):
                        continue
                    _backup_file(rel_path)
                    print(f"[UPDATER] กำลังคัดลอกไฟล์: {rel_path}...")
                    _atomic_copy(src_path, dest_path)
                    copied_files += 1
                    percent = int((copied_files / total_files) * 100)
                    print(f"[UPDATER] ความคืบหน้า: {percent}%")
                    self.update_status("Installing the new version...", percent)
                    time.sleep(0.05)
            _process_delete_manifest()
            version_file_path = os.path.join(TEMP_UPDATE_DIR, "VERSION")
            if os.path.exists(version_file_path):
                with open(version_file_path, 'r') as f:
                    _write_version(f.read().strip())
            self.update_status("Update installed. Starting AI Camera again...", 100)
            try:
                shutil.rmtree(TEMP_UPDATE_DIR)
                print("[UPDATER] ลบ temp_update เรียบร้อย")
            except Exception as e:
                print(f"[UPDATER] ลบโฟลเดอร์ชั่วคราวไม่สำเร็จ: {e}")
            _cleanup_backup()
            need_rollback = False
            time.sleep(1)
        except Exception as e:
            print(f"[UPDATER] เกิดข้อผิดพลาด: {e}")
            if need_rollback:
                _restore_backup()
                _cleanup_backup()
            self.update_status(f"Error: {e}. The previous version was kept.", 0)
            time.sleep(3)
        finally:
            self.finish_update()

    def update_status(self, text, percent):
        self.root.after(0, self._update_status_ui, text, percent)

    def _update_status_ui(self, text, percent):
        failed = percent == 0 and text.startswith("Error")
        self.label_status.config(text=text, fg=ERROR if failed else MUTED)
        self._target = max(0.0, min(1.0, percent / 100))
        if failed:
            self._shown = 0.0

    def _pill(self, width, colour, tag):
        # A rounded 8px bar: two end caps and the rectangle between them.
        r = BAR_H / 2
        width = max(BAR_H, width)
        self.bar.delete(tag)
        self.bar.create_oval(0, 0, BAR_H, BAR_H, fill=colour, outline=colour, tags=tag)
        self.bar.create_oval(width - BAR_H, 0, width, BAR_H, fill=colour, outline=colour, tags=tag)
        self.bar.create_rectangle(r, 0, width - r, BAR_H, fill=colour, outline=colour, tags=tag)

    def _animate(self):
        # Eases toward the reported progress so the bar moves smoothly between files.
        if self._shown < self._target:
            self._shown = min(self._target, self._shown + max(0.004, (self._target - self._shown) * 0.15))
        self._pill(BAR_W * self._shown, FILL, 'fill')
        self.root.after(30, self._animate)

    def finish_update(self):
        self.root.after(0, self._finish_update_ui)

    def _finish_update_ui(self):
        self.root.destroy()
        print("[UPDATER] กำลังเริ่มระบบใหม่...")
        
        # Check if running inside macOS App Bundle
        app_bundle_path = os.path.abspath(os.path.join(ROOT_DIR, "../../.."))
        if app_bundle_path.lower().endswith(".app"):
            relaunch_app_bundle(app_bundle_path)
        else:
            # Not a bundle: a branch installed by git clone. It is started as
            # `python client.py` (Login Item / LaunchAgent), so restart it the same
            # way. bootstrap.py is the bundle launcher: here it would leave a
            # caffeinate behind on every restart and, with MariaDB not answering on
            # 3306 at that moment, start an empty database of its own.
            main_script = os.path.join(ROOT_DIR, "client.py")
            if os.path.exists(main_script):
                subprocess.Popen([sys.executable, 'client.py'], cwd=ROOT_DIR)
            else:
                subprocess.Popen([sys.executable, 'client_logic.py'], cwd=ROOT_DIR)
        

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--restart-only":
        print("[UPDATER] Restart only mode triggered...")
        app_bundle_path = os.path.abspath(os.path.join(ROOT_DIR, "../../.."))
        if app_bundle_path.lower().endswith(".app"):
            relaunch_app_bundle(app_bundle_path)
        else:
            # Not a bundle: a branch installed by git clone. It is started as
            # `python client.py` (Login Item / LaunchAgent), so restart it the same
            # way. bootstrap.py is the bundle launcher: here it would leave a
            # caffeinate behind on every restart and, with MariaDB not answering on
            # 3306 at that moment, start an empty database of its own.
            main_script = os.path.join(ROOT_DIR, "client.py")
            new_env = os.environ.copy()
            new_env['AICAMERA_RESTARTED'] = '1'
            if os.path.exists(main_script):
                subprocess.Popen([sys.executable, 'client.py'], cwd=ROOT_DIR, start_new_session=True, env=new_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen([sys.executable, 'client_logic.py'], cwd=ROOT_DIR, start_new_session=True, env=new_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(0)
    elif len(sys.argv) > 1 and sys.argv[1] == "--port-change-restart":
        print("[UPDATER] Port change restart mode triggered...")
        app_bundle_path = os.path.abspath(os.path.join(ROOT_DIR, "../../.."))
        if app_bundle_path.lower().endswith(".app"):
            relaunch_app_bundle(app_bundle_path)
        else:
            # Not a bundle: a branch installed by git clone. It is started as
            # `python client.py` (Login Item / LaunchAgent), so restart it the same
            # way. bootstrap.py is the bundle launcher: here it would leave a
            # caffeinate behind on every restart and, with MariaDB not answering on
            # 3306 at that moment, start an empty database of its own.
            main_script = os.path.join(ROOT_DIR, "client.py")
            new_env = os.environ.copy()
            new_env.pop('AICAMERA_RESTARTED', None)
            if os.path.exists(main_script):
                subprocess.Popen([sys.executable, 'client.py'], cwd=ROOT_DIR, start_new_session=True, env=new_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen([sys.executable, 'client_logic.py'], cwd=ROOT_DIR, start_new_session=True, env=new_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(0)
    else:
        root = tk.Tk()
        app = UpdaterApp(root)
        root.mainloop()


if __name__ == "__main__":
    main()