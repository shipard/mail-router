from __future__ import annotations

import asyncio
import contextlib
import email
import logging
import os
import uuid
from email import policy as email_policy
from pathlib import Path
from typing import Any

from aiosmtpd.lmtp import LMTP

from .address import parse_recipient
from .lookup import LookupTable
from .parser import idempotency_key
from .queue import Queue

log = logging.getLogger(__name__)


def _extract_message_id(raw: bytes) -> str | None:
    """Pull Message-ID from a raw .eml without parsing the whole body."""
    try:
        # Read only headers — cheap.
        msg = email.message_from_bytes(raw, policy=email_policy.default)
    except Exception:  # noqa: BLE001
        return None
    mid = msg.get("Message-ID") or msg.get("Message-Id") or msg.get("message-id")
    if not mid:
        return None
    return str(mid).strip()


class LMTPHandler:
    """aiosmtpd handler: validates recipients and durably writes each mail
    into the SQLite queue before ACKing 250."""

    def __init__(self, *, queue: Queue, lookup: LookupTable) -> None:
        self._queue = queue
        self._lookup = lookup

    async def handle_RCPT(
        self,
        server: Any,
        session: Any,
        envelope: Any,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        parsed = parse_recipient(address, self._lookup.allowed_domains)
        if parsed is None:
            return "550 5.1.1 Unknown recipient (malformed address or domain)"
        if self._lookup.resolve(parsed.ds_id) is None:
            return "550 5.1.1 Unknown recipient"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(
        self,
        server: Any,
        session: Any,
        envelope: Any,
    ) -> str:
        raw: bytes = envelope.content or b""
        sender: str = envelope.mail_from or ""
        message_id = _extract_message_id(raw)

        try:
            for rcpt in envelope.rcpt_tos:
                parsed = parse_recipient(rcpt, self._lookup.allowed_domains)
                if parsed is None:
                    # Should not happen — RCPT already validated.
                    continue
                if message_id:
                    local_part = rcpt.split("@", 1)[0]
                    key = idempotency_key(parsed.domain, local_part, message_id)
                else:
                    # No Message-ID → no cross-retry dedup. Use a random key so the
                    # UNIQUE(idempotency_key) constraint still holds.
                    key = f"nomid-{uuid.uuid4().hex}"

                item_id, created = self._queue.enqueue(
                    sender_email=sender,
                    recipient_email=rcpt,
                    ds_id=parsed.ds_id,
                    mailbox=parsed.mailbox,
                    idempotency_key=key,
                    raw_eml=raw,
                )
                log.info(
                    "mail_enqueued" if created else "mail_duplicate",
                    extra={
                        "id": item_id,
                        "ds_id": parsed.ds_id,
                        "mailbox": parsed.mailbox,
                        "recipient": rcpt,
                        "sender": sender,
                        "size": len(raw),
                        "message_id": message_id,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("enqueue_failed", extra={"err": str(exc)})
            return "451 4.3.0 Temporary failure, try again later"

        return "250 OK"


class LMTPReceiver:
    """Unix-socket LMTP server. Lifecycle: start → serve_forever → stop."""

    def __init__(
        self,
        *,
        socket_path: str | Path,
        queue: Queue,
        lookup: LookupTable,
        socket_mode: int = 0o660,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._handler = LMTPHandler(queue=queue, lookup=lookup)
        self._socket_mode = socket_mode
        self._server: asyncio.AbstractServer | None = None

    def _factory(self) -> LMTP:
        return LMTP(self._handler, enable_SMTPUTF8=True)

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        loop = asyncio.get_running_loop()
        self._server = await loop.create_unix_server(
            self._factory, path=str(self._socket_path)
        )
        os.chmod(self._socket_path, self._socket_mode)
        log.info("lmtp_listening", extra={"socket": str(self._socket_path)})

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                self._socket_path.unlink()
