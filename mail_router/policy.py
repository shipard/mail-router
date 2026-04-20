from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
from pathlib import Path

from .address import parse_recipient
from .lookup import LookupTable

log = logging.getLogger(__name__)


class PolicyServer:
    """Postfix policy delegation server.

    Protocol: https://www.postfix.org/SMTPD_POLICY_README.html
    Each request is a block of key=value lines terminated by an empty line.
    Reply is `action=<verdict>` + empty line. Connections are persistent.
    """

    def __init__(
        self,
        *,
        socket_path: str | Path,
        lookup: LookupTable,
        socket_mode: int = 0o660,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._lookup = lookup
        self._socket_mode = socket_mode
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        os.chmod(self._socket_path, self._socket_mode)
        log.info("policy_listening", extra={"socket": str(self._socket_path)})

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

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername") or "unix"
        try:
            while not reader.at_eof():
                attrs = await _read_request(reader)
                if attrs is None:
                    break
                verdict = self._evaluate(attrs)
                writer.write(f"action={verdict}\n\n".encode())
                await writer.drain()
                log.debug(
                    "policy_verdict",
                    extra={
                        "recipient": attrs.get("recipient"),
                        "verdict": verdict,
                    },
                )
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("policy_handler_error", extra={"peer": str(peer)})
            writer.write(b"action=DEFER_IF_PERMIT Mail router policy error\n\n")
            with contextlib.suppress(Exception):
                await writer.drain()
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def _evaluate(self, attrs: dict[str, str]) -> str:
        recipient = attrs.get("recipient", "").strip()
        if not recipient:
            return "REJECT 550 5.1.1 Empty recipient"
        parsed = parse_recipient(recipient, self._lookup.allowed_domains)
        if parsed is None:
            return "REJECT 550 5.1.1 Unknown recipient"
        ds = self._lookup.resolve(parsed.ds_id)
        if ds is None:
            return "REJECT 550 5.1.1 Unknown recipient"
        return "OK"


async def _read_request(reader: asyncio.StreamReader) -> dict[str, str] | None:
    attrs: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            return attrs or None
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if text == "":
            return attrs
        if "=" not in text:
            continue
        key, _, value = text.partition("=")
        attrs[key.strip()] = value.strip()


def _mask_owning_group(path: Path, group: str | None) -> None:
    """Best-effort chown to the Postfix group so postfix can connect. Used by
    deployment scripts; kept here for symmetry with LMTP receiver."""
    if not group:
        return
    import grp  # local import; only needed on deploy

    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return
    st = path.stat()
    os.chown(path, st.st_uid, gid)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
