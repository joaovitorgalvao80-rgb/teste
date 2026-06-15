"""Local API consumption telemetry.

This records calls made by this app, not provider billing. It is intentionally
best-effort: failures to write telemetry must never break the production flow.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from typing import Iterator, Optional


_CTX = contextvars.ContextVar("nwrch_api_usage_ctx", default={})


@contextlib.contextmanager
def context(
    *,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    job_id: Optional[int] = None,
    operation: str = "",
) -> Iterator[None]:
    current = dict(_CTX.get() or {})
    if user_id is not None:
        current["user_id"] = user_id
    if project_id is not None:
        current["project_id"] = project_id
    if job_id is not None:
        current["job_id"] = job_id
    if operation:
        current["operation"] = operation
    token = _CTX.set(current)
    try:
        yield
    finally:
        _CTX.reset(token)


def record(
    provider: str,
    operation: str = "",
    *,
    status_code: Optional[int] = None,
    ok: bool = True,
    units: int = 1,
    latency_ms: float = 0,
    detail: str = "",
) -> None:
    ctx = _CTX.get() or {}
    if not ctx.get("user_id"):
        return
    try:
        import database as db

        db.record_api_usage(
            ctx.get("user_id"),
            ctx.get("project_id"),
            ctx.get("job_id"),
            provider,
            operation or ctx.get("operation", ""),
            status_code=status_code,
            ok=ok,
            units=units,
            latency_ms=latency_ms,
            detail=detail,
        )
    except Exception:
        return


def elapsed_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 1)
