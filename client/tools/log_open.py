"""Read log files sealed by log_seal.py. Needs the private key, which only the server keeps.

    python tools/log_open.py --key KEY.pem FILE_OR_DIR ...           # print to stdout
    python tools/log_open.py --key KEY.pem --out DIR FILE_OR_DIR ... # write plain copies
    python tools/log_open.py --key KEY.pem -f FILE                   # follow, like tail -f

The key path can also come from $AICAMERA_LOG_KEY. Lines that are not sealed
(a file from before sealing, or a line a shell script appended) come out as
they are. Self-contained on purpose (only `cryptography`), so the server can
take this file as it is; the format is described in log_seal.py.

Not shipped: tools/ is excluded from builds.
"""
import argparse
import base64
import binascii
import os
import sys
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"#AICLOG1"
INFO = b"aicamera-log-seal-v1"
_HEX = set(b"0123456789abcdef")


def load_private_key(path):
    with open(path, "rb") as f:
        data = f.read()
    password = os.environ.get("AICAMERA_LOG_KEY_PASSWORD")
    return serialization.load_pem_private_key(data, password=password.encode() if password else None)


class Opener:
    """Decrypts the lines of one sealed file, in order."""

    def __init__(self, private_key):
        self._key = private_key
        self._public = private_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw)
        self._sessions = {}

    def line(self, raw):
        """Plain bytes for one line of the file, or None for a key line."""
        if raw.startswith(MAGIC + b" "):
            try:
                _, kid, pub_b64 = raw.split()
                ephemeral_pub = base64.b64decode(pub_b64)
                shared = self._key.exchange(X25519PublicKey.from_public_bytes(ephemeral_pub))
                key = HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral_pub + self._public,
                           info=INFO).derive(shared)
                self._sessions[kid] = AESGCM(key)
            except Exception:
                return b"[log_open: unreadable key line]\n"
            return None
        kid, sep, body = raw.partition(b" ")
        if not sep or len(kid) != 16 or not set(kid) <= _HEX:
            return raw  # never sealed
        aead = self._sessions.get(kid)
        if aead is None:
            return b"[log_open: record without its key line]\n"
        try:
            blob = base64.b64decode(body.strip(), validate=True)
            return aead.decrypt(blob[:12], blob[12:], kid)
        except (InvalidTag, binascii.Error, ValueError):
            return b"[log_open: record does not open with this key, or is damaged]\n"


def iter_files(targets):
    for t in targets:
        if os.path.isdir(t):
            for dirpath, _, names in os.walk(t):
                for n in sorted(names):
                    if ".log" in n:
                        yield os.path.join(dirpath, n)
        else:
            yield t


def open_file(path, private_key, out):
    opener = Opener(private_key)
    with open(path, "rb") as f:
        for raw in f:
            text = opener.line(raw)
            if text is not None:
                out.write(text)


def follow(path, private_key, out):
    opener, f, ino = None, None, None
    while True:
        try:
            st = os.stat(path)
        except OSError:
            time.sleep(0.5)
            continue
        if f is None or st.st_ino != ino:
            if f:
                f.close()
            f, ino, opener = open(path, "rb"), st.st_ino, Opener(private_key)
        raw = f.readline()
        if not raw or not raw.endswith(b"\n"):
            if raw:
                f.seek(-len(raw), os.SEEK_CUR)
            out.flush()
            time.sleep(0.5)
            continue
        text = opener.line(raw)
        if text is not None:
            out.write(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--key", default=os.environ.get("AICAMERA_LOG_KEY"), help="private key PEM")
    ap.add_argument("--out", help="write a plain copy of each file into this folder")
    ap.add_argument("-f", "--follow", action="store_true", help="keep reading new lines (one file)")
    args = ap.parse_args()
    if not args.key:
        ap.error("no key: pass --key or set AICAMERA_LOG_KEY")
    private_key = load_private_key(args.key)
    stdout = sys.stdout.buffer

    if args.follow:
        try:
            follow(args.targets[0], private_key, stdout)
        except KeyboardInterrupt:
            return 0
    for path in iter_files(args.targets):
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            dest = os.path.join(args.out, os.path.basename(path))
            with open(dest, "wb") as out:
                open_file(path, private_key, out)
            print(dest)
        else:
            if len(args.targets) > 1 or os.path.isdir(args.targets[0]):
                stdout.write(f"==> {path} <==\n".encode())
            open_file(path, private_key, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
