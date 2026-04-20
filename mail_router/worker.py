from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .alerts import Alerter
from .client import ShpdClient
from .config import WorkerConfig
from .lookup import LookupTable
from .models import QueueItem
from .parser import ParseError, parse_eml
from .queue import Queue

log = logging.getLogger(__name__)


class Worker:
    """Polls the SQLite queue, posts to shpd, retries with backoff."""

    def __init__(
        self,
        *,
        queue: Queue,
        lookup: LookupTable,
        client: ShpdClient,
        alerter: Alerter,
        config: WorkerConfig,
        queue_size_threshold: int,
    ) -> None:
        self._queue = queue
        self._lookup = lookup
        self._client = client
        self._alerter = alerter
        self._config = config
        self._queue_size_threshold = queue_size_threshold
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        requeued = self._queue.requeue_in_flight()
        if requeued:
            log.info("requeued_in_flight", extra={"count": requeued})
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._config.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        """One poll cycle — public so tests can drive it deterministically."""
        try:
            batch = self._queue.dequeue_batch(self._config.batch_size)
        except Exception:  # noqa: BLE001
            log.exception("dequeue_failed")
            return

        for item in batch:
            await self._process(item)

        self._maybe_alert_queue_size()

    async def _process(self, item: QueueItem) -> None:
        log.info(
            "processing",
            extra={
                "id": item.id,
                "ds_id": item.ds_id,
                "attempt": item.attempt_count + 1,
            },
        )

        ds_config = self._lookup.resolve(item.ds_id)
        if ds_config is None:
            self._dead_letter(item, f"ds_id not in lookup: {item.ds_id}")
            return

        try:
            parsed = parse_eml(item.raw_eml)
        except ParseError as exc:
            self._dead_letter(item, f"parse_error: {exc}")
            return

        result = await self._client.send(
            ds_config=ds_config,
            mailbox=item.mailbox,
            received_at_fallback=item.received_at.astimezone(UTC)
                if item.received_at.tzinfo
                else item.received_at.replace(tzinfo=UTC),
            parsed=parsed,
            raw_eml=item.raw_eml,
            idempotency_key=item.idempotency_key if not item.idempotency_key.startswith("nomid-") else None,
        )

        if result.success:
            self._queue.mark_delivered(item.id, result.message_id)
            log.info(
                "delivered",
                extra={
                    "id": item.id,
                    "ds_id": item.ds_id,
                    "message_id": result.message_id,
                    "replay": result.idempotent_replay,
                },
            )
            return

        if not result.retry:
            self._dead_letter(item, f"HTTP {result.status_code}: {result.error}")
            return

        next_attempt = item.attempt_count + 1
        if next_attempt >= self._config.max_attempts:
            self._dead_letter(
                item,
                f"max_attempts exceeded after HTTP {result.status_code}: {result.error}",
            )
            return

        delay = self._backoff_seconds(next_attempt)
        self._queue.mark_retry(
            item.id,
            attempt_count=next_attempt,
            next_attempt_at=datetime.now(UTC) + timedelta(seconds=delay),
            last_error=f"HTTP {result.status_code}: {result.error}",
        )
        log.warning(
            "retry_scheduled",
            extra={
                "id": item.id,
                "attempt": next_attempt,
                "delay_s": delay,
                "status": result.status_code,
            },
        )

    def _backoff_seconds(self, attempt: int) -> int:
        """Lookup seconds for the given 1-based attempt number; clamp to last."""
        idx = min(attempt, len(self._config.backoff) - 1)
        return int(self._config.backoff[idx])

    def _dead_letter(self, item: QueueItem, reason: str) -> None:
        self._queue.mark_dead_letter(item.id, reason)
        log.error(
            "dead_letter",
            extra={"id": item.id, "ds_id": item.ds_id, "reason": reason},
        )
        self._alerter.notify(
            event="dead_letter",
            subject=f"mail dead-lettered (#{item.id})",
            body=(
                f"Item #{item.id}\n"
                f"From: {item.sender_email}\n"
                f"To:   {item.recipient_email}\n"
                f"DS:   {item.ds_id}\n"
                f"Reason: {reason}\n"
            ),
        )

    def _maybe_alert_queue_size(self) -> None:
        stats = self._queue.stats()
        pending = stats.get("pending", 0)
        if pending > self._queue_size_threshold:
            self._alerter.notify(
                event="queue_backlog",
                subject=f"queue backlog {pending} > {self._queue_size_threshold}",
                body=f"Queue stats: {stats}\n",
            )
