import logging
import sys
from typing import Optional

_LOG_FORMAT = "%(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False

def setup_logging(level: str = "INFO", json: bool = False) -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())

    # Remove default handlers if any (idempotent)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)

    if json:
        try:
            import json as _json
            class JsonFormatter(logging.Formatter):
                def format(self, record: logging.LogRecord) -> str:
                    data = {
                        "ts": self.formatTime(record, _DATE_FORMAT),
                        "level": record.levelname,
                        "logger": record.name,
                        "msg": record.getMessage(),
                    }
                    if record.exc_info:
                        data["exc_info"] = self.formatException(record.exc_info)
                    return _json.dumps(data, ensure_ascii=False)
            formatter = JsonFormatter()
        except Exception:
            formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)
    else:
        formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

    handler.setFormatter(formatter)
    root.addHandler(handler)
    _configured = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    if not _configured:
        setup_logging()
    return logging.getLogger(name if name else __name__)