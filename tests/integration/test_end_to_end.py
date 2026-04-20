"""End-to-end: LMTP → queue → worker → fake shpd (via httpx MockTransport).

Covers §10.2 scenarios from tasks/phase1.md:
  1. Happy path: mail → queue → delivered
  2. 422 → dead_letter (no retry)
  3. 500 → retry → eventual delivered
  4. Persistent 500 × 4 → dead_letter
  5. Duplicate Message-ID → queue deduplicates
"""
from __future__ import annotations

import asyncio
import json
import smtplib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from mail_router.alerts import Alerter
from mail_router.client import ShpdClient
from mail_router.config import AlertsConfig, WorkerConfig
from mail_router.lookup import LookupTable
from mail_router.queue import Queue
from mail_router.receiver import LMTPReceiver
from mail_router.worker import Worker

SAMPLE_EML = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: firma-xyz@shipard.email\r\n"
    b"Subject: hello\r\n"
    b"Message-ID: <msg-001@example.com>\r\n"
    b"Date: Mon, 18 Apr 2026 14:32:00 +0200\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"hello world\r\n"
)


class _FakeShpd:
    """Queue of pre-scripted httpx.Response objects, one popped per call."""
    def __init__(self) -> None:
        self.responses: list[httpx.Response] = []
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self.responses:
            return httpx.Response(500, json={
                "success": False,
                "error": {"code": "UNCONFIGURED", "message": "no response scripted"},
            })
        return self.responses.pop(0)


@pytest.fixture
async def pipeline(tmp_path: Path):
    lookup_file = tmp_path / "lookup.json"
    lookup_file.write_text(json.dumps({
        "hosts": ["shipard.email"],
        "data_sources": {
            "firma-xyz": {"api_url": "https://shpd.example.com", "api_token": "tok"}
        },
    }))
    lookup = LookupTable(lookup_file)
    queue = Queue(tmp_path / "q.db")

    # LMTP receiver on a tmp Unix socket.
    sock_path = tmp_path / "lmtp.sock"
    receiver = LMTPReceiver(socket_path=sock_path, queue=queue, lookup=lookup)
    await receiver.start()
    serve_task = asyncio.create_task(receiver.serve_forever())

    # Worker with MockTransport-backed httpx client.
    fake = _FakeShpd()
    transport = httpx.MockTransport(fake.handler)
    httpx_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    shpd_client = ShpdClient(client=httpx_client)
    worker = Worker(
        queue=queue,
        lookup=lookup,
        client=shpd_client,
        alerter=Alerter(AlertsConfig(enabled=False)),
        config=WorkerConfig(poll_interval=0.01, batch_size=10, max_attempts=4,
                            http_timeout=5.0, backoff=[0, 60, 300, 1800]),
        queue_size_threshold=100,
    )

    ctx: dict[str, Any] = {
        "queue": queue, "worker": worker, "fake": fake,
        "sock": str(sock_path), "lookup": lookup,
    }
    yield ctx

    serve_task.cancel()
    await receiver.stop()
    await shpd_client.close()
    queue.close()


def _lmtp_send(sock: str, sender: str, rcpts: list[str], data: bytes) -> None:
    client = smtplib.LMTP(sock)
    try:
        client.sendmail(sender, rcpts, data)
    finally:
        client.quit()


async def _send_in_thread(sock: str, sender: str, rcpts: list[str], data: bytes) -> None:
    await asyncio.to_thread(_lmtp_send, sock, sender, rcpts, data)


@pytest.mark.asyncio
async def test_happy_path(pipeline):
    pipeline["fake"].responses.append(httpx.Response(
        201, json={"success": True, "data": {"ndx": 1, "message_id": "MSG-1", "idempotent_replay": False}}
    ))
    await _send_in_thread(pipeline["sock"], "alice@example.com", ["firma-xyz@shipard.email"], SAMPLE_EML)

    # Mail sits pending in the queue.
    stats = pipeline["queue"].stats()
    assert stats.get("pending") == 1

    await pipeline["worker"].tick()
    stats = pipeline["queue"].stats()
    assert stats.get("delivered") == 1
    assert len(pipeline["fake"].calls) == 1


@pytest.mark.asyncio
async def test_422_dead_letter(pipeline):
    pipeline["fake"].responses.append(httpx.Response(422, json={
        "success": False, "error": {"code": "VALIDATION_ERROR", "message": "bad"},
    }))
    await _send_in_thread(pipeline["sock"], "alice@example.com", ["firma-xyz@shipard.email"], SAMPLE_EML)
    await pipeline["worker"].tick()
    assert pipeline["queue"].stats().get("dead_letter") == 1


@pytest.mark.asyncio
async def test_500_then_success(pipeline):
    pipeline["fake"].responses.extend([
        httpx.Response(500, json={"success": False, "error": {"code": "X", "message": "boom"}}),
        httpx.Response(201, json={"success": True, "data": {"ndx": 1, "message_id": "MSG", "idempotent_replay": False}}),
    ])
    await _send_in_thread(pipeline["sock"], "alice@example.com", ["firma-xyz@shipard.email"], SAMPLE_EML)
    await pipeline["worker"].tick()
    # First attempt: scheduled for retry.
    stats = pipeline["queue"].stats()
    assert stats.get("pending") == 1
    # Make it due.
    item = next(pipeline["queue"].iter_all())
    pipeline["queue"].mark_retry(
        item.id, attempt_count=item.attempt_count,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        last_error="",
    )
    await pipeline["worker"].tick()
    assert pipeline["queue"].stats().get("delivered") == 1


@pytest.mark.asyncio
async def test_persistent_500_dead_letter(pipeline):
    for _ in range(4):
        pipeline["fake"].responses.append(httpx.Response(500, json={
            "success": False, "error": {"code": "X", "message": "boom"},
        }))
    await _send_in_thread(pipeline["sock"], "alice@example.com", ["firma-xyz@shipard.email"], SAMPLE_EML)
    for _ in range(4):
        await pipeline["worker"].tick()
        stats = pipeline["queue"].stats()
        if stats.get("pending"):
            item = next(i for i in pipeline["queue"].iter_all() if i.state == "pending")
            pipeline["queue"].mark_retry(
                item.id, attempt_count=item.attempt_count,
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
                last_error="",
            )
    assert pipeline["queue"].stats().get("dead_letter") == 1


@pytest.mark.asyncio
async def test_duplicate_message_id_dedup(pipeline):
    pipeline["fake"].responses.append(httpx.Response(
        201, json={"success": True, "data": {"ndx": 1, "message_id": "MSG", "idempotent_replay": False}}
    ))
    await _send_in_thread(pipeline["sock"], "alice@example.com", ["firma-xyz@shipard.email"], SAMPLE_EML)
    await _send_in_thread(pipeline["sock"], "alice@example.com", ["firma-xyz@shipard.email"], SAMPLE_EML)
    # Exactly one row — second insert collided on idempotency_key.
    rows = list(pipeline["queue"].iter_all())
    assert len(rows) == 1
    await pipeline["worker"].tick()
    assert len(pipeline["fake"].calls) == 1
    assert pipeline["queue"].stats().get("delivered") == 1
