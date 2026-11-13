"""Sends one DLC file to a branch: whole for small files, in parts for large ones.

A file used to go as one Socket.IO message. A model of a few hundred MB became
a single base64 message of 1.37x its size, held whole in memory on both sides,
with the server's single eventlet thread busy until it was out. The Engine.IO
pings queued behind it (ping_timeout) and the branch, seeing no file arrive for
a minute, decided the transfer was over and dropped the connection — either
way the file died halfway.

Files above CHUNK_THRESHOLD now go in parts: add_file_begin, add_file_chunk
(CHUNK_SIZE bytes each, read from disk as they go) and add_file_end with the
sha256 of the whole file. Each part waits for the branch's acknowledgement
before the next one, so pings and other branches get their turn, nothing piles
up in memory, and a branch that falls behind or drops simply stops the file
(it is sent again on the next update — the branch only keeps a file whose hash
matches). Only .py files are encrypted in transit, and those are always small,
so a file in parts is sent as it is stored.

A branch running a client from before this change does not answer
add_file_begin: its large files are skipped with a warning instead of sent the
old way, which could not get through anyway.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from config import Config

CHUNK_THRESHOLD = int(os.getenv("DLC_CHUNK_THRESHOLD", str(8 * 1024 * 1024)))
CHUNK_SIZE = int(os.getenv("DLC_CHUNK_SIZE", str(4 * 1024 * 1024)))
# Per part, not per file: 4 MB on a slow shop line still arrives well inside this.
ACK_TIMEOUT = int(os.getenv("DLC_CHUNK_ACK_TIMEOUT", "120"))


def master_file_path(rel_path: str) -> Optional[str]:
    """Absolute path of a file in DLC master, None when it points outside it."""
    root = os.path.normpath(Config.DLC_MASTER_PATH)
    full = os.path.normpath(os.path.join(root, rel_path))
    if not full.startswith(root + os.sep):
        return None
    return full


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _call(socketio, event: str, data: dict, sid: str, timeout: int = None):
    """Emit and wait for the branch's answer; None when it does not answer in time."""
    try:
        return socketio.call(event, data, to=sid, timeout=timeout or ACK_TIMEOUT)
    except Exception:
        return None


def needs_parts(rel_paths) -> bool:
    """Would any of these DLC master files go in parts (a model, not code)?"""
    for rel in rel_paths:
        full = master_file_path(rel)
        if full and not rel.endswith(".py") and os.path.isfile(full) and os.path.getsize(full) > CHUNK_THRESHOLD:
            return True
    return False


def models_missing(rel_paths) -> list:
    """Model files this update's services need that are not in DLC master.

    A service whose code loads its model through configs/model_vault.py reads
    models/<service>.bin (face_service -> models/face.bin). Those files are kept
    out of git and copied to the server by hand; a deploy without them would send
    every branch services with no model to load, so nothing goes out until they are there.
    """
    missing = []
    for folder in sorted({rel.split("/")[0] for rel in rel_paths if "/" in rel}):
        root = master_file_path(folder)
        if root is None or not os.path.isdir(root) or folder == "configs":
            continue
        uses_vault = False
        for r, _d, files in os.walk(root):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(r, name), "rb") as f:
                        if b"model_vault" in f.read():
                            uses_vault = True
                            break
            if uses_vault:
                break
        model = f"{folder[:-len('_service')] if folder.endswith('_service') else folder}.bin"
        if uses_vault and not os.path.isfile(os.path.join(root, "models", model)):
            missing.append(f"{folder}/models/{model}")
    return missing


def takes_parts(socketio, sid: str) -> bool:
    """Can the branch on `sid` receive files in parts? Asked before an update that needs it.

    A client from before files in parts has no answer. Such a branch must not get
    the update at all: its services would arrive expecting models that could not
    be sent to it, and the ones it runs now would stop. Held back, it keeps
    working on what it has until its client is updated.
    """
    answer = _call(socketio, "dlc_caps", {}, sid, timeout=15)
    return isinstance(answer, dict) and answer.get("chunked_files") is True


def send_dlc_file(socketio, sid: str, rel_path: str, cipher_suite, logger) -> bool:
    """Send DLC master's `rel_path` to the branch on `sid`. True once the branch has it (or it was queued)."""
    full = master_file_path(rel_path)
    if full is None or not os.path.isfile(full):
        logger.error(f"[DLC] Not in DLC master, not sent: {rel_path}")
        return False
    size = os.path.getsize(full)
    enc = rel_path.endswith(".py")

    if enc or size <= CHUNK_THRESHOLD:
        with open(full, "rb") as f:
            content = f.read()
        payload = content
        if enc:
            try:
                payload = cipher_suite.encrypt(content)
            except Exception as exc:
                logger.error(f"[DLC] Encryption failed for {rel_path}: {exc}")
                return False
        socketio.emit("add_file", {
            "command": "add_file",
            "filename": rel_path,
            "content": base64.b64encode(payload).decode("ascii"),
            "hash": hashlib.sha256(content).hexdigest(),
            "enc": enc,
        }, to=sid)
        return True

    file_hash = sha256_of(full)
    parts = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    logger.info(f"[DLC]   {rel_path}: {size / 1e6:.1f} MB in {parts} parts")
    if _call(socketio, "add_file_begin", {"filename": rel_path, "size": size, "hash": file_hash,
                                          "parts": parts}, sid) is not True:
        logger.warning(f"[DLC]   {rel_path} skipped: the branch did not accept a file in parts "
                       f"(client too old, or it went offline)")
        return False
    with open(full, "rb") as f:
        for index in range(parts):
            block = f.read(CHUNK_SIZE)
            answer = _call(socketio, "add_file_chunk", {
                "filename": rel_path, "index": index,
                "content": base64.b64encode(block).decode("ascii"),
            }, sid)
            if answer is not True:
                logger.warning(f"[DLC]   {rel_path} stopped at part {index + 1}/{parts} "
                               f"(no answer from the branch); it is sent again on the next update")
                return False
    if _call(socketio, "add_file_end", {"filename": rel_path, "hash": file_hash, "size": size}, sid) is not True:
        logger.warning(f"[DLC]   {rel_path}: the branch did not confirm the whole file (hash or size differ)")
        return False
    return True
