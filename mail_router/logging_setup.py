from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in ("args", "asctime", "created", "exc_info", "exc_text", "filename",
                      "funcName", "levelname", "levelno", "lineno", "message", "module",
                      "msecs", "msg", "name", "pathname", "process", "processName",
                      "relativeCreated", "stack_info", "thread", "threadName",
                      "taskName"):
                continue
            payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure(level: str = "INFO") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
