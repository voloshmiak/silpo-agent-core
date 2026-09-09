import contextvars
import json
import logging
import os
import sys
import uuid
from typing import Any

LEVEL_ENV = "SILPOFIT_LOG_LEVEL"
FORMAT = "%(asctime)s %(levelname)-5s [%(run_id)s] %(name)s: %(message)s"

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()
        return True


def setup_logging(level: str | None = None) -> None:
    resolved = (level or os.getenv(LEVEL_ENV) or "INFO").upper()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(FORMAT, datefmt="%H:%M:%S"))
    handler.addFilter(_RunIdFilter())
    logging.basicConfig(level=resolved, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def new_run_id() -> str:
    run_id = uuid.uuid4().hex[:8]
    _run_id.set(run_id)
    return run_id


def preview(value: Any, limit: int = 300) -> str:
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = repr(value)
    value = value.replace("\n", " ")
    return value if len(value) <= limit else f"{value[:limit]}… (+{len(value) - limit} chars)"
