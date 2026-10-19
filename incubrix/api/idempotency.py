"""ASGI middleware that makes keyed POST requests safe across API processes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from incubrix.config import Settings, get_settings
from incubrix.db.engine import get_session_factory
from incubrix.db.models import IdempotencyRecord


class IdempotencyMiddleware:
    """Persist final POST responses and replay them while rejecting key reuse."""

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings | None = None,
    ) -> None:
        self.app = app
        self.settings = settings or get_settings()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].startswith("/api/v1/")
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        key = headers.get(b"idempotency-key", b"").decode("latin-1").strip()
        if not key:
            if scope.get("method") == "POST" and scope.get("path", "").startswith("/api/v1/"):
                await self._send_error(
                    send, 422, "VALIDATION_ERROR", "Idempotency-Key header is required."
                )
                return
            await self.app(scope, receive, send)
            return
        if len(key) > 255:
            await self._send_error(send, 422, "VALIDATION_ERROR", "Idempotency-Key is too long.")
            return

        body_file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        digest = hashlib.sha256()
        size = 0
        try:
            content_length = headers.get(b"content-length")
            if content_length and int(content_length) > self.settings.MAX_UPLOAD_BYTES:
                await self._send_error(
                    send, 413, "PAYLOAD_TOO_LARGE", "Request body exceeds the configured limit."
                )
                return
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.settings.MAX_UPLOAD_BYTES:
                    await self._send_error(
                        send, 413, "PAYLOAD_TOO_LARGE", "Request body exceeds the configured limit."
                    )
                    return
                body_file.write(chunk)
                digest.update(chunk)
                if not message.get("more_body", False):
                    break

            route = scope["path"]
            request_hash = hashlib.sha256(
                scope["method"].encode() + b"\n" + route.encode() + b"\n" + digest.digest()
            ).hexdigest()
            outcome = await run_in_threadpool(self._reserve, key, route, request_hash)
            if outcome[0] == "conflict":
                await self._send_error(
                    send,
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "This Idempotency-Key was already used for a different request.",
                )
                return
            if outcome[0] == "complete":
                await self._send_stored(send, outcome[1], outcome[2])
                return
            if outcome[0] == "pending":
                for _ in range(300):
                    await asyncio.sleep(0.1)
                    outcome = await run_in_threadpool(self._lookup, key, route, request_hash)
                    if outcome[0] != "pending":
                        break
                if outcome[0] == "complete":
                    await self._send_stored(send, outcome[1], outcome[2])
                    return
                if outcome[0] == "conflict":
                    await self._send_error(
                        send,
                        409,
                        "IDEMPOTENCY_KEY_REUSED",
                        "This Idempotency-Key was already used for a different request.",
                    )
                    return
                await self._send_error(
                    send,
                    409,
                    "IDEMPOTENCY_IN_PROGRESS",
                    "A request with this key is still in progress; retry shortly.",
                    retryable=True,
                )
                return

            body_file.seek(0)
            body_complete = size == 0

            async def replay_receive() -> Message:
                nonlocal body_complete
                if body_complete:
                    return {"type": "http.request", "body": b"", "more_body": False}
                chunk = body_file.read(65_536)
                if not chunk:
                    body_complete = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": body_file.tell() < size,
                }

            response_start: Message | None = None
            response_body = bytearray()

            async def capture_send(message: Message) -> None:
                nonlocal response_start
                if message["type"] == "http.response.start":
                    response_start = message
                elif message["type"] == "http.response.body":
                    response_body.extend(message.get("body", b""))

            try:
                await self.app(scope, replay_receive, capture_send)
            except Exception:
                response_start = {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                }
                response_body = bytearray(
                    b'{"error":{"code":"INTERNAL_ERROR","message":"An unexpected error occurred.",'
                    b'"retryable":true,"details":null}}'
                )

            if response_start is None:
                await run_in_threadpool(self._release, key, request_hash)
                await self._send_error(send, 500, "INTERNAL_ERROR", "An unexpected error occurred.")
                return
            response_status = int(response_start["status"])
            response_text = bytes(response_body).decode("utf-8")
            await run_in_threadpool(
                self._complete,
                key,
                request_hash,
                response_status,
                response_text,
            )
            response_start["headers"] = [
                (name, value)
                for name, value in response_start.get("headers", [])
                if name.lower() != b"content-length"
            ]
            await send(response_start)
            await send(
                {
                    "type": "http.response.body",
                    "body": bytes(response_body),
                    "more_body": False,
                }
            )
        finally:
            body_file.close()

    def _reserve(self, key: str, route: str, request_hash: str) -> tuple[str, Any, Any]:
        now = datetime.now(timezone.utc)
        sessions = get_session_factory(self.settings.DATABASE_URL)
        try:
            with sessions.begin() as session:
                existing = session.execute(
                    select(IdempotencyRecord).where(IdempotencyRecord.key == key).with_for_update()
                ).scalar_one_or_none()
                if existing is not None and existing.expires_at <= now:
                    session.delete(existing)
                    session.flush()
                    existing = None
                if existing is not None:
                    if existing.route != route or existing.request_hash != request_hash:
                        return ("conflict", None, None)
                    if existing.response_status is not None:
                        return ("complete", existing.response_status, existing.response_body or "")
                    return ("pending", None, None)
                session.add(
                    IdempotencyRecord(
                        key=key,
                        route=route,
                        request_hash=request_hash,
                        created_at=now,
                        expires_at=now + timedelta(seconds=self.settings.IDEMPOTENCY_TTL_S),
                    )
                )
                session.flush()
                return ("reserved", None, None)
        except IntegrityError:
            return self._lookup(key, route, request_hash)

    def _lookup(self, key: str, route: str, request_hash: str) -> tuple[str, Any, Any]:
        with get_session_factory(self.settings.DATABASE_URL)() as session:
            existing = session.get(IdempotencyRecord, key)
            if existing is None:
                return ("missing", None, None)
            if existing.route != route or existing.request_hash != request_hash:
                return ("conflict", None, None)
            if existing.response_status is None:
                return ("pending", None, None)
            return ("complete", existing.response_status, existing.response_body or "")

    def _complete(self, key: str, request_hash: str, status: int, body: str) -> None:
        now = datetime.now(timezone.utc)
        with get_session_factory(self.settings.DATABASE_URL).begin() as session:
            record = session.get(IdempotencyRecord, key)
            if record is None or record.request_hash != request_hash:
                raise RuntimeError("Idempotency reservation disappeared before completion.")
            record.response_status = status
            record.response_body = body
            record.expires_at = now + timedelta(seconds=self.settings.IDEMPOTENCY_TTL_S)

    def _release(self, key: str, request_hash: str) -> None:
        with get_session_factory(self.settings.DATABASE_URL).begin() as session:
            record = session.get(IdempotencyRecord, key)
            if record is not None and record.request_hash == request_hash:
                session.delete(record)

    @staticmethod
    async def _send_stored(send: Send, status: int, body: str) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body.encode("utf-8")})

    @staticmethod
    async def _send_error(
        send: Send,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": None,
                }
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
