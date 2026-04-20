from email.message import EmailMessage

import pytest

from mail_router.parser import ParseError, idempotency_key, parse_eml


def _build(
    *,
    subject: str = "Hello",
    from_: str = "Alice <alice@example.com>",
    to: str = "firma-xyz@shipard.email",
    body_plain: str | None = "plain body",
    body_html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    message_id: str | None = "<abc@example.com>",
    date: str | None = "Mon, 18 Apr 2026 14:32:00 +0200",
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_
    msg["To"] = to
    if message_id:
        msg["Message-ID"] = message_id
    if date:
        msg["Date"] = date
    if body_plain is not None and body_html is None:
        msg.set_content(body_plain)
    elif body_plain is None and body_html is not None:
        msg.set_content("")
        msg.add_alternative(body_html, subtype="html")
    elif body_plain is not None and body_html is not None:
        msg.set_content(body_plain)
        msg.add_alternative(body_html, subtype="html")
    for name, data, ctype in attachments or []:
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def test_parse_plain():
    raw = _build()
    p = parse_eml(raw)
    assert p.subject == "Hello"
    assert p.sender_email == "alice@example.com"
    assert p.sender_name == "Alice"
    assert p.body_plain and "plain body" in p.body_plain
    assert p.body_html is None
    assert p.message_id == "<abc@example.com>"
    assert p.date is not None
    assert p.date.utcoffset() is not None


def test_parse_html_only():
    raw = _build(body_plain=None, body_html="<p>hi</p>")
    p = parse_eml(raw)
    assert p.body_html and "<p>hi</p>" in p.body_html


def test_parse_multipart_alternative():
    raw = _build(body_plain="alt plain", body_html="<p>alt</p>")
    p = parse_eml(raw)
    assert p.body_plain and "alt plain" in p.body_plain
    assert p.body_html and "alt" in p.body_html


def test_parse_attachments():
    raw = _build(attachments=[("doc.pdf", b"%PDF-1.4 fake", "application/pdf")])
    p = parse_eml(raw)
    assert len(p.attachments) == 1
    att = p.attachments[0]
    assert att.filename == "doc.pdf"
    assert att.content_type == "application/pdf"
    assert att.content == b"%PDF-1.4 fake"


def test_parse_missing_message_id():
    raw = _build(message_id=None)
    p = parse_eml(raw)
    assert p.message_id is None


def test_parse_unicode_subject():
    raw = _build(subject="Příliš žluťoučký kůň")
    p = parse_eml(raw)
    assert p.subject == "Příliš žluťoučký kůň"


def test_parse_no_date():
    raw = _build(date=None)
    p = parse_eml(raw)
    assert p.date is None


def test_parse_invalid_mime_raises():
    # Non-parseable binary garbage. email library is very lenient, so force
    # a parse error by passing something it cannot interpret at all.
    with pytest.raises(ParseError):
        parse_eml(None)  # type: ignore[arg-type]


def test_idempotency_key_deterministic():
    k1 = idempotency_key("shipard.email", "firma-xyz", "<abc@x>")
    k2 = idempotency_key("shipard.email", "firma-xyz", "<abc@x>")
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex


def test_idempotency_key_domain_case_insensitive():
    assert idempotency_key("Shipard.Email", "x", "<m>") == idempotency_key("shipard.email", "x", "<m>")
