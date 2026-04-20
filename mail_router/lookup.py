from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .models import DsConfig

log = logging.getLogger(__name__)


class LookupTable:
    """Thread-safe view over /etc/shipard-mail-router/lookup.json.

    Reloads the file on demand using mtime — cheap enough to call on every
    request. The alternative (inotify) would add a dep for marginal benefit.
    """

    def __init__(self, path: str | Path, *, auto_reload: bool = True) -> None:
        self._path = Path(path)
        self._auto_reload = auto_reload
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._hosts: frozenset[str] = frozenset()
        self._data_sources: dict[str, DsConfig] = {}
        self._load()

    @property
    def allowed_domains(self) -> set[str]:
        if self._auto_reload:
            self._maybe_reload()
        return set(self._hosts)

    def resolve(self, ds_id: str) -> DsConfig | None:
        if self._auto_reload:
            self._maybe_reload()
        return self._data_sources.get(ds_id.lower())

    def _maybe_reload(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            with self._lock:
                if mtime != self._mtime:
                    self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            log.warning("lookup_file_missing", extra={"path": str(self._path)})
            self._hosts = frozenset()
            self._data_sources = {}
            self._mtime = None
            return

        hosts = frozenset(h.lower() for h in raw.get("hosts", []))
        data: dict[str, DsConfig] = {}
        for key, entry in (raw.get("data_sources") or {}).items():
            data[key.lower()] = DsConfig(
                ds_id=key.lower(),
                api_url=entry["api_url"].rstrip("/"),
                api_token=entry["api_token"],
            )
        self._hosts = hosts
        self._data_sources = data
        try:
            self._mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._mtime = None
        log.info(
            "lookup_loaded",
            extra={"hosts": sorted(hosts), "ds_count": len(data)},
        )
