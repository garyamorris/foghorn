"""structlog configured once; correlation ids come from contextvars (async + thread safe)."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars
from structlog.typing import EventDict

from foghorn.settings import get_settings

_SENSITIVE = ("api_key", "authorization", "secret", "token", "password", "cookie")


def _redact(_: Any, __: str, event: EventDict) -> EventDict:
    for k in list(event):
        if any(s in k.lower() for s in _SENSITIVE):
            event[k] = "***"
    return event


def configure_logging(force: bool = False) -> None:
    s = get_settings()
    if structlog.is_configured() and not force:
        return
    renderer: Any = structlog.processors.JSONRenderer() if s.log_json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            merge_contextvars,  # run_id / thread_id / graph / node bound by bind_run()
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, s.log_level, logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),  # stderr: never pollute stdout/SSE
        cache_logger_on_first_use=False,  # tests reconfigure
    )


def bind_run(config: RunnableConfig | None, *, graph: str | None = None, node: str | None = None) -> None:
    """Call at node entry: pulls ids from the RunnableConfig LangGraph hands every node."""
    cfg = config or {}
    conf = cfg.get("configurable", {}) or {}
    meta = cfg.get("metadata", {}) or {}
    clear_contextvars()
    bind_contextvars(
        thread_id=conf.get("thread_id") or meta.get("thread_id"),
        run_id=str(cfg.get("run_id") or meta.get("run_id") or ""),
        graph=graph or meta.get("graph_id"),
        node=node or meta.get("langgraph_node"),
        assistant_id=meta.get("assistant_id"),
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
