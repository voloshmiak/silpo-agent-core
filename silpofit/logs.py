"""Logging setup shared by the whole service.

Every line carries a run id so one request's lines can be pulled out of an
interleaved log (the service handles requests concurrently, and a single plan
run emits dozens of lines). The id is also sent to the caller on every SSE
event, so a frontend error can be traced back to the exact run in the logs.
"""

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
    """Makes `%(run_id)s` available on every record, including third-party ones."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()
        return True


def setup_logging(level: str | None = None) -> None:
    """Configures root logging. Safe to call more than once."""
    resolved = (level or os.getenv(LEVEL_ENV) or "INFO").upper()
    # Ukrainian plan text and tool results go through these lines, and a
    # Windows console defaults to a codepage that turns them into "???".
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):  # pragma: no cover - not a real stream
        pass
    # stdout, not stderr: Cloud Run labels everything on stderr as an error,
    # which would hide the actual ERROR lines among the INFO ones.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(FORMAT, datefmt="%H:%M:%S"))
    handler.addFilter(_RunIdFilter())
    logging.basicConfig(level=resolved, handlers=[handler], force=True)
    # httpx logs one INFO line per MCP request; at DEBUG it dumps headers that
    # would leak the user's Silpo token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def new_run_id() -> str:
    """Starts a new correlation id for the current task and returns it."""
    run_id = uuid.uuid4().hex[:8]
    _run_id.set(run_id)
    return run_id


def current_run_id() -> str:
    return _run_id.get()


def preview(value: Any, limit: int = 300) -> str:
    """Renders anything as a single short line — never raises, never wraps."""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover - defensive: logging must not fail
            value = repr(value)
    value = value.replace("\n", " ")
    return value if len(value) <= limit else f"{value[:limit]}… (+{len(value) - limit} chars)"