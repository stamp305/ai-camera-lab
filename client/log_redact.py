"""Keeps camera addresses and this Mac's folders out of every log line.

Our own log lines are written not to carry anything that identifies the shop,
the Mac or a person (fix the line itself, not this file). What is left is text
we do not write: tracebacks, which name every folder on the way, and errors
from libraries such as OpenCV/FFmpeg, which repeat the camera URL they failed
to open — and a camera URL carries the camera's username and password
(rtsp://user:pass@ip/...). Masking only the password still printed the address
and stream path, so the whole URL goes: the camera name in the same line
(cam_<ip>_<channel>) already says which camera it was. Paths are cut down to
the part inside the app or its data folder, which is all a bug report needs.

Covers three ways text reaches a log file:
  - log messages, through a record factory rather than a handler filter, so the
    handlers DLC services add for their own files after startup get it too;
  - tracebacks attached to log records (Formatter.formatException/formatStack);
  - anything Python itself writes to stderr (warnings, uncaught exceptions,
    progress bars), for a run from a terminal. The app's launcher discards
    stderr, so there it never reaches a file.
"""
import logging
import os
import re
import sys

_CAMERA_URL = re.compile(r"rtsps?://\S+", re.IGNORECASE)
# tqdm and friends redraw one line with carriage returns: thousands of lines a
# day with nothing a support case needs.
_PROGRESS_BAR = re.compile(r"\r|\d+%\|")

_HOME = os.path.expanduser("~")
# Longest first: each later prefix is contained in the earlier ones.
_PREFIXES = [
    os.path.dirname(os.path.abspath(__file__)) + os.sep,
    os.path.join(_HOME, "Library", "Application Support", "AICamera") + os.sep,
]


def _camera_url(match):
    # "(rtsp://...)": the closing bracket belongs to the sentence, not the URL.
    url = match.group()
    return "<camera stream>)" if url.endswith(")") and "(" not in url else "<camera stream>"


def redact(text):
    text = _CAMERA_URL.sub(_camera_url, text)
    for prefix in _PREFIXES:
        if len(prefix) > 1:
            text = text.replace(prefix, "")
    if _HOME not in ("", "~", "/"):
        text = text.replace(_HOME, "~")
    return text


class _RedactingStream:
    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        if _PROGRESS_BAR.search(text):
            return len(text)
        return self._stream.write(redact(text))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install():
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_redacting", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            return record
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return record

    factory._redacting = True
    logging.setLogRecordFactory(factory)

    format_exception = logging.Formatter.formatException
    format_stack = logging.Formatter.formatStack
    logging.Formatter.formatException = lambda self, ei: redact(format_exception(self, ei))
    logging.Formatter.formatStack = lambda self, si: redact(format_stack(self, si))

    if sys.stderr is not None and not isinstance(sys.stderr, _RedactingStream):
        sys.stderr = _RedactingStream(sys.stderr)
