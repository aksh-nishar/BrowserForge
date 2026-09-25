"""Application logging: structlog on top of stdlib logging, so third-party logs (uvicorn,
playwright, ...) flow through the same pipeline and carry the same context (e.g. job_id).

Dev: colored console. Demo/prod: JSON lines, optionally to a rotating file.
Call once at startup. Never log credentials.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
from structlog.typing import Processor

_OWNED = "_browserforge_handler"
_MAX_LOG_BYTES = 5_000_000
_LOG_BACKUPS = 3


def _formatter(shared: list[Processor], renderer: Processor) -> structlog.stdlib.ProcessorFormatter:
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )


def configure_logging(
    *,
    json_logs: bool = False,
    log_file: str | Path | None = None,
    level: int = logging.INFO,
) -> None:
    """(Re)configure logging. Safe to call more than once: handlers are replaced, not stacked."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )

    json_renderer = structlog.processors.JSONRenderer()
    console_renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    handlers: list[logging.Handler] = []
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_formatter(shared, json_renderer if json_logs else console_renderer))
    handlers.append(stream)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
        )
        file_handler.setFormatter(_formatter(shared, json_renderer))
        handlers.append(file_handler)

    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _OWNED, False)]:
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        setattr(handler, _OWNED, True)
        root.addHandler(handler)
    root.setLevel(level)
