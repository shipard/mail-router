from datetime import UTC, datetime

import httpx
import pytest
import respx

from mail_router.client import ShpdClient
from mail_router.models import DsConfig, ParsedEmail


DS = DsConfig(ds_id="firma-xyz", api_url="https://shpd.example.com", api_token="tok")


def _parsed() -> ParsedEmail:
    return ParsedEmail(
        subject="hi",
        sender_email="alice@example.com",
        sender_name="Alice",
        message_id="<abc@x>",
        in_reply_to=None,
        references=None,
        date=datetime(2026, 4, 18, 14, 32, tzinfo=UTC),
        body_plain="body",
        body_html=None,
        attachments=[],
    )


@pytest.mark.asyncio
@respx.mock
async def test_send_201_success():
    route = respx.post("https://shpd.example.com/api/v1/_mail/incoming").mock(
        return_value=httpx.Response(
            201,
            json={"success": True, "data": {"ndx": 1, "message_id": "MSG-1", "idempotent_replay": False}},
        )
    )
    client = ShpdClient()
    try:
        result = await client.send(
            ds_config=DS,
            mailbox=None,
            received_at_fallback=datetime.now(UTC),
            parsed=_parsed(),
            raw_eml=b"raw",
            idempotency_key="idkey",
        )
    finally:
        await client.close()
    assert result.success
    assert result.message_id == "MSG-1"
    assert result.retry is False
    assert route.called
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.headers["X-Idempotency-Key"] == "idkey"


@pytest.mark.asyncio
@respx.mock
async def test_send_422_dead_letter():
    respx.post("https://shpd.example.com/api/v1/_mail/incoming").mock(
        return_value=httpx.Response(422, json={
            "success": False,
            "error": {"code": "VALIDATION_ERROR", "message": "bad field"},
        })
    )
    client = ShpdClient()
    try:
        result = await client.send(
            ds_config=DS, mailbox=None,
            received_at_fallback=datetime.now(UTC),
            parsed=_parsed(), raw_eml=b"raw", idempotency_key=None,
        )
    finally:
        await client.close()
    assert not result.success
    assert result.retry is False
    assert "VALIDATION_ERROR" in (result.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_send_500_retries():
    respx.post("https://shpd.example.com/api/v1/_mail/incoming").mock(
        return_value=httpx.Response(500, json={
            "success": False,
            "error": {"code": "INTERNAL_ERROR", "message": "boom"},
        })
    )
    client = ShpdClient()
    try:
        result = await client.send(
            ds_config=DS, mailbox=None,
            received_at_fallback=datetime.now(UTC),
            parsed=_parsed(), raw_eml=b"raw", idempotency_key=None,
        )
    finally:
        await client.close()
    assert result.retry is True


@pytest.mark.asyncio
@respx.mock
async def test_send_401_dead_letter():
    respx.post("https://shpd.example.com/api/v1/_mail/incoming").mock(
        return_value=httpx.Response(401, json={
            "success": False,
            "error": {"code": "UNAUTHORIZED", "message": "bad token"},
        })
    )
    client = ShpdClient()
    try:
        result = await client.send(
            ds_config=DS, mailbox=None,
            received_at_fallback=datetime.now(UTC),
            parsed=_parsed(), raw_eml=b"raw", idempotency_key=None,
        )
    finally:
        await client.close()
    assert result.retry is False


@pytest.mark.asyncio
@respx.mock
async def test_send_network_error_retries():
    respx.post("https://shpd.example.com/api/v1/_mail/incoming").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    client = ShpdClient()
    try:
        result = await client.send(
            ds_config=DS, mailbox=None,
            received_at_fallback=datetime.now(UTC),
            parsed=_parsed(), raw_eml=b"raw", idempotency_key=None,
        )
    finally:
        await client.close()
    assert result.retry is True
    assert result.status_code == 0


@pytest.mark.asyncio
@respx.mock
async def test_send_idempotent_replay():
    respx.post("https://shpd.example.com/api/v1/_mail/incoming").mock(
        return_value=httpx.Response(201, json={
            "success": True,
            "data": {"ndx": 1, "message_id": "MSG-1", "idempotent_replay": True},
        })
    )
    client = ShpdClient()
    try:
        result = await client.send(
            ds_config=DS, mailbox=None,
            received_at_fallback=datetime.now(UTC),
            parsed=_parsed(), raw_eml=b"raw", idempotency_key="k",
        )
    finally:
        await client.close()
    assert result.success
    assert result.idempotent_replay is True


@pytest.mark.asyncio
@respx.mock
async def test_send_no_idempotency_header_when_none():
    route = respx.post("https://shpd.example.com/api/v1/_mail/incoming").mock(
        return_value=httpx.Response(201, json={
            "success": True, "data": {"ndx": 1, "message_id": "m", "idempotent_replay": False}
        })
    )
    client = ShpdClient()
    try:
        await client.send(
            ds_config=DS, mailbox=None,
            received_at_fallback=datetime.now(UTC),
            parsed=_parsed(), raw_eml=b"raw", idempotency_key=None,
        )
    finally:
        await client.close()
    req = route.calls[0].request
    assert "X-Idempotency-Key" not in req.headers
