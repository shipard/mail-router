from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .models import DsConfig, ParsedEmail

log = logging.getLogger(__name__)

ENDPOINT_PATH = "/api/v1/_mail/incoming"

# HTTP status classes we explicitly handle.
_DEAD_LETTER_STATUSES = {400, 401, 403, 404, 409, 413, 415, 422}


@dataclass
class DeliveryResult:
    status_code: int
    retry: bool
    message_id: str | None
    error: str | None
    idempotent_replay: bool

    @property
    def success(self) -> bool:
        return self.status_code == 201 and self.error is None


def _iso8601_with_tz(dt: datetime | None, fallback: datetime) -> str:
    target = dt or fallback
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return target.isoformat(timespec="seconds")


class ShpdClient:
    """httpx async client wrapping POST /api/v1/_mail/incoming.

    One instance per worker; connection pool reused across calls.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(
        self,
        *,
        ds_config: DsConfig,
        mailbox: str | None,
        received_at_fallback: datetime,
        parsed: ParsedEmail,
        raw_eml: bytes,
        idempotency_key: str | None,
    ) -> DeliveryResult:
        url = ds_config.api_url + ENDPOINT_PATH

        # httpx 0.28 requires data as a dict when combined with files= (passing
        # a list of tuples falls back to a sync request body and crashes under
        # AsyncClient). All fields here have unique keys, so dict is safe.
        data: dict[str, str] = {
            "received_at": _iso8601_with_tz(parsed.date, received_at_fallback),
            "sender_email": parsed.sender_email or "",
            "subject": parsed.subject or "",
        }
        if mailbox:
            data["mailbox"] = mailbox
        if parsed.message_id:
            data["external_message_id"] = parsed.message_id
        if parsed.sender_name:
            data["sender_name"] = parsed.sender_name
        if parsed.body_plain:
            data["body_plain"] = parsed.body_plain
        if parsed.body_html:
            data["body_html"] = parsed.body_html
        if parsed.in_reply_to:
            data["in_reply_to"] = parsed.in_reply_to
        if parsed.references:
            data["reply_references"] = parsed.references

        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("raw_source", ("message.eml", raw_eml, "message/rfc822")),
        ]
        for att in parsed.attachments:
            files.append(
                ("attachments[]", (att.filename, att.content, att.content_type))
            )

        headers = {
            "Authorization": f"Bearer {ds_config.api_token}",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key

        try:
            response = await self._client.post(url, data=data, files=files, headers=headers)
        except httpx.TimeoutException as exc:
            log.warning("shpd_timeout", extra={"ds_id": ds_config.ds_id, "err": str(exc)})
            return DeliveryResult(
                status_code=0,
                retry=True,
                message_id=None,
                error=f"timeout: {exc}",
                idempotent_replay=False,
            )
        except httpx.HTTPError as exc:
            log.warning("shpd_http_error", extra={"ds_id": ds_config.ds_id, "err": str(exc)})
            return DeliveryResult(
                status_code=0,
                retry=True,
                message_id=None,
                error=f"network: {exc}",
                idempotent_replay=False,
            )

        return self._interpret(response)

    @staticmethod
    def _interpret(response: httpx.Response) -> DeliveryResult:
        status = response.status_code
        body_text = response.text[:2000]  # cap for logging / DLQ reason
        parsed_body: dict[str, Any] | None = None
        try:
            parsed_body = response.json()
        except ValueError:
            parsed_body = None

        if status == 201 and parsed_body and parsed_body.get("success"):
            data = parsed_body.get("data") or {}
            return DeliveryResult(
                status_code=201,
                retry=False,
                message_id=data.get("message_id"),
                error=None,
                idempotent_replay=bool(data.get("idempotent_replay")),
            )

        error_msg: str
        if parsed_body and isinstance(parsed_body.get("error"), dict):
            err = parsed_body["error"]
            error_msg = f"{err.get('code', '?')}: {err.get('message', '')}".strip(": ")
        else:
            error_msg = f"HTTP {status}: {body_text}"

        retry = status not in _DEAD_LETTER_STATUSES and status >= 500
        # Unexpected 2xx (non-201) or 3xx should not retry silently — push to DLQ.
        if 200 <= status < 500 and status != 201:
            retry = False

        return DeliveryResult(
            status_code=status,
            retry=retry,
            message_id=None,
            error=error_msg,
            idempotent_replay=False,
        )
