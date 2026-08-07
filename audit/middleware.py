"""Middleware & decorators for instrumenting your own agents.

Two integration surfaces:

  * ``@audit_tool`` — wrap any (sync or async) Python function so every call is
    recorded as an :class:`audit.models.AuditEvent` (args redacted, duration,
    result summary, exceptions → severity).
  * ``AuditedSession`` — wrap an ``mcp.ClientSession`` so every
    ``tools/call`` / ``resources/read`` is audited transparently, ideal for
    custom MCP agents that want audit trails without a separate proxy.

Example::

    from audit import AuditEmitter, LocalAuditStore
    from audit.middleware import audit_tool

    store = LocalAuditStore("~/.audit")
    emitter = AuditEmitter(store)

    @audit_tool(emitter, agent_id="researcher")
    def lookup_pdb(pdb_id: str, token: str):
        ...
"""

from __future__ import annotations

import asyncio
import functools
import time
import traceback
from typing import Any, Awaitable, Callable, TypeVar

from .emitter import AuditEmitter
from .models import AuditEvent
from .redaction import redact
from .policy import risk_tier_for, severity_for_tier

F = TypeVar("F", bound=Callable)
MCP_METHODS = ("tools/call", "resources/read", "resources/list",
               "prompts/get", "tools/list")

_DATA_CLASS_HINTS = (
    "dataframe", "csv", "sqlite", "database", "table", "array", "matrix",
    "image", "figure", "file", "pdf", "notebook", "graph", "json",
)


def _infer_data_classes(args: dict | None, result: str | None) -> list[str]:
    classes: set[str] = set()
    text = " ".join([
        str(k) + "=" + str(v)[:200] for k, v in (args or {}).items()
    ]) + " " + (result or "")[:2000]
    low = text.lower()
    for hint in _DATA_CLASS_HINTS:
        if hint in low:
            classes.add(hint)
    return sorted(classes)


def audit_tool(emitter: AuditEmitter, agent_id: str,
               tool_name: str | None = None, source: str = "middleware",
               session_id: str | None = None, trace_id: str | None = None,
               redaction_keys: set[str] | None = None) -> Callable[[F], F]:
    """Decorator: audit every call to the wrapped function.

    Works for both sync and async callables. Sensitive argument values are
    redacted before storage. Duration, result size and exceptions are captured.
    """
    def deco(fn: F) -> F:
        name = tool_name or fn.__name__
        is_async = asyncio.iscoroutinefunction(fn)

        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            return await _run(emitter, agent_id, name, source, session_id,
                              trace_id, redaction_keys, fn, args, kwargs)

        @functools.wraps(fn)
        def swrapper(*args, **kwargs):
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(
                _run(emitter, agent_id, name, source, session_id, trace_id,
                     redaction_keys, fn, args, kwargs))

        return awrapper if is_async else swrapper  # type: ignore[return-value]
    return deco


async def _run(emitter, agent_id, name, source, session_id, trace_id,
               redaction_keys, fn, args, kwargs) -> Any:
    started = time.perf_counter()
    error: str | None = None
    status = "ok"
    result_text: str | None = None
    try:
        # Best-effort signature binding so we can record named arguments.
        try:
            import inspect
            sig = inspect.signature(fn)
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            named = dict(bound.arguments)
        except (TypeError, ValueError):
            named = {}
        result = await fn(*args, **kwargs) if asyncio.iscoroutinefunction(fn) else fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        result_text = str(result)[:4000] if result is not None else None
        return result
    except Exception as e:  # noqa: BLE001
        status = "error"
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000.0
        tier = risk_tier_for(name)
        event = AuditEvent(
            agent_id=agent_id, source=source, session_id=session_id,
            trace_id=trace_id, tool_name=name, method=name,
            arguments_redacted=redact(named, redaction_keys) if 'named' in locals() else None,
            result_summary=AuditEvent.result_summary_for(
                status=status,
                data_classes=_infer_data_classes(locals().get("named"), result_text),
                size=len(result_text) if result_text else None,
                error=error),
            duration_ms=duration_ms,
            severity=severity_for_tier(tier) if status == "ok" else "critical",
            tags=["middleware", tier],
        )
        try:
            await emitter.emit(event)
        except Exception:  # noqa: BLE001
            pass


class AuditedSession:
    """Transparent audit wrapper around an ``mcp.ClientSession``.

    Every ``tools/call`` and ``resources/read`` is wrapped, redacted and
    emitted. Arguments are redacted; results are summarised (status, size,
    error) — full payloads are NOT persisted, keeping the audit log lean.
    """

    def __init__(self, session, emitter: AuditEmitter, agent_id: str,
                 mcp_server: str | None = None, session_id: str | None = None,
                 trace_id: str | None = None):
        self._session = session
        self.emitter = emitter
        self.agent_id = agent_id
        self.mcp_server = mcp_server
        self.session_id = session_id
        self.trace_id = trace_id

    async def initialize(self):
        return await self._session.initialize()

    async def list_tools(self):
        return await self._session.list_tools()

    async def call_tool(self, name: str, arguments: dict | None = None):
        started = time.perf_counter()
        error = None
        is_err = False
        try:
            res = await self._session.call_tool(name, arguments=arguments or {})
            is_err = bool(getattr(res, "isError", False))
            text = _content_text(res)
            await self._emit(name, arguments, started,
                             status="error" if is_err else "ok", error=error,
                             result_text=text)
            return res
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            await self._emit(name, arguments, started, status="error", error=error)
            raise

    async def read_resource(self, uri: str, **kwargs):
        started = time.perf_counter()
        try:
            res = await self._session.read_resource(uri, **kwargs)
            await self._emit(f"resource:{uri}", {"uri": uri}, started,
                             status="ok", result_text=None)
            return res
        except Exception as e:  # noqa: BLE001
            await self._emit(f"resource:{uri}", {"uri": uri}, started,
                             status="error", error=f"{type(e).__name__}: {e}")
            raise

    async def _emit(self, name, arguments, started, status, error=None,
                    result_text=None):
        duration_ms = (time.perf_counter() - started) * 1000.0
        tier = risk_tier_for(name)
        event = AuditEvent(
            agent_id=self.agent_id, source="mcp_proxy",
            mcp_server=self.mcp_server, session_id=self.session_id,
            trace_id=self.trace_id, method="tools/call", tool_name=name,
            arguments_redacted=redact(arguments or {}),
            result_summary=AuditEvent.result_summary_for(
                status=status,
                data_classes=_infer_data_classes(arguments, result_text),
                size=len(result_text) if result_text else None,
                error=error),
            duration_ms=duration_ms,
            severity=severity_for_tier(tier) if status == "ok" else "critical",
            tags=["middleware", "mcp", tier],
        )
        try:
            await self.emitter.emit(event)
        except Exception:  # noqa: BLE001
            pass


def _content_text(res) -> str | None:
    parts = []
    for block in getattr(res, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts)[:4000] or None
