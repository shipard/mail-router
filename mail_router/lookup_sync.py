"""Oneshot pull of lookup.json from the hosting API (D4).

Fetches ``GET {lookup_sync.url}`` with the router's ``shpd_hk_`` key and,
when the content changed (ETag mismatch), atomically replaces the local
lookup file (temp file in the same directory + ``os.replace`` — the
existing mtime watch in :class:`mail_router.lookup.LookupTable` picks the
change up without restarts).

Safety rules (the lookup file feeds the live mail path):

* the response is validated *before* the file is touched — a torn or
  invalid payload never overwrites a working lookup file,
* network / HTTP errors log a warning and exit 0 — the router keeps
  running on the stale lookup, mail is not lost,
* a non-zero exit is reserved for local I/O failures (the systemd
  oneshot turns that into a visible unit failure).

The last seen ETag is persisted next to the lookup file
(``{lookup_file}.etag``) so an unchanged lookup costs a 304 round trip.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger("mail_router.lookup_sync")

#: Written file mode — the sync runs as the same user as the daemons.
_LOOKUP_FILE_MODE = 0o600


class ValidationFailure(Exception):
    """Fetched payload is not a usable lookup.json."""


def validate_payload(payload: object) -> dict:
    """Check the fetched document against LookupTable._load expectations.

    Raises :class:`ValidationFailure` with a human-readable reason when the
    payload would break (or silently corrupt) the live lookup. Returns the
    payload as a dict on success.
    """
    if not isinstance(payload, dict):
        raise ValidationFailure("top-level value is not an object")

    hosts = payload.get("hosts")
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h.strip() for h in hosts):
        raise ValidationFailure("'hosts' must be a list of non-empty strings")

    data_sources = payload.get("data_sources")
    if not isinstance(data_sources, dict):
        raise ValidationFailure("'data_sources' must be an object")
    for key, entry in data_sources.items():
        if not isinstance(entry, dict):
            raise ValidationFailure(f"data_sources[{key!r}] is not an object")
        for field in ("api_url", "api_token"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValidationFailure(f"data_sources[{key!r}].{field} must be a non-empty string")

    return payload


def _read_etag(etag_file: Path) -> str | None:
    try:
        value = etag_file.read_text().strip()
        return value or None
    except OSError:
        return None


def _write_atomic(target: Path, content: str, mode: int = _LOOKUP_FILE_MODE) -> None:
    """Temp file in the same directory + os.replace — never a torn file.

    Same-directory matters twice: os.replace must not cross filesystems,
    and the systemd unit only whitelists the lookup directory for writes.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def sync_once(config: Config, *, client: httpx.Client | None = None) -> int:
    """One fetch-validate-write cycle. Returns the process exit code."""
    sync = config.lookup_sync
    if sync is None:
        log.error("lookup_sync_not_configured")
        return 2

    lookup_file = config.lookup_file
    etag_file = Path(str(lookup_file) + ".etag")

    headers = {
        "Authorization": f"Bearer {sync.api_key}",
        "Accept": "application/json",
    }
    etag = _read_etag(etag_file)
    if etag is not None:
        headers["If-None-Match"] = etag

    own_client = client is None
    http = client or httpx.Client(timeout=sync.timeout)
    try:
        response = http.get(sync.url, headers=headers)
    except httpx.TimeoutException as exc:
        log.warning("lookup_sync_failed", extra={"reason": "timeout", "err": str(exc)})
        return 0
    except httpx.HTTPError as exc:
        log.warning("lookup_sync_failed", extra={"reason": "http_error", "err": str(exc)})
        return 0
    finally:
        if own_client:
            http.close()

    if response.status_code == 304:
        log.info("lookup_sync_unchanged")
        return 0

    if response.status_code != 200:
        log.warning("lookup_sync_failed", extra={
            "reason": "unexpected_status",
            "status": response.status_code,
            "body": response.text[:500],
        })
        return 0

    try:
        payload = validate_payload(json.loads(response.text))
    except json.JSONDecodeError as exc:
        log.warning("lookup_sync_failed", extra={"reason": "invalid_json", "err": str(exc)})
        return 0
    except ValidationFailure as exc:
        log.warning("lookup_sync_failed", extra={"reason": "invalid_payload", "err": str(exc)})
        return 0

    if not payload["data_sources"]:
        # Valid state (fresh hosting with no active DS yet), but worth a
        # warning — an unexpected wipe would silently reject all mail.
        log.warning("lookup_sync_empty_data_sources")

    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        _write_atomic(lookup_file, body)
        _write_atomic(etag_file, (response.headers.get("ETag") or "") + "\n")
    except OSError as exc:
        log.error("lookup_sync_write_failed", extra={"path": str(lookup_file), "err": str(exc)})
        return 1

    log.info("lookup_sync_updated", extra={
        "hosts": payload["hosts"],
        "ds_count": len(payload["data_sources"]),
        "etag": response.headers.get("ETag"),
    })
    return 0
