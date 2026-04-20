from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ParsedAddress:
    ds_id: str
    mailbox: str | None
    domain: str


@dataclass(frozen=True)
class DsConfig:
    ds_id: str
    api_url: str
    api_token: str


@dataclass
class QueueItem:
    id: int
    state: str
    received_at: datetime
    sender_email: str
    recipient_email: str
    ds_id: str
    mailbox: str | None
    idempotency_key: str
    raw_eml: bytes
    attempt_count: int
    next_attempt_at: datetime | None
    last_error: str | None
    delivered_at: datetime | None
    delivered_message_id: str | None
    dead_letter_reason: str | None


@dataclass
class Attachment:
    filename: str
    content_type: str
    content: bytes


@dataclass
class ParsedEmail:
    subject: str
    sender_email: str
    sender_name: str | None
    message_id: str | None
    in_reply_to: str | None
    references: str | None
    date: datetime | None
    body_plain: str | None
    body_html: str | None
    attachments: list[Attachment] = field(default_factory=list)
