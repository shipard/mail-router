"""lookup_sync — fetch/validate/atomic-write cycle, no real network."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx

from mail_router.config import Config, LookupSyncConfig
from mail_router.lookup_sync import sync_once

VALID_PAYLOAD = {
    "hosts": ["shipard.email"],
    "data_sources": {
        "a3f2-b8c1-d4e7-f9a0": {
            "api_url": "https://one.shpd.dev",
            "api_token": "shpd_ak_" + "1" * 32,
        },
        "firma-jedna": {
            "api_url": "https://one.shpd.dev",
            "api_token": "shpd_ak_" + "1" * 32,
        },
    },
}


def _config(tmp_path: Path) -> Config:
    return Config(
        policy_socket=tmp_path / "policy.sock",
        lmtp_socket=tmp_path / "lmtp.sock",
        queue_db=tmp_path / "q.db",
        lookup_file=tmp_path / "lookup.json",
        lookup_sync=LookupSyncConfig(
            url="https://portal.example.com/api/v1/_hosting/mail/lookup",
            api_key="shpd_hk_" + "a" * 43,
            timeout=5.0,
        ),
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_valid_200_writes_atomically_and_stores_etag(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=VALID_PAYLOAD, headers={"ETag": '"abc123"'})

    exit_code = sync_once(config, client=_client(handler))

    assert exit_code == 0
    assert calls[0].headers["Authorization"] == "Bearer shpd_hk_" + "a" * 43
    assert "If-None-Match" not in calls[0].headers

    written = json.loads(config.lookup_file.read_text())
    assert written == VALID_PAYLOAD
    assert stat.S_IMODE(config.lookup_file.stat().st_mode) == 0o600
    assert (tmp_path / "lookup.json.etag").read_text().strip() == '"abc123"'


def test_304_sends_etag_and_does_not_write(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.lookup_file.write_text('{"hosts": [], "data_sources": {}}')
    (tmp_path / "lookup.json.etag").write_text('"abc123"\n')
    before = config.lookup_file.stat().st_mtime_ns
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(304, headers={"ETag": '"abc123"'})

    exit_code = sync_once(config, client=_client(handler))

    assert exit_code == 0
    assert calls[0].headers["If-None-Match"] == '"abc123"'
    assert config.lookup_file.stat().st_mtime_ns == before
    assert config.lookup_file.read_text() == '{"hosts": [], "data_sources": {}}'


def test_invalid_json_keeps_existing_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.lookup_file.write_text('{"hosts": ["shipard.email"], "data_sources": {}}')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{ torn json")

    exit_code = sync_once(config, client=_client(handler))

    assert exit_code == 0
    assert json.loads(config.lookup_file.read_text())["hosts"] == ["shipard.email"]
    assert not (tmp_path / "lookup.json.etag").exists()


def test_missing_required_keys_keeps_existing_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    original = '{"hosts": ["shipard.email"], "data_sources": {}}'
    config.lookup_file.write_text(original)

    bad_payloads = [
        {"hosts": "not-a-list", "data_sources": {}},
        {"hosts": [], "data_sources": {"x": {"api_url": "https://x"}}},          # missing api_token
        {"hosts": [], "data_sources": {"x": {"api_url": "", "api_token": "t"}}},  # empty api_url
        {"hosts": [], "data_sources": ["not", "a", "dict"]},
        ["not", "an", "object"],
    ]
    for payload in bad_payloads:
        def handler(request: httpx.Request, payload=payload) -> httpx.Response:
            return httpx.Response(200, json=payload)

        exit_code = sync_once(config, client=_client(handler))

        assert exit_code == 0
        assert config.lookup_file.read_text() == original


def test_network_error_keeps_stale_lookup_and_exits_zero(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.lookup_file.write_text('{"hosts": [], "data_sources": {}}')

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    exit_code = sync_once(config, client=_client(handler))

    assert exit_code == 0
    assert config.lookup_file.read_text() == '{"hosts": [], "data_sources": {}}'


def test_http_error_status_keeps_stale_lookup(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.lookup_file.write_text("original")

    for status in (401, 404, 500):
        def handler(request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, text="nope")

        assert sync_once(config, client=_client(handler)) == 0
        assert config.lookup_file.read_text() == "original"


def test_empty_data_sources_is_written_with_warning(tmp_path: Path, caplog) -> None:
    config = _config(tmp_path)
    payload = {"hosts": ["shipard.email"], "data_sources": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, headers={"ETag": '"empty"'})

    with caplog.at_level("WARNING", logger="mail_router.lookup_sync"):
        exit_code = sync_once(config, client=_client(handler))

    assert exit_code == 0
    assert json.loads(config.lookup_file.read_text()) == payload
    assert any(r.message == "lookup_sync_empty_data_sources" for r in caplog.records)


def test_missing_config_section_returns_2(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.lookup_sync = None

    assert sync_once(config) == 2


def test_written_file_is_loadable_by_lookup_table(tmp_path: Path) -> None:
    from mail_router.lookup import LookupTable

    config = _config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=VALID_PAYLOAD)

    assert sync_once(config, client=_client(handler)) == 0

    table = LookupTable(config.lookup_file, auto_reload=False)
    assert table.allowed_domains == {"shipard.email"}
    resolved = table.resolve("a3f2-b8c1-d4e7-f9a0")
    assert resolved is not None
    assert resolved.api_url == "https://one.shpd.dev"
