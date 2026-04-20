from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mail_router.queue import Queue


@pytest.fixture
def queue(tmp_path: Path) -> Queue:
    return Queue(tmp_path / "q.db")


def _enq(queue: Queue, key: str = "k1", ds: str = "firma-xyz") -> int:
    item_id, _ = queue.enqueue(
        sender_email="alice@example.com",
        recipient_email=f"{ds}@shipard.email",
        ds_id=ds,
        mailbox=None,
        idempotency_key=key,
        raw_eml=b"From: x\r\n\r\nbody",
    )
    return item_id


def test_enqueue_creates_row(queue: Queue):
    item_id, created = queue.enqueue(
        sender_email="a@x", recipient_email="b@y", ds_id="ds1",
        mailbox=None, idempotency_key="k", raw_eml=b"x",
    )
    assert created is True
    assert item_id > 0


def test_enqueue_idempotent(queue: Queue):
    id1, c1 = queue.enqueue(
        sender_email="a@x", recipient_email="b@y", ds_id="ds1",
        mailbox=None, idempotency_key="same", raw_eml=b"x",
    )
    id2, c2 = queue.enqueue(
        sender_email="a@x", recipient_email="b@y", ds_id="ds1",
        mailbox=None, idempotency_key="same", raw_eml=b"y",
    )
    assert c1 is True
    assert c2 is False
    assert id1 == id2


def test_dequeue_batch_moves_to_in_flight(queue: Queue):
    _enq(queue, "a")
    _enq(queue, "b")
    batch = queue.dequeue_batch(10)
    assert len(batch) == 2
    # Subsequent dequeue finds nothing — they're in_flight.
    assert queue.dequeue_batch(10) == []


def test_dequeue_respects_next_attempt_at(queue: Queue):
    item_id = _enq(queue, "k")
    # Move to in_flight then simulate retry scheduled in future.
    queue.dequeue_batch(10)
    queue.mark_retry(
        item_id, attempt_count=1,
        next_attempt_at=datetime.now(UTC) + timedelta(minutes=5),
        last_error="boom",
    )
    assert queue.dequeue_batch(10) == []


def test_mark_delivered(queue: Queue):
    item_id = _enq(queue)
    queue.dequeue_batch(10)
    queue.mark_delivered(item_id, "MSG-1")
    it = queue.get(item_id)
    assert it is not None
    assert it.state == "delivered"
    assert it.delivered_message_id == "MSG-1"


def test_mark_dead_letter(queue: Queue):
    item_id = _enq(queue)
    queue.dequeue_batch(10)
    queue.mark_dead_letter(item_id, "bad request")
    it = queue.get(item_id)
    assert it is not None
    assert it.state == "dead_letter"
    assert it.dead_letter_reason == "bad request"


def test_requeue_in_flight(queue: Queue):
    _enq(queue, "a")
    _enq(queue, "b")
    queue.dequeue_batch(10)
    assert queue.stats().get("in_flight") == 2
    touched = queue.requeue_in_flight()
    assert touched == 2
    assert queue.stats().get("pending") == 2


def test_stats(queue: Queue):
    _enq(queue, "a")
    _enq(queue, "b")
    stats = queue.stats()
    assert stats.get("pending") == 2


def test_prune_delivered(queue: Queue):
    item_id = _enq(queue)
    queue.dequeue_batch(10)
    queue.mark_delivered(item_id, "MSG")
    # Prune with days=0 should catch anything older than "now" — but delivered_at
    # is exactly now. Accept 0 prunes; bump by going 1 day into the future.
    import time as _time
    _time.sleep(0.01)
    n = queue.prune_delivered(days=-1)  # everything delivered before (now+1day)
    assert n >= 1
