import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from mail_router.alerts import Alerter
from mail_router.client import ShpdClient
from mail_router.config import AlertsConfig, WorkerConfig
from mail_router.lookup import LookupTable
from mail_router.queue import Queue
from mail_router.worker import Worker


@pytest.fixture
async def ctx(tmp_path: Path):
    """Full worker setup with MockTransport-backed httpx client."""
    lookup_file = tmp_path / "lookup.json"
    lookup_file.write_text(json.dumps({
        "hosts": ["shipard.email"],
        "data_sources": {
            "firma-xyz": {"api_url": "https://shpd.example.com", "api_token": "tok"}
        },
    }))
    lookup = LookupTable(lookup_file)
    queue = Queue(tmp_path / "q.db")

    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not responses:
            return httpx.Response(500)
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    httpx_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    client = ShpdClient(client=httpx_client)

    alerter = Alerter(AlertsConfig(enabled=False))

    worker = Worker(
        queue=queue,
        lookup=lookup,
        client=client,
        alerter=alerter,
        config=WorkerConfig(poll_interval=0.01, batch_size=10, max_attempts=4,
                            http_timeout=5.0, backoff=[0, 60, 300, 1800]),
        queue_size_threshold=100,
    )

    def enqueue(key: str = "k1") -> int:
        item_id, _ = queue.enqueue(
            sender_email="alice@example.com",
            recipient_email="firma-xyz@shipard.email",
            ds_id="firma-xyz",
            mailbox=None,
            idempotency_key=key,
            raw_eml=b"From: alice@example.com\r\nTo: firma-xyz@shipard.email\r\nMessage-ID: <m@x>\r\nDate: Mon, 18 Apr 2026 14:32:00 +0200\r\nSubject: t\r\n\r\nbody",
        )
        return item_id

    yield {"queue": queue, "worker": worker, "enqueue": enqueue, "responses": responses, "client": client}

    await client.close()
    queue.close()


@pytest.mark.asyncio
async def test_worker_happy_path(ctx):
    item_id = ctx["enqueue"]()
    ctx["responses"].append(httpx.Response(
        201, json={"success": True, "data": {"ndx": 1, "message_id": "MSG-1", "idempotent_replay": False}}
    ))
    await ctx["worker"].tick()
    item = ctx["queue"].get(item_id)
    assert item.state == "delivered"
    assert item.delivered_message_id == "MSG-1"


@pytest.mark.asyncio
async def test_worker_422_to_dead_letter(ctx):
    item_id = ctx["enqueue"]()
    ctx["responses"].append(httpx.Response(422, json={
        "success": False, "error": {"code": "VALIDATION_ERROR", "message": "bad"},
    }))
    await ctx["worker"].tick()
    item = ctx["queue"].get(item_id)
    assert item.state == "dead_letter"
    assert "VALIDATION_ERROR" in (item.dead_letter_reason or "")


@pytest.mark.asyncio
async def test_worker_500_schedules_retry(ctx):
    item_id = ctx["enqueue"]()
    ctx["responses"].append(httpx.Response(500, json={
        "success": False, "error": {"code": "INTERNAL_ERROR", "message": "boom"},
    }))
    await ctx["worker"].tick()
    item = ctx["queue"].get(item_id)
    assert item.state == "pending"
    assert item.attempt_count == 1
    assert item.next_attempt_at is not None and item.next_attempt_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_worker_retry_then_success(ctx):
    item_id = ctx["enqueue"]()
    ctx["responses"].extend([
        httpx.Response(500, json={"success": False, "error": {"code": "X", "message": "boom"}}),
        httpx.Response(201, json={"success": True, "data": {"ndx": 1, "message_id": "MSG", "idempotent_replay": False}}),
    ])
    await ctx["worker"].tick()
    # Force the next attempt to be due immediately.
    ctx["queue"].mark_retry(
        item_id, attempt_count=1,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        last_error="boom",
    )
    await ctx["worker"].tick()
    item = ctx["queue"].get(item_id)
    assert item.state == "delivered"


@pytest.mark.asyncio
async def test_worker_max_attempts_dead_letter(ctx):
    item_id = ctx["enqueue"]()
    for _ in range(4):
        ctx["responses"].append(httpx.Response(500, json={
            "success": False, "error": {"code": "X", "message": "boom"},
        }))
    # Simulate 4 consecutive failed attempts by resetting next_attempt_at.
    for _ in range(4):
        await ctx["worker"].tick()
        item = ctx["queue"].get(item_id)
        if item.state == "pending":
            ctx["queue"].mark_retry(
                item.id, attempt_count=item.attempt_count,
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
                last_error=item.last_error or "",
            )
    item = ctx["queue"].get(item_id)
    assert item.state == "dead_letter"


@pytest.mark.asyncio
async def test_worker_duplicate_message_id_idempotent(ctx):
    # Enqueue same idempotency key twice — queue deduplicates.
    id1 = ctx["enqueue"]("same-key")
    id2 = ctx["enqueue"]("same-key")
    assert id1 == id2
    ctx["responses"].append(httpx.Response(
        201, json={"success": True, "data": {"ndx": 1, "message_id": "M", "idempotent_replay": False}}
    ))
    await ctx["worker"].tick()
    item = ctx["queue"].get(id1)
    assert item.state == "delivered"
