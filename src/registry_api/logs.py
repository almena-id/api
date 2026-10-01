"""Logging: one line per record on stdout, JSON or text (``REGISTRY_LOG_FORMAT``).

Every record made while a request is served carries its context: the request's
id (``X-Request-ID``, taken from the caller when it is a sane one, made up
otherwise, and echoed in the response), the signed-in user and the tenant the
request acts on, once the dependencies that check them have (``current_session``,
``member_tenant``). They are UUIDs only: no email, name or address is ever
added, and the client's IP is not logged. Extra fields whose names say they
hold a secret or personal data are redacted before they are written.

Each request leaves one access record (``registry_api.access``): method, path
(never the query string), route, status and duration. Successful health checks
leave none: Docker asks every ten seconds.

A JSON record::

    {"timestamp": "2026-09-30T13:01:32.940073+00:00", "level": "INFO",
     "logger": "registry_api.access", "message": "GET /api/v1/tenants/… 200",
     "request_id": "…", "user_id": "…", "tenant_id": "…",
     "method": "GET", "path": "/api/v1/tenants/…", "route": "/api/v1/tenants/{tenant_id}",
     "status": 200, "duration_ms": 12.3}
"""

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
REDACTED = "[redacted]"

# Field names that hold a secret or personal data, whatever the record.
_SENSITIVE = frozenset(
    {
        "authorization",
        "code",
        "cookie",
        "email",
        "id_token",
        "move_ticket",
        "nonce",
        "password",
        "poll",
        "proof",
        "secret",
        "state",
        "ticket",
        "token",
    }
)
_SENSITIVE_SUFFIXES = ("_key", "_password", "_secret", "_token")

# What every LogRecord has; anything else on a record came in `extra`.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    # uvicorn's copy of its message with terminal colours.
    "color_message",
}

access_log = logging.getLogger("registry_api.access")


@dataclass
class RequestContext:
    request_id: str
    user_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None


# One object per request: dependencies fill it in place, so what they learn
# reaches the access record however the context was copied on the way.
_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def set_user(user_id: uuid.UUID) -> None:
    """Tie what the current request logs to this user (once their session holds)."""
    if (context := _context.get()) is not None:
        context.user_id = user_id


def set_tenant(tenant_id: uuid.UUID) -> None:
    """Tie what the current request logs to this tenant (once membership is checked)."""
    if (context := _context.get()) is not None:
        context.tenant_id = tenant_id


def _is_sensitive(name: str) -> bool:
    name = name.lower().replace("-", "_")
    return name in _SENSITIVE or name.endswith(_SENSITIVE_SUFFIXES)


def _redact(name: str, value: Any) -> Any:
    if _is_sensitive(name):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(name, v) for v in value]
    return value


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    """The record's context and extra fields, redacted."""
    fields: dict[str, Any] = {}
    context = getattr(record, "context", None)
    if isinstance(context, RequestContext):
        fields["request_id"] = context.request_id
        if context.user_id is not None:
            fields["user_id"] = str(context.user_id)
        if context.tenant_id is not None:
            fields["tenant_id"] = str(context.tenant_id)
    for name, value in record.__dict__.items():
        if name not in _STANDARD and name != "context" and value is not None:
            fields[name] = _redact(name, value)
    return fields


class _ContextFilter(logging.Filter):
    """Stamps each record with the request it was made in."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "context"):
            record.context = _context.get()
        return True


class _ServerErrorFilter(logging.Filter):
    """Drops uvicorn's report of an unhandled error: the access record has it
    already, with the request's context."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Exception in ASGI application")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


_IN_ACCESS_MESSAGE = frozenset({"method", "path", "status"})


class TextFormatter(logging.Formatter):
    """For a terminal: ``13:01:32.940 INFO registry_api.access [id] message key=value…``."""

    def format(self, record: logging.LogRecord) -> str:
        # The access record's message already says them.
        fields = {k: v for k, v in _fields(record).items() if k not in _IN_ACCESS_MESSAGE}
        request_id = fields.pop("request_id", "-")
        moment = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        line = f"{moment} {record.levelname:<7} {record.name} [{request_id}] {record.getMessage()}"
        if fields:
            line += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


_installed: list[logging.Handler] = []


def configure_logging(level: str, fmt: Literal["json", "text"]) -> None:
    """Send every logger, uvicorn's included, through one stdout handler.

    Handlers installed by others (pytest's, say) are left alone; a second call
    replaces the handler of the first.
    """
    root = logging.getLogger()
    for handler in _installed:
        root.removeHandler(handler)
    _installed.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    root.setLevel(level)
    _installed.append(handler)

    # uvicorn configures its own loggers before it loads the app: hand them to
    # the root. Its access log goes: it has the client's address and the query
    # string, and ours replaces it.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    error_log = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _ServerErrorFilter) for f in error_log.filters):
        error_log.addFilter(_ServerErrorFilter())
    logging.getLogger("uvicorn.access").disabled = True


class RequestContextMiddleware:
    """Gives each HTTP request its context and leaves its access record."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"), ""
        )
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        context = RequestContext(request_id)
        token = _context.set(context)
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            _access(scope, 500, started, failed=True)
            raise
        else:
            _access(scope, status, started, failed=False)
        finally:
            _context.reset(token)


def _template(scope: Scope) -> str | None:
    """The matched route's path template: the path with each parameter's value
    put back as ``{name}``. (FastAPI's route knows only its own part of it,
    without the prefixes of the routers it was included through.)"""
    if "route" not in scope:
        return None
    names = {str(value): name for name, value in scope.get("path_params", {}).items()}
    path: str = scope["path"]
    return "/".join(f"{{{names[part]}}}" if part in names else part for part in path.split("/"))


def _access(scope: Scope, status: int, started: float, *, failed: bool) -> None:
    path: str = scope["path"]
    if status < 400 and (path == "/health" or path.startswith("/health/")):
        return
    access_log.log(
        logging.ERROR if failed else logging.INFO,
        "%s %s %d",
        scope["method"],
        path,
        status,
        exc_info=failed,
        extra={
            "method": scope["method"],
            "path": path,
            "route": _template(scope),
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
