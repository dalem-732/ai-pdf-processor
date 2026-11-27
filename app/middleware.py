"""ASGI-защита HTTP-слоя: request-id, заголовки, ключ, лимиты до чтения тела."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from app.config import Settings
from app.schemas import ErrorResponse

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_PROTECTED_PREFIX = "/api/v1/summarize"
_MAX_TRACKED_CLIENTS = 10_000

Scope = dict[str, Any]
Message = dict[str, Any]


class _BodyTooLarge(Exception):
    pass


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _request_id(headers: dict[str, str]) -> str:
    candidate = headers.get(REQUEST_ID_HEADER.lower(), "").strip()
    if _REQUEST_ID_RE.match(candidate):
        return candidate
    return new_request_id()


def content_security_policy(script_hashes: list[str]) -> str:
    scripts = " ".join(f"'sha256-{value}'" for value in script_hashes)
    return (
        "default-src 'none'; "
        f"script-src {scripts}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )


def script_hash(source: str) -> str:
    return base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode()


class RateLimiter:
    """Скользящее окно запросов на клиента, в памяти процесса."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str, *, limit: int, window: float) -> float | None:
        if limit <= 0:
            return None
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1.0, window - (now - hits[0]))
        hits.append(now)
        if len(self._hits) > _MAX_TRACKED_CLIENTS:
            self._purge(now, window)
        return None

    def _purge(self, now: float, window: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] >= window]:
            del self._hits[key]


class RequestGuardMiddleware:
    def __init__(
        self,
        app: Callable[..., Any],
        *,
        settings_provider: Callable[[], Settings],
        page_csp: str,
    ) -> None:
        self.app = app
        self.settings_provider = settings_provider
        self.page_csp = page_csp
        self.limiter = RateLimiter()

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        request_id = _request_id(headers)
        scope.setdefault("state", {})["request_id"] = request_id
        path: str = scope.get("path", "")
        extra_headers = [
            (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
        ]
        if not path.startswith(("/docs", "/redoc")):
            extra_headers.append((b"x-frame-options", b"DENY"))
        if path == "/":
            extra_headers.append((b"content-security-policy", self.page_csp.encode()))

        state: dict[str, Any] = {
            "started": False,
            "too_large": False,
            "replaced": False,
            "detail": "",
        }

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                if state["too_large"]:
                    state["replaced"] = True
                    await self._send_error(
                        send, request_id, extra_headers, 413, "file_too_large", state["detail"]
                    )
                    return
                state["started"] = True
                message = dict(message)
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ] + extra_headers
            elif state["replaced"]:
                return
            await send(message)

        if not path.startswith(_PROTECTED_PREFIX) or scope.get("method") != "POST":
            await self.app(scope, receive, send_wrapper)
            return

        settings = self.settings_provider()
        state["detail"] = f"Файл больше допустимых {settings.max_upload_mb} МБ"

        retry_after = self.limiter.check(
            self._client_ip(scope, headers, settings),
            limit=settings.rate_limit_requests,
            window=settings.rate_limit_window_seconds,
        )
        if retry_after is not None:
            await self._send_error(
                send,
                request_id,
                extra_headers + [(b"retry-after", str(int(retry_after)).encode())],
                429,
                "rate_limited",
                "Слишком много запросов, повторите позже",
            )
            return

        if settings.auth_required and not self._authorized(headers, settings):
            await self._send_error(
                send,
                request_id,
                extra_headers,
                401,
                "unauthorized",
                "Нужен API-ключ: заголовок X-API-Key или Authorization: Bearer",
            )
            return

        limit = settings.max_request_bytes
        declared = headers.get("content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = False
            if too_big:
                await self._send_error(
                    send, request_id, extra_headers, 413, "file_too_large", state["detail"]
                )
                return

        received = 0

        async def receive_wrapper() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    state["too_large"] = True
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except _BodyTooLarge:
            if not state["started"] and not state["replaced"]:
                await self._send_error(
                    send, request_id, extra_headers, 413, "file_too_large", state["detail"]
                )
        except Exception:
            if state["too_large"] and not state["started"]:
                if not state["replaced"]:
                    await self._send_error(
                        send, request_id, extra_headers, 413, "file_too_large", state["detail"]
                    )
                return
            raise

    @staticmethod
    def _authorized(headers: dict[str, str], settings: Settings) -> bool:
        expected = settings.api_key.get_secret_value() if settings.api_key else ""
        provided = headers.get("x-api-key", "")
        authorization = headers.get("authorization", "")
        if not provided and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        return bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())

    @staticmethod
    def _client_ip(scope: Scope, headers: dict[str, str], settings: Settings) -> str:
        if settings.trust_proxy_headers or os.environ.get("RAILWAY_PROJECT_ID"):
            real_ip = headers.get("x-real-ip", "").strip()
            if real_ip:
                return real_ip
            forwarded = headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[-1].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    @staticmethod
    async def _send_error(
        send: Callable,
        request_id: str,
        extra_headers: list[tuple[bytes, bytes]],
        status: int,
        code: str,
        detail: str,
    ) -> None:
        body = json.dumps(
            ErrorResponse(error=code, detail=detail, request_id=request_id).model_dump(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ]
                + extra_headers,
            }
        )
        await send({"type": "http.response.body", "body": body})
