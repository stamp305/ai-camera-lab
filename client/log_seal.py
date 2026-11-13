"""Log files that only the server can read.

The logs sit in ~/Library/Application Support/AICamera, which anyone using the
Mac can open. Every log file the app writes there is now sealed to the
server's public key (SERVER_PUBLIC_KEY): this Mac can write a log but cannot
read it back, and neither can anyone at the store. Reading one means taking the
file to the server, which alone holds the private key (tools/log_open.py until
the server side exists).

A sealed file is still text, one record per line, so appends from several
processes and a torn last line cost at most that line:

    #AICLOG1 <kid> <base64: ephemeral X25519 public key>     key line
    <kid> <base64: 12-byte nonce + AES-256-GCM ciphertext>    record line

Each time a process opens a log file it makes a fresh X25519 key pair, derives
an AES key from ECDH(its private half, the server's public key) with
HKDF-SHA256, writes the public half as a key line before its first record and
keeps the private half only in memory. A record is one write: one log message,
traceback included. kid ties each record to its key line.

Covers what reaches a log file:
  - every logging.FileHandler (and the rotating ones) whose file is under the
    app's data folder or the app itself, including the handlers delivered
    services add for their own log/<service>.log;
  - stdout/stderr when they point at such a file (the LaunchAgent of a branch
    installed by git clone writes them to logs/launchd.log), at the descriptor
    level so output from C libraries and child processes is sealed too;
  - files written in plain text by older versions, sealed in place the first
    time this runs.
"""
import base64
import fcntl
import hashlib
import logging
import os
import stat
import sys
import threading
import time

MAGIC = "#AICLOG1"
_MAGIC_B = MAGIC.encode()
_INFO = b"aicamera-log-seal-v1"
# Raw X25519 public key of the server that opens the logs. Its private half is
# kept off the app (tools/log_keygen.py made the pair).
SERVER_PUBLIC_KEY = "OAbXBqfc6pMrzLWnbvfPfB49vj6IKJXlTuKFelUXVzU="

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/AICamera")
_ROOTS = tuple(os.path.realpath(p) + os.sep for p in (_SUPPORT_DIR, _APP_DIR))
_LOG_DIRS = ("logs", "log")
# Old plain files are sealed in blocks of up to this many bytes, one record each.
_BLOCK = 64 * 1024
# A plain file written this recently may still be open in another program (the
# installer's pip run appends to logs/); it is sealed on a later start instead.
_QUIET_SECONDS = 120
# Sealing is paused: logs are written in plain text again. Everything below is
# kept so it can be switched back on; while off, install() only moves files a
# sealing version left behind out of the way (to <name>.sealed), so new plain
# lines never land in a sealed file. Those can still be read with log_open.py.
ENABLED = False

_server_key = None


def _public_key():
    global _server_key
    if _server_key is None:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        _server_key = X25519PublicKey.from_public_bytes(base64.b64decode(SERVER_PUBLIC_KEY))
    return _server_key


def _new_session():
    """(AESGCM, kid bytes, key line bytes) for one writer of one file."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    server = _public_key()
    raw = serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ephemeral = X25519PrivateKey.generate()
    ephemeral_pub = ephemeral.public_key().public_bytes(*raw)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral_pub + server.public_bytes(*raw),
               info=_INFO).derive(ephemeral.exchange(server))
    kid = hashlib.sha256(ephemeral_pub).hexdigest()[:16].encode()
    key_line = b"%s %s %s\n" % (_MAGIC_B, kid, base64.b64encode(ephemeral_pub))
    return AESGCM(key), kid, key_line


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def should_seal(path):
    """True for a file inside the app's data folder or the app itself."""
    try:
        real = os.path.realpath(path)
    except Exception:
        return False
    return any(real.startswith(root) for root in _ROOTS)


def is_sealed(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(_MAGIC_B)) == _MAGIC_B
    except OSError:
        return False


class _Sealer:
    """Turns byte strings into sealed record lines on a file descriptor."""

    def __init__(self, fd):
        self._fd = fd
        self._aead, self._kid, self._key_line = _new_session()
        self._started = False
        self._lock = threading.Lock()

    def seal(self, data):
        if not data:
            return
        nonce = os.urandom(12)
        line = b"%s %s\n" % (self._kid, base64.b64encode(nonce + self._aead.encrypt(nonce, data, self._kid)))
        with self._lock:
            if not self._started:
                # One write with the first record, so no reader ever sees a
                # record whose key line is still missing.
                line = self._key_line + line
                self._started = True
            _write_all(self._fd, line)


class SealedFile:
    """What a log FileHandler writes to instead of a plain file: one write() is one sealed record."""

    def __init__(self, path, mode="a", encoding=None, errors=None):
        self.name = path
        self.mode = mode
        # FileHandler passes "locale" when no encoding was given: fine for open(), not for str.encode().
        self.encoding = encoding if encoding and encoding != "locale" else "utf-8"
        self.errors = errors or "backslashreplace"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if "w" in mode:
            flags |= os.O_TRUNC
        else:
            seal_existing(path)
        self._fd = os.open(path, flags, 0o600)
        self._sealer = _Sealer(self._fd)
        self.closed = False

    def write(self, text):
        if self.closed:
            raise ValueError("write to closed file")
        data = text.encode(self.encoding, self.errors) if isinstance(text, str) else bytes(text)
        self._sealer.seal(data)
        return len(text)

    def writable(self):
        return True

    def flush(self):
        pass

    def fileno(self):
        return self._fd

    def seek(self, offset, whence=0):
        return os.lseek(self._fd, offset, whence)

    def tell(self):
        return os.lseek(self._fd, 0, os.SEEK_CUR)

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self._fd)


class _Discard:
    """Stands in for a log file that could not be sealed: nothing is written in plain."""
    closed = False

    def write(self, text):
        return len(text)

    def flush(self):
        pass

    def close(self):
        self.closed = True


def seal_existing(path):
    """Seal a file an older version wrote in plain text, in place. Safe to race with other processes."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            current = os.stat(path)
        except OSError:
            return
        st = os.fstat(fd)
        # Empty, already sealed, not a plain file, or replaced while we waited for the lock.
        if (st.st_size == 0 or not stat.S_ISREG(st.st_mode)
                or (st.st_dev, st.st_ino) != (current.st_dev, current.st_ino)
                or os.pread(fd, len(_MAGIC_B), 0) == _MAGIC_B):
            return
        tmp = f"{path}.sealing-{os.getpid()}"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            sealer = _Sealer(out)
            pending = b""
            while True:
                chunk = os.read(fd, _BLOCK)
                if not chunk:
                    break
                pending += chunk
                cut = pending.rfind(b"\n") + 1
                if cut:
                    sealer.seal(pending[:cut])
                    pending = pending[cut:]
            sealer.seal(pending)
            os.close(out)
            out = None
            # Same date as before: TimedRotatingFileHandler picks its next rollover from it.
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, path)
        except BaseException:
            if out is not None:
                os.close(out)
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass
    finally:
        os.close(fd)


def seal_old_logs():
    """Seal every plain log file left by an older version in the app's log folders."""
    for root in (_SUPPORT_DIR, _APP_DIR):
        for name in _LOG_DIRS:
            folder = os.path.join(root, name)
            try:
                entries = os.listdir(folder)
            except OSError:
                continue
            for entry in entries:
                path = os.path.join(folder, entry)
                if ".log" not in entry or ".sealing-" in entry:
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if (stat.S_ISREG(st.st_mode) and time.time() - st.st_mtime >= _QUIET_SECONDS
                        and not is_sealed(path)):
                    seal_existing(path)


def _seal_stdio():
    """Point stdout/stderr at a sealing pipe when they write to one of the app's files."""
    targets = {}
    for fd in (1, 2):
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                continue
            path = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
        except Exception:
            continue
        if should_seal(path):
            targets.setdefault(path, []).append(fd)
    for path, fds in targets.items():
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        try:
            sealed = SealedFile(path)
        except Exception:
            continue
        read_end, write_end = os.pipe()
        for fd in fds:
            os.dup2(write_end, fd)
        os.close(write_end)
        threading.Thread(target=_pump, args=(read_end, sealed), name="log-seal-stdio", daemon=True).start()


def _pump(read_end, sealed):
    pending = b""
    while True:
        try:
            chunk = os.read(read_end, _BLOCK)
        except InterruptedError:
            continue
        except OSError:
            chunk = b""
        if not chunk:
            break
        pending += chunk
        cut = pending.rfind(b"\n") + 1
        if cut:
            for line in pending[:cut].splitlines(keepends=True):
                sealed.write(line)
            pending = pending[cut:]
        elif len(pending) > _BLOCK:
            sealed.write(pending)
            pending = b""
    if pending:
        sealed.write(pending)


def set_aside_sealed_logs():
    """Rename every sealed log file to <name>.sealed so logging starts a fresh plain file."""
    for root in (_SUPPORT_DIR, _APP_DIR):
        for name in _LOG_DIRS:
            folder = os.path.join(root, name)
            try:
                entries = os.listdir(folder)
            except OSError:
                continue
            for entry in entries:
                if ".log" not in entry or ".sealed" in entry or ".sealing-" in entry:
                    continue
                path = os.path.join(folder, entry)
                if not os.path.isfile(path) or not is_sealed(path):
                    continue
                target = f"{path}.sealed"
                n = 1
                while os.path.exists(target):
                    n += 1
                    target = f"{path}.sealed{n}"
                try:
                    os.rename(path, target)
                except OSError:
                    pass


def install():
    """Seal every log file this process writes. Call before the first log handler exists."""
    if not ENABLED:
        set_aside_sealed_logs()
        return
    original = logging.FileHandler._open
    if getattr(original, "_sealing", False):
        return

    def _open(self):
        if not should_seal(self.baseFilename):
            return original(self)
        try:
            return SealedFile(self.baseFilename, self.mode, self.encoding, self.errors)
        except Exception as e:
            if sys.__stderr__ is not None:
                sys.__stderr__.write(f"log_seal: cannot seal a log file ({type(e).__name__})\n")
            return _Discard()

    _open._sealing = True
    logging.FileHandler._open = _open
    seal_old_logs()
    _seal_stdio()
