"""Scan the client's log folders for anything that identifies the shop, the Mac, a person, or our stack.

    python tools/scan_log_leaks.py                    # ~/Library/Application Support/AICamera/{logs,log}
    python tools/scan_log_leaks.py path/to/dir ...    # any files or folders
    python tools/scan_log_leaks.py --key KEY.pem      # also open sealed logs (log_seal.py) and scan them

Strict on purpose: it would rather report a harmless line than miss a leak.
  - patterns: URLs of any scheme, host and domain names, IP addresses, camera
    names, MACs, activation codes, emails, phone and ID numbers, keys/tokens,
    identity fields (user, name, client_id, serial, ...), folders, source and
    model file names, model and library names;
  - literals: this Mac's own values (serial, every MAC, host names, user name,
    IPs, activateID/client_id from the identity store), searched as plain text;
  - files that are not logs at all (pictures and the like) inside the log folders.

Every hit is also put through log_redact.redact(): "after filter" counts what
would still reach a file with today's filter, and for those the most common
line shapes are printed (digits as 9, the hit as <X>) so the code that wrote
them can be found. Samples are masked, so the report can be pasted into a chat.
Exit code 1 when anything HIGH or MEDIUM survives the filter.

Not shipped: tools/ is excluded from builds.
"""
import argparse
import collections
import gzip
import io
import os
import re
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.path.expanduser("~/Library/Application Support/AICamera")
DEFAULT_DIRS = [os.path.join(SUPPORT, "logs"), os.path.join(SUPPORT, "log"),
                os.path.join(ROOT, "logs"), os.path.join(ROOT, "log")]
LOGISH = (".log", ".txt", ".out", ".err")


def _load_source(name, folder=ROOT):
    """The .py itself, never a compiled .so built for another Python."""
    import importlib.util
    path = os.path.join(folder, name + ".py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(name + "_src", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_redact_mod = _load_source("log_redact")
FILTER_LOADED = _redact_mod is not None
redact = _redact_mod.redact if FILTER_LOADED else (lambda text: text)
SEALED_MAGIC = b"#AICLOG1"

_IP4 = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}"
_TLD = r"(?:th|com|net|org|io|co|dev|app|local|lan|cloud|ai|me|info|biz|asia|xyz|site|online|tech)"
_IDENTITY_KEYS = (r"user(?:_?name)?|name|full_?name|first_?name|last_?name|nick_?name|employee(?:_?name|_?id)?|"
                  r"staff(?:_?name)?|customer(?:_?name)?|phone|tel|mobile|email|line_?id|address|"
                  r"store(?:_?name|_?id)?|branch(?:_?name|_?code)?|shop(?:_?name)?|client_?id|activate_?id|"
                  r"activation(?:_?code)?|serial(?:_?number)?|mac(?:_?address)?|device_?id|host(?:_?name)?|"
                  r"latitude|longitude|lat|lng|lon|province|district")

PATTERNS = [
    # (category, severity, regex)
    ("URL with user:password", "HIGH", r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@\S+"),
    ("camera URL (rtsp)", "HIGH", r"(?i)rtsps?://\S+"),
    ("password / secret field", "HIGH",
     r"(?i)\b(?:pass(?:word|wd)?|pwd|secret|api[_-]?key|device[_-]?key|token|auth(?:orization)?|cookie|session)"
     r"\b['\"]?\s*[:=]\s*['\"]?(?!\*\*\*)[^\s'\",}&]{3,}"),
    ("activation code", "HIGH", r"\bADV-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b"),
    ("Fernet token / ciphertext", "HIGH", r"\bgAAAAA[0-9A-Za-z_\-=]{20,}"),
    ("JWT / bearer token", "HIGH", r"\beyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}|\b[Bb]earer\s+[0-9A-Za-z._\-]{16,}"),
    ("MAC address", "HIGH", r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b|\b[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}\b"),
    ("email address", "HIGH", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("identity field with a value", "HIGH",
     r"(?i)['\"]?\b(?:" + _IDENTITY_KEYS + r")\b['\"]?\s*[:=]\s*(?!['\"]?(?:<|\*\*\*|None\b|null\b|''|\"\"))['\"]?[^\s'\",}&]{2,}"),
    ("Thai phone number", "HIGH", r"(?<![\d.])0[689]\d{8}(?![\d.])|(?<![\d.])0[689]\d-\d{3}-\d{4}(?!\d)|\+66\d{8,9}(?!\d)"),
    # Not 1[6-9]…: epoch milliseconds from 2020 on (?t=… cache busters) look the same.
    ("13-digit ID number", "HIGH", r"(?<![\d.])(?!1[6-9])[1-8]\d{12}(?![\d.])"),
    ("URL (any scheme)", "HIGH", r"\b(?:https?|wss?|ftp|tcp|mqtt|grpc|redis|mysql|mariadb|postgres(?:ql)?)://[^\s'\"<>]+"),
    ("host / domain name", "HIGH", r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+" + _TLD + r"\b(?!\.\w)"),
    ("socket / API path", "MEDIUM", r"/socket\.io\b|/ai-camera/[\w/.-]*|/api/v?\d*[\w/.-]*"),
    ("public IP address", "HIGH", r"\b(?!(?:10|127|0)\.)(?!192\.168\.)(?!172\.(?:1[6-9]|2\d|3[01])\.)(?!169\.254\.)" + _IP4 + r"\b"),
    ("private IP address", "MEDIUM", r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01])|169\.254)(?:\.\d{1,3}){2,3}\b"),
    ("IPv6 address", "MEDIUM", r"(?i)\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b|(?<![\w:])(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?(?![\w:])"),
    ("camera name with IP", "MEDIUM", r"\bcam_\d{1,3}_\d{1,3}_\d{1,3}_\d{1,3}_\d+\b|\b" + _IP4 + r"_\d+\b"),
    ("home / user folder", "HIGH", r"/Users/[^/\s'\"]+|/private/var/folders/\S+|/var/folders/\S+|/Volumes/[^/\s'\"]+|~/"),
    ("app data / install folder", "MEDIUM",
     r"Application Support/AICamera|AICamera\.app|Contents/(?:Resources|MacOS)|managed_services|dlc_local|python_env|site-packages"),
    ("source file / module path", "MEDIUM", r"\b[\w/.-]+\.(?:py|pyx|pyc|so|c|js|html)\b(?![\w-])"),
    ("model or library name", "MEDIUM",
     r"(?i)\b(?:yolo\w*|transreid|reid|osnet\w*|bytetrack|botsort|deepsort|mediapipe|face_?landmarker|hand_?landmarker|"
     r"vit[-_]\w+|mivolo\w*|insightface|arcface|safetensors|mlpackage|mlmodelc?|coreml\w*|coremltools|onnx\w*|"
     r"ultralytics|torch\w*|tensorflow|keras|transformers|huggingface|timm|opencv|cv2|ffmpeg|ffprobe|mariadb\w*|"
     r"mysql|uvicorn|fastapi|starlette|socketio|engineio|httpx|httpcore|urllib3|requests(?=\.)|jinja2|pystray|cython|"
     r"pyinstaller|keyring|fernet|cryptography)\b"),
    ("model weights file", "MEDIUM", r"\b[\w.-]+\.(?:pt|pth|onnx|safetensors|mlpackage|mlmodel|tflite|task|npz|npy|engine|bin|pkl)\b"),
    ("long hex / key-like string", "MEDIUM", r"\b[0-9a-fA-F]{24,}\b|\b[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
    ("UUID", "LOW", r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    ("loopback address / port", "LOW", r"\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?\b"),
    ("GPS coordinate", "HIGH", r"\b(?:[0-9]|1[0-9]|20)\.\d{4,}\s*,\s*(?:9[7-9]|10[0-6])\.\d{4,}\b"),
]
PATTERNS = [(c, s, re.compile(p)) for c, s, p in PATTERNS]


def _run(cmd):
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=20).strip()
    except Exception:
        return ""


def known_values():
    """This Mac's own identifying values, to search as literals."""
    found = {}
    hw = _run(["system_profiler", "SPHardwareDataType"])
    m = re.search(r"Serial Number \(system\):\s+(\S+)", hw)
    if m:
        found["hardware serial"] = m.group(1)
    m = re.search(r"Hardware UUID:\s+(\S+)", hw)
    if m:
        found["hardware UUID"] = m.group(1)
    for mac in set(re.findall(r"ether ((?:[0-9a-f]{2}:){5}[0-9a-f]{2})", _run(["ifconfig"]))):
        found["this Mac's MAC (" + mac[-5:] + ")"] = mac
        found["this Mac's MAC no colons (" + mac[-5:] + ")"] = mac.replace(":", "")
    for key, cmd in (("computer name", ["scutil", "--get", "ComputerName"]),
                     ("local host name", ["scutil", "--get", "LocalHostName"]),
                     ("host name", ["scutil", "--get", "HostName"])):
        v = _run(cmd)
        if len(v) >= 4:
            found[key] = v
    host = socket.gethostname()
    if len(host) >= 4:
        found["socket host name"] = host
        found["socket host name (short)"] = host.split(".")[0]
    user = os.environ.get("USER") or ""
    if len(user) >= 3:
        found["login user name"] = user
    real = _run(["id", "-F"])
    if len(real) >= 3:
        found["user's full name"] = real
    for iface in ("en0", "en1"):
        ip = _run(["ipconfig", "getifaddr", iface])
        if ip:
            found[f"this Mac's IP ({iface})"] = ip
    try:  # read-only: no Keychain, no writes
        sys.path.insert(0, ROOT)
        import device_identity
        raw = device_identity._read_raw() or {}
        for field in ("activateID", "client_id", "serial_number", "mac"):
            v = str(raw.get(field) or "")
            if len(v) >= 6:
                found["identity " + field] = v
    except Exception:
        pass
    # One label per value, the longest values first.
    seen, out = set(), {}
    for k, v in sorted(found.items(), key=lambda kv: -len(kv[1])):
        if v not in seen:
            seen.add(v)
            out[k] = v
    return out


def mask(value, keep=3):
    value = value.strip()
    if len(value) <= keep * 2 + 2:
        return value[:1] + "…" + value[-1:] if len(value) > 2 else "…"
    return value[:keep] + "…" + value[-keep:]


def shape(line, matcher):
    line = line.replace(matcher, "<X>") if isinstance(matcher, str) else matcher.sub("<X>", line)
    line = re.sub(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[,.]\d+)?\s*-?\s*", "", line)
    return re.sub(r"\d", "9", line).strip()[:160]


def open_lines(path, private_key):
    """Lines of a log file as text; sealed files are opened when a key is given."""
    with open(path, "rb") as f:
        head = f.read(len(SEALED_MAGIC))
    if head == SEALED_MAGIC:
        if private_key is None:
            return None
        buf = io.BytesIO()
        _load_source("log_open", HERE).open_file(path, private_key, buf)
        return io.StringIO(buf.getvalue().decode("utf-8", "replace"))
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def walk(targets):
    logs, others = [], []
    for t in targets:
        if os.path.isfile(t):
            logs.append(t)
            continue
        for dirpath, _, names in os.walk(t):
            for n in sorted(names):
                p = os.path.join(dirpath, n)
                (logs if any(x in n for x in LOGISH) else others).append(p)
    return logs, others


def scan(paths, literals, n_examples, private_key):
    stats = {}
    sealed_skipped, scanned, lines_total = [], [], 0

    def hit(cat, sev, path, value, after, matcher):
        s = stats.setdefault((cat, sev), {"raw": 0, "after": 0, "files": set(), "examples": [],
                                          "shapes": collections.Counter()})
        s["raw"] += 1
        s["files"].add(os.path.basename(path))
        if after is None:
            return
        s["after"] += 1
        s["shapes"][shape(after, matcher)] += 1
        ex = mask(value)
        if len(s["examples"]) < n_examples and ex not in s["examples"]:
            s["examples"].append(ex)

    for path in paths:
        try:
            lines = open_lines(path, private_key)
        except OSError:
            continue
        if lines is None:
            sealed_skipped.append(path)
            continue
        scanned.append(path)
        with lines:
            for line in lines:
                lines_total += 1
                after = None
                for cat, sev, rx in PATTERNS:
                    for m in rx.finditer(line):
                        if after is None:
                            after = redact(line)
                        survives = any(m2.group() == m.group() for m2 in rx.finditer(after))
                        hit(cat, sev, path, m.group(), after if survives else None, rx)
                for cat, value in literals.items():
                    if value in line:
                        if after is None:
                            after = redact(line)
                        hit("this Mac: " + cat, "HIGH", path, value, after if value in after else None, value)
    return stats, scanned, sealed_skipped, lines_total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="files or folders (default: the app's log folders)")
    ap.add_argument("--key", default=os.environ.get("AICAMERA_LOG_KEY"), help="private key, to scan sealed logs too")
    ap.add_argument("--examples", type=int, default=3, help="masked samples per finding (default 3)")
    ap.add_argument("--shapes", type=int, default=6, help="line shapes per surviving finding (default 6)")
    ap.add_argument("--no-literals", action="store_true", help="skip this Mac's own values")
    ap.add_argument("--raw", action="store_true", help="ignore log_redact: show what the code itself writes")
    args = ap.parse_args()
    global redact, FILTER_LOADED
    if args.raw:
        redact, FILTER_LOADED = (lambda text: text), True

    private_key = _load_source("log_open", HERE).load_private_key(args.key) if args.key else None
    targets = args.targets or [d for d in DEFAULT_DIRS if os.path.isdir(d)]
    paths, others = walk(targets)
    literals = {} if args.no_literals else known_values()
    stats, scanned, sealed, n_lines = scan(paths, literals, args.examples, private_key)

    if not FILTER_LOADED:
        print("log_redact.py not found: 'after filter' equals the raw count.")
    print(f"Scanned {len(scanned)} log file(s), {n_lines:,} lines; "
          f"{len(sealed)} sealed file(s) skipped (pass --key to open them).")
    if literals:
        print("This Mac's own values searched: " + ", ".join(sorted(literals)))
    if others:
        kinds = collections.Counter(os.path.splitext(p)[1].lower() or "(none)" for p in others)
        folders = sorted({os.path.basename(os.path.dirname(p)) for p in others})
        print(f"NOT LOGS inside the log folders: {len(others)} file(s) — "
              + ", ".join(f"{n}× {k}" for k, n in kinds.most_common()) + " — in " + ", ".join(folders[:6]))
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    print()
    print(f"{'severity':8} {'finding':34} {'hits':>10} {'after filter':>12}  files")
    for (cat, sev), s in sorted(stats.items(), key=lambda kv: (order[kv[0][1]], -kv[1]["after"], kv[0][0])):
        files = ", ".join(sorted(s["files"])[:3]) + (" …" if len(s["files"]) > 3 else "")
        print(f"{sev:8} {cat:34} {s['raw']:>10,} {s['after']:>12,}  {files}")
        if s["after"]:
            print(f"{'':8}   e.g. " + " | ".join(s["examples"]))
            for sh, n in s["shapes"].most_common(args.shapes):
                print(f"{'':8}   {n:>8,}  {sh}")
    left = sum(s["after"] for (c, sev), s in stats.items() if sev in ("HIGH", "MEDIUM"))
    print()
    print(f"HIGH/MEDIUM hits still present after today's filter: {left:,}")
    return 1 if left or others else 0


if __name__ == "__main__":
    sys.exit(main())
