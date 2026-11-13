"""The web UI's pages and static files, packed into one encrypted file for the .app.

templates/ and static/ used to ship as plain files inside the .app: anyone could
read every page and script, and edit them — a changed template is served as it
is, the first launch clears the quarantine flag, so macOS never checks the
signature again. A build now packs both folders into assets.bin (zip, then
AES-256-GCM) and ships neither folder (tools/pack_ui_assets.py). The key is
not a new secret: it is derived from the DLC key (STATIC_LEGACY_KEY), which
every install already has compiled into _secrets.so.

At run time every page and file is read from memory. A changed byte anywhere in
assets.bin fails the GCM check and nothing is served. What the browser
receives can still be seen in its developer tools — the point is that nothing
sits on disk to copy or change.

A source checkout has no assets.bin and keeps reading the folders.
"""
import hashlib
import io
import os
import zipfile

MAGIC = b"AICUI1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(BASE_DIR, "assets.bin")
FOLDERS = ("templates", "static")
# Never packed: design previews kept next to the real pages in a working copy.
_SKIP_DIRS = {"_preview", "__pycache__"}

_KEY_LABEL = b"aicamera-ui-assets-v1"

_files = None


def key():
    """32 bytes derived from the DLC key, the same in every build."""
    # The compiled one first, so a test run's environment cannot make the
    # bundle unreadable; the environment only where _secrets has none.
    try:
        import _secrets
        value = getattr(_secrets, "STATIC_LEGACY_KEY", "") or ""
    except ImportError:
        value = ""
    value = value or os.environ.get("STATIC_LEGACY_KEY", "")
    if isinstance(value, str):
        value = value.strip().encode()
    if not value:
        raise RuntimeError("no DLC key (STATIC_LEGACY_KEY) to open the UI bundle")
    return hashlib.sha256(_KEY_LABEL + value).digest()


def available():
    return os.path.exists(BUNDLE)


def pack(base_dir, out_path, key):
    """Zip templates/ and static/ under base_dir and seal them into out_path."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in FOLDERS:
            for dirpath, dirs, names in os.walk(os.path.join(base_dir, folder)):
                dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
                for name in sorted(names):
                    if not name.startswith("."):
                        path = os.path.join(dirpath, name)
                        z.write(path, os.path.relpath(path, base_dir).replace(os.sep, "/"))
    nonce = os.urandom(12)
    with open(out_path, "wb") as f:
        f.write(MAGIC + nonce + AESGCM(key).encrypt(nonce, buf.getvalue(), MAGIC))


def unpack(blob, key):
    """{"templates/x.html": bytes, "static/js/app.js": bytes, ...}; raises if anything was changed."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:len(MAGIC)] != MAGIC:
        raise ValueError("not a UI asset bundle")
    nonce = blob[len(MAGIC):len(MAGIC) + 12]
    data = AESGCM(key).decrypt(nonce, blob[len(MAGIC) + 12:], MAGIC)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {name: z.read(name) for name in z.namelist()}


def files():
    global _files
    if _files is None:
        with open(BUNDLE, "rb") as f:
            _files = unpack(f.read(), key())
    return _files


def read(path):
    """One file by its path inside the UI folder, e.g. "static/images/icons/logo_aicam.png"."""
    return files().get(path)


def templates():
    """{name: source} for a jinja2.DictLoader."""
    prefix = "templates/"
    return {name[len(prefix):]: data.decode("utf-8")
            for name, data in files().items() if name.startswith(prefix)}


def etag(data):
    return '"' + hashlib.sha256(data).hexdigest()[:16] + '"'
