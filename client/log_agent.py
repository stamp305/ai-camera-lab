"""ตัวจัดการไฟล์ log ฝั่งเครื่องลูกข่าย (branch) สำหรับ Admin server

Admin server ยิง event ผ่าน Socket.IO (`log_list` / `log_read`) แล้วรอ ack
กลับมาเป็น dict ตามที่ฟังก์ชันด้านล่างคืนให้ตรง ๆ (python-socketio ส่ง
return value ของ handler กลับไปเป็น ack โดยอัตโนมัติ)

`list_logs()` และ `read_log()` เป็น pure function รับ `base_dir` เป็นพารามิเตอร์
(แทนที่จะอ้าง APP_SUPPORT_DIR ตรง ๆ ในตัวมันเอง) เพื่อให้เทสต์ยิงใส่ temp dir
ได้โดยไม่ไปยุ่งกับข้อมูลเครื่องจริงใน ~/Library/Application Support/AICamera
ส่วน `on_log_list` / `on_log_read` เป็น handler บาง ๆ ที่ผูกกับ APP_SUPPORT_DIR
จริงไว้ให้ client_logic.py เอาไป register กับ sio ตรง ๆ

Log ที่อ่านได้มีอยู่สองที่ใต้ base_dir:
  - log/   : log ของ DLC services (เช่น camera_service.log) — มีโฟลเดอร์
             monitor_snapshots/ ที่เป็นภาพ ไม่ใช่ log ต้องกันไม่ให้เห็น/อ่านได้
  - logs/  : log ของตัว client เอง (client.log, bootstrap.log และไฟล์ที่
             rotate ออกมา)

ไฟล์หรือโฟลเดอร์ที่ชื่อขึ้นต้นด้วย '.' (hidden เช่น logs/.cache/x.log) ถือเป็น
ของซ่อนเสมอ ไม่ว่าจะซ่อนอยู่ลึกแค่ไหนใต้ root — list_logs() จะไม่แสดงเลย และ
read_log() จะตอบ bad_path แม้จะรู้ path ตรง ๆ แล้วขอเจาะจงอ่านก็ตาม (ไม่ใช่แค่
ซ่อนจากรายการ แต่อ่านไม่ได้ด้วย)
"""
import base64
import json
import logging
import os

logger = logging.getLogger(__name__)

# ค่าตรงกับ APP_SUPPORT_DIR ใน client_logic.py — คัดลอกมาไว้ที่นี่แทนการ
# import client_logic เพราะ client_logic เป็นฝ่าย import โมดูลนี้อยู่แล้ว
# (import ย้อนกลับกันจะกลายเป็น circular import)
APP_SUPPORT_DIR = os.path.expanduser('~/Library/Application Support/AICamera')

# Root ที่อนุญาตให้ list/read เท่านั้น ชื่ออื่นถือว่าไม่รู้จัก (bad_root)
LOG_ROOTS = ("log", "logs")

# โฟลเดอร์ลูกชั้นบนสุดที่ต้องกันไว้ต่อ root — ปัจจุบันมีแค่ log/monitor_snapshots
# (เก็บภาพจากกล้อง ไม่ใช่ log ไฟล์ และมีขนาดใหญ่เกินจะ list/อ่านผ่านช่องทางนี้)
_EXCLUDED_TOP_SUBDIR = {"log": "monitor_snapshots"}
# Only text log files go to the admin. log/ also holds camera pictures outside
# monitor_snapshots (monitor_shadow_reports/<day>/*_intrusion_*.jpg: people in
# the store), and a folder-by-folder exclusion missed them, so this is a list of
# what may go rather than of what may not: a name with ".log" in it (rotated
# client.log.2026-11-13 and set-aside .sealed files included) or a text extension.
_LOG_EXTENSIONS = (".jsonl", ".json", ".txt")


def _is_log_file(name):
    low = name.lower()
    return ".log" in low or low.endswith(_LOG_EXTENSIONS)

MAX_LIST_ENTRIES = 2000
# engineio frame ประมาณ ~1MB และชื่อไฟล์ภาษาไทยโดน JSON escape เป็น \uXXXX ทำให้
# บวมกว่าความยาวจริงหลายเท่า จำกัดขนาด JSON โดยประมาณของ "files" ไว้ไม่ให้เกินนี้
# ด้วย (นอกเหนือจาก cap จำนวนรายการ) กันเฟรมใหญ่เกินจน socket ส่งไม่ผ่าน
MAX_LIST_BYTES = 700_000
MIN_READ_LENGTH = 1
MAX_READ_LENGTH = 262144  # 256 KiB


def _err(code):
    return {"ok": False, "error": code}


def _walk_root(root_dir, root_name):
    """เดินไฟล์ทั้งหมดใต้ root_dir แบบ recursive คืนเป็น list ของ dict

    ข้าม symlink ทั้งไฟล์และโฟลเดอร์ (ไม่ตามลิงก์ออกไปนอก root และไม่เอา
    ไฟล์ที่เป็นลิงก์มาแสดง), ไฟล์และโฟลเดอร์ที่ขึ้นต้นด้วย '.' (hidden — ตัด
    โฟลเดอร์ทิ้งจาก dirnames ก่อน os.walk เดินลงไปเอง เลยไม่โผล่ไม่ว่าจะซ่อน
    อยู่ลึกแค่ไหน ไม่ใช่แค่ชั้นบนสุด), และโฟลเดอร์ลูกชั้นบนสุดที่ถูกกันไว้
    (เช่น monitor_snapshots ใต้ root "log")
    """
    entries = []
    if not os.path.isdir(root_dir) or os.path.islink(root_dir):
        return entries  # root ไม่มีอยู่จริง (หรือเป็น symlink) แค่ไม่มีไฟล์ให้ ไม่ใช่ error

    excluded = _EXCLUDED_TOP_SUBDIR.get(root_name)

    for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root_dir)
        if rel_dir == '.':
            rel_dir = ''

        # ตัดโฟลเดอร์ที่ไม่ต้องการออกจาก dirnames ก่อน os.walk เดินลงไปเอง
        keep = []
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                continue
            # โฟลเดอร์ซ่อน (ชื่อขึ้นต้นด้วย '.') ตัดทิ้งทุกระดับความลึก ไม่ใช่
            # แค่ชั้นบนสุด กันไฟล์ที่อยู่ข้างในหลุดออกมาโผล่ในรายการ (ไฟล์ข้างใน
            # เองอาจไม่ได้ขึ้นต้นด้วย '.' ก็ได้ เช่น logs/.cache/x.log)
            if d.startswith('.'):
                continue
            # casefold กัน macOS ที่ดิสก์ไม่สนตัวพิมพ์เล็ก-ใหญ่ เช่น
            # "Monitor_Snapshots" หรือ "MONITOR_SNAPSHOTS" ก็ต้องโดนกันเหมือนกัน
            if excluded and rel_dir == '' and d.casefold() == excluded.casefold():
                continue
            keep.append(d)
        dirnames[:] = keep

        for fn in filenames:
            if fn.startswith('.') or not _is_log_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel_f = fn if not rel_dir else f"{rel_dir}/{fn}"
            try:
                st = os.stat(full)
            except OSError as e:
                logger.error(f"[LOG AGENT] stat failed for {root_name}/{rel_f}: {type(e).__name__}")
                continue
            entries.append({
                "root": root_name,
                "path": rel_f,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    return entries


def list_logs(base_dir):
    """คืนรายการไฟล์ log ทั้งหมดใต้ log/ และ logs/ ของ base_dir

    Root ที่หายไปแค่ไม่มีไฟล์ให้ ไม่ใช่ error รายการสุดท้ายเรียงตาม
    (root, path) แล้ว cap ไว้ทั้งจำนวนรายการ (MAX_LIST_ENTRIES) และขนาด JSON
    โดยประมาณของ "files" (MAX_LIST_BYTES) — โดน cap อันไหนก่อนก็ truncated=True
    """
    try:
        entries = []
        for root_name in LOG_ROOTS:
            entries.extend(_walk_root(os.path.join(base_dir, root_name), root_name))
        entries.sort(key=lambda e: (e["root"], e["path"]))

        files = []
        total_bytes = 0
        truncated = False
        for entry in entries:
            if len(files) >= MAX_LIST_ENTRIES:
                truncated = True
                break
            # +1 ประมาณ comma ที่คั่นระหว่าง element ตอนรวมเป็น JSON array จริง
            entry_bytes = len(json.dumps(entry)) + 1
            if total_bytes + entry_bytes > MAX_LIST_BYTES:
                truncated = True
                break
            files.append(entry)
            total_bytes += entry_bytes

        result = {"ok": True, "files": files}
        if truncated:
            result["truncated"] = True
        return result
    except Exception as e:  # ห้ามปล่อยให้ exception หลุดออกจาก handler
        logger.error(f"[LOG AGENT] list_logs failed: {e}")
        return _err("io_error")


def _is_within_dir(path, ancestor):
    """True ถ้า path เท่ากับ ancestor หรืออยู่ใต้ ancestor จริง ๆ

    เทียบแบบ commonpath (ตัด path เป็น component แล้วเทียบทีละท่อน) แทนที่จะ
    ใช้ string prefix ตรง ๆ ซึ่งจะพลาดเคสเช่น '/a/foobar' โดนนับผิดว่าอยู่ใต้
    '/a/foo' ทั้งที่จริงเป็นคนละโฟลเดอร์กัน (ไม่มี separator คั่น)
    """
    if path == ancestor:
        return True
    try:
        return os.path.commonpath([path, ancestor]) == ancestor
    except ValueError:
        # ไม่มี common path เลย (เช่นคนละ drive บน Windows) แปลว่าไม่ได้อยู่ใต้กันแน่ ๆ
        return False


def _resolve_safe_path(root_dir, root_name, rel_path, base_dir):
    """แปลง rel_path (posix, สัมพัทธ์กับ root) เป็น absolute path จริงบนดิสก์

    คืน None ถ้า path นี้มีทางหลุดออกจาก root_dir ไม่ว่าด้วยวิธีไหน:
    เป็น absolute path, มี segment '.'/'..', segment ใด ๆ (รวมไฟล์ปลายทาง)
    ขึ้นต้นด้วย '.' (hidden — กันทั้งไฟล์ซ่อนตรง ๆ และไฟล์ที่อยู่ใต้โฟลเดอร์
    ซ่อน ไม่ว่าจะซ่อนอยู่ลึกแค่ไหน ให้ผลตรงกับที่ list_logs()/_walk_root ไม่
    แสดงของซ่อนเช่นกัน), ตัว segment ใด ๆ ระหว่างทาง (รวมไฟล์ปลายทาง) เป็น
    symlink, realpath แล้วไปโผล่นอก root_dir, ชี้เข้าโฟลเดอร์ลูกชั้นบนสุดที่
    ถูกกันไว้ (monitor_snapshots — เทียบแบบ casefold กัน macOS ที่ดิสก์ไม่สน
    ตัวพิมพ์เล็ก-ใหญ่ เช่น "Monitor_Snapshots"/"MONITOR_SNAPSHOTS" ก็คือ
    ไดเรกทอรีเดียวกัน), หรือ realpath ของปลายทางไปโผล่ใน log/monitor_snapshots
    ตรง ๆ (เช็คซ้ำอีกชั้นแบบไม่สนว่ามาจาก root ไหน/ชื่อ segment อะไร เป็น
    backstop เผื่อการเช็คชื่อ segment ด้านบนพลาดจาก edge case ที่คาดไม่ถึง)
    """
    if not rel_path or os.path.isabs(rel_path):
        return None

    parts = rel_path.split('/')
    if any(p in ('', '.', '..') for p in parts):
        return None
    # segment ใด ๆ ขึ้นต้นด้วย '.' ถือเป็นของซ่อนเสมอ (ครอบคลุมทั้งไฟล์ซ่อน
    # ตรง ๆ เช่น "logs/.hidden.log" และไฟล์ที่อยู่ใต้โฟลเดอร์ซ่อน เช่น
    # "logs/.cache/x.log") ให้อ่านไม่ได้เหมือนกับที่ list_logs() ไม่แสดงให้เห็น
    if any(p.startswith('.') for p in parts):
        return None

    excluded = _EXCLUDED_TOP_SUBDIR.get(root_name)
    if excluded and parts[0].casefold() == excluded.casefold():
        return None

    if os.path.islink(root_dir):
        return None

    # ไล่ทีละ segment กันไม่ให้ตัวใดตัวหนึ่งในเส้นทางเป็น symlink — ต้องเช็ค
    # ทุกระดับ ไม่ใช่แค่ไฟล์ปลายทาง เพราะโฟลเดอร์กลางทางก็ลิงก์ออกนอก root ได้
    current = root_dir
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return None

    root_real = os.path.realpath(root_dir)
    target_real = os.path.realpath(current)
    if target_real != root_real and not target_real.startswith(root_real + os.sep):
        return None

    # Backstop เพิ่มเติมนอกจากเช็คชื่อ segment ด้านบน: เทียบ realpath ปลายทาง
    # กับ log/monitor_snapshots ตรง ๆ เสมอ ไม่ว่าจะ resolve มาจาก root ไหน
    # (คำนวณ realpath ได้แม้โฟลเดอร์นี้จะยังไม่มีอยู่จริงบนดิสก์ ณ ตอนนี้)
    monitor_snapshots_real = os.path.realpath(
        os.path.join(base_dir, "log", _EXCLUDED_TOP_SUBDIR["log"])
    )
    if _is_within_dir(target_real, monitor_snapshots_real):
        return None

    return current


def read_log(base_dir, payload):
    """อ่านไฟล์ log เป็นช่วง ๆ (offset/length) คืน base64 ของ bytes ที่อ่านได้

    สำเร็จ: {"ok": True, "data": <base64>, "size": int, "offset": int, "eof": bool}
    ล้มเหลว: {"ok": False, "error": <bad_request|bad_root|bad_path|not_found|
              file_changed|io_error>} — bad_path ครอบคลุมไฟล์/โฟลเดอร์ที่ชื่อ
              ขึ้นต้นด้วย '.' (hidden) ด้วย ไม่ว่าจะซ่อนอยู่ที่ระดับไหนก็ตาม
              (เช่น logs/.hidden.log หรือไฟล์ใต้ logs/.cache/) แม้ผู้เรียกจะรู้
              path ตรง ๆ แล้วขอเจาะจงอ่านก็ตาม — สอดคล้องกับ list_logs() ที่ไม่
              แสดงของซ่อนพวกนี้ให้เห็นตั้งแต่แรก; file_changed คือ offset ที่
              ส่งมาเกินขนาดไฟล์ปัจจุบัน (ไฟล์โดน rotate/truncate ระหว่างที่
              แอดมินกำลังดึงอยู่ offset เดิมที่ถืออยู่ในมือใช้ต่อไม่ได้แล้ว)
    ไม่โยน exception ออกไปเด็ดขาด — จับเองแล้ว log ด้วย module logger
    """
    try:
        if not isinstance(payload, dict):
            return _err("bad_request")

        root = payload.get("root")
        rel_path = payload.get("path")
        offset = payload.get("offset")
        length = payload.get("length")

        if not isinstance(root, str) or not isinstance(rel_path, str):
            return _err("bad_request")
        # bool เป็น subclass ของ int ใน Python — กันไว้ไม่ให้ True/False หลุดผ่านมาเป็น offset/length
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return _err("bad_request")
        if not isinstance(length, int) or isinstance(length, bool):
            return _err("bad_request")

        if root not in LOG_ROOTS:
            return _err("bad_root")

        length = max(MIN_READ_LENGTH, min(length, MAX_READ_LENGTH))

        root_dir = os.path.join(base_dir, root)
        full_path = _resolve_safe_path(root_dir, root, rel_path, base_dir)
        if full_path is None:
            return _err("bad_path")

        if not _is_log_file(os.path.basename(full_path)):
            return _err("bad_path")
        if not os.path.isfile(full_path):
            return _err("not_found")

        try:
            with open(full_path, 'rb') as f:
                size = os.fstat(f.fileno()).st_size
                # offset เกิน size แปลว่าไฟล์โดน rotate/truncate ไปแล้วหลังจาก
                # ฝั่งแอดมินเริ่มดึง log อยู่ (offset ที่ถืออยู่ในมือใช้ต่อไม่ได้
                # แล้ว) ต้องแจ้งเป็น error แยกจาก eof ปกติ — offset == size ยัง
                # ถือเป็น eof ปกติ (อ่านได้ข้อมูลว่างเปล่า ไม่ใช่ error)
                if offset > size:
                    return _err("file_changed")
                f.seek(offset)
                chunk = f.read(length)
        except OSError as e:
            logger.error(f"[LOG AGENT] io_error reading {root}/{rel_path}: {e}")
            return _err("io_error")

        return {
            "ok": True,
            "data": base64.b64encode(chunk).decode("ascii"),
            "size": size,
            "offset": offset,
            "eof": (offset + len(chunk)) >= size,
        }
    except Exception as e:  # ห้ามปล่อยให้ exception หลุดออกจาก handler
        logger.error(f"[LOG AGENT] read_log unexpected error: {e}")
        return _err("io_error")


def on_log_list(data):  # pragma: no cover - thin socket handler wrapper
    """Socket.IO handler: `log_list`. Payload ไม่ใช้ (ส่ง {} มาก็พอ)."""
    return list_logs(APP_SUPPORT_DIR)


def on_log_read(data):  # pragma: no cover - thin socket handler wrapper
    """Socket.IO handler: `log_read`. Payload: {root, path, offset, length}."""
    return read_log(APP_SUPPORT_DIR, data)
