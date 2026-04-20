from __future__ import annotations

import email
import hashlib
import logging
from datetime import datetime
from email import policy as email_policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

from .models import Attachment, ParsedEmail

log = logging.getLogger(__name__)


class ParseError(Exception):
    """Raised when an .eml cannot be parsed even with lenient policy."""


def _decode_header(value: str | None) -> str:
    if value is None:
        return ""
    # The default email policy on BytesParser already handles RFC 2047.
    return str(value).strip()


def _extract_address(header_value: str | None) -> tuple[str, str | None]:
    """Return (email, display_name_or_None) for first address in header."""
    if not header_value:
        return "", None
    addrs = getaddresses([header_value])
    if not addrs:
        return "", None
    name, addr = addrs[0]
    return addr.strip(), (name.strip() or None)


def _safe_filename(fallback_idx: int, part: EmailMessage) -> str:
    name = part.get_filename()
    if name:
        return name
    ext = ""
    ctype = part.get_content_type()
    if ctype == "text/plain":
        ext = ".txt"
    elif ctype == "text/html":
        ext = ".html"
    elif "/" in ctype:
        ext = "." + ctype.split("/", 1)[1].split(";")[0]
    return f"attachment-{fallback_idx}{ext}"


def _is_attachment(part: EmailMessage) -> bool:
    """Treat as attachment: any non-multipart with Content-Disposition != inline,
    or any non-text part. Inline images with Content-ID are excluded."""
    if part.is_multipart():
        return False
    ctype = part.get_content_type()
    disp = (part.get_content_disposition() or "").lower()
    if disp == "attachment":
        return True
    if ctype.startswith("text/"):
        # Text is only an attachment if explicitly marked.
        return False
    # Non-text, non-attachment: exclude inline with Content-ID (embedded images).
    if part.get("Content-ID"):
        return False
    return True


def _extract_body(msg: EmailMessage) -> tuple[str | None, str | None]:
    plain: str | None = None
    html: str | None = None
    plain_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    if plain_part is not None:
        try:
            plain = plain_part.get_content()
        except Exception as exc:  # noqa: BLE001
            log.warning("body_plain_decode_failed", extra={"err": str(exc)})
    if html_part is not None:
        try:
            html = html_part.get_content()
        except Exception as exc:  # noqa: BLE001
            log.warning("body_html_decode_failed", extra={"err": str(exc)})
    return plain, html


def parse_eml(raw: bytes) -> ParsedEmail:
    try:
        msg: EmailMessage = email.message_from_bytes(raw, policy=email_policy.default)  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"cannot parse MIME: {exc}") from exc

    sender_email, sender_name = _extract_address(msg.get("From"))
    subject = _decode_header(msg.get("Subject"))
    message_id = _decode_header(msg.get("Message-ID")) or None
    in_reply_to = _decode_header(msg.get("In-Reply-To")) or None
    references = _decode_header(msg.get("References")) or None

    date_header = msg.get("Date")
    date_dt: datetime | None = None
    if date_header:
        try:
            date_dt = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            date_dt = None

    body_plain, body_html = _extract_body(msg)

    attachments: list[Attachment] = []
    idx = 0
    for part in msg.walk():
        if part is msg or part.is_multipart():
            continue
        if not _is_attachment(part):
            continue
        idx += 1
        try:
            content = part.get_payload(decode=True) or b""
        except Exception as exc:  # noqa: BLE001
            log.warning("attachment_decode_failed", extra={"err": str(exc)})
            content = b""
        attachments.append(
            Attachment(
                filename=_safe_filename(idx, part),
                content_type=part.get_content_type(),
                content=content,
            )
        )

    return ParsedEmail(
        subject=subject,
        sender_email=sender_email,
        sender_name=sender_name,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        date=date_dt,
        body_plain=body_plain,
        body_html=body_html,
        attachments=attachments,
    )


def idempotency_key(domain: str, local_part: str, message_id: str) -> str:
    payload = f"{domain.lower()}/{local_part}/{message_id}".encode()
    return hashlib.sha256(payload).hexdigest()
