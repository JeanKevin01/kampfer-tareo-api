"""Logging JSON mínimo del API (F0.8).

Uso:
    from core.log import setup_logging, get_logger
    setup_logging()              # una vez, al importar main
    log = get_logger("api")
    log.info("mensaje", extra={"path": "/x", "ms": 12})

Formato de salida (una línea JSON por evento, apto para `docker logs` / Coolify):
    {"t": "...", "lvl": "INFO", "log": "api", "msg": "...", "path": "/x", "ms": 12}
Nivel por env: LOG_LEVEL (default INFO).
"""
import json
import logging
import os
import sys
import time


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "lvl": record.levelname,
            "log": record.name,
            "msg": record.getMessage(),
        }
        for k in ("path", "ms", "status"):
            v = getattr(record, k, None)
            if v is not None:
                d[k] = v
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def setup_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, _JsonFormatter) for h in root.handlers):
        return  # idempotente (reload de uvicorn, tests)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
