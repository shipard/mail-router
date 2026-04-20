import asyncio
import json
from pathlib import Path

import pytest

from mail_router.lookup import LookupTable
from mail_router.policy import PolicyServer


@pytest.fixture
def lookup(tmp_path: Path) -> LookupTable:
    f = tmp_path / "lookup.json"
    f.write_text(json.dumps({
        "hosts": ["shipard.email"],
        "data_sources": {
            "firma-xyz": {"api_url": "https://h", "api_token": "t"},
            "4l3j-z0bz-kz39-echj": {"api_url": "https://h", "api_token": "t"},
        },
    }))
    return LookupTable(f)


@pytest.mark.asyncio
async def test_policy_ok_known_recipient(tmp_path: Path, lookup: LookupTable):
    sock = tmp_path / "policy.sock"
    server = PolicyServer(socket_path=sock, lookup=lookup)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b"request=smtpd_access_policy\n")
        writer.write(b"recipient=firma-xyz@shipard.email\n")
        writer.write(b"\n")
        await writer.drain()
        line = await reader.readline()
        assert line == b"action=OK\n"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_policy_reject_unknown_recipient(tmp_path: Path, lookup: LookupTable):
    sock = tmp_path / "policy.sock"
    server = PolicyServer(socket_path=sock, lookup=lookup)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b"request=smtpd_access_policy\n")
        writer.write(b"recipient=nobody@shipard.email\n")
        writer.write(b"\n")
        await writer.drain()
        line = await reader.readline()
        assert line.startswith(b"action=REJECT")
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_policy_persistent_connection(tmp_path: Path, lookup: LookupTable):
    sock = tmp_path / "policy.sock"
    server = PolicyServer(socket_path=sock, lookup=lookup)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
        # two requests on one connection
        for rcpt, expected in [
            ("firma-xyz@shipard.email", b"action=OK\n"),
            ("nobody@shipard.email", b"action=REJECT"),
        ]:
            writer.write(f"recipient={rcpt}\n\n".encode())
            await writer.drain()
            line = await reader.readline()
            assert line.startswith(expected)
            # consume the terminating empty line
            await reader.readline()
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
