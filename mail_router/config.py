from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class WorkerConfig:
    poll_interval: float = 5.0
    batch_size: int = 10
    max_attempts: int = 4
    http_timeout: float = 30.0
    backoff: list[int] = field(default_factory=lambda: [0, 60, 300, 1800])


@dataclass
class AlertsConfig:
    enabled: bool = True
    smtp_host: str = "localhost"
    smtp_port: int = 25
    from_address: str = "mail-router@localhost"
    to_addresses: list[str] = field(default_factory=list)
    throttle: int = 1800
    queue_size_threshold: int = 100


@dataclass
class LookupSyncConfig:
    """Pull of lookup.json from the hosting API (lookup-sync oneshot)."""

    url: str
    api_key: str
    timeout: float = 10.0


@dataclass
class Config:
    policy_socket: Path
    lmtp_socket: Path
    queue_db: Path
    lookup_file: Path
    lookup_reload: bool = True
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    lookup_sync: LookupSyncConfig | None = None
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: str | Path) -> Config:
        raw = yaml.safe_load(Path(path).read_text())
        worker_raw = raw.get("worker", {}) or {}
        alerts_raw = raw.get("alerts", {}) or {}
        # Optional section: absent -> None, lookup-sync refuses to run.
        # Other processes ignore it entirely.
        lookup_sync_raw = raw.get("lookup_sync") or None
        return cls(
            policy_socket=Path(raw["policy_socket"]),
            lmtp_socket=Path(raw["lmtp_socket"]),
            queue_db=Path(raw["queue_db"]),
            lookup_file=Path(raw["lookup_file"]),
            lookup_reload=bool(raw.get("lookup_reload", True)),
            worker=WorkerConfig(
                poll_interval=float(worker_raw.get("poll_interval", 5)),
                batch_size=int(worker_raw.get("batch_size", 10)),
                max_attempts=int(worker_raw.get("max_attempts", 4)),
                http_timeout=float(worker_raw.get("http_timeout", 30)),
                backoff=list(worker_raw.get("backoff", [0, 60, 300, 1800])),
            ),
            alerts=AlertsConfig(
                enabled=bool(alerts_raw.get("enabled", True)),
                smtp_host=str(alerts_raw.get("smtp_host", "localhost")),
                smtp_port=int(alerts_raw.get("smtp_port", 25)),
                from_address=str(alerts_raw.get("from_address", "mail-router@localhost")),
                to_addresses=list(alerts_raw.get("to_addresses", [])),
                throttle=int(alerts_raw.get("throttle", 1800)),
                queue_size_threshold=int(alerts_raw.get("queue_size_threshold", 100)),
            ),
            lookup_sync=LookupSyncConfig(
                url=str(lookup_sync_raw["url"]),
                api_key=str(lookup_sync_raw["api_key"]),
                timeout=float(lookup_sync_raw.get("timeout", 10)),
            ) if lookup_sync_raw is not None else None,
            log_level=str(raw.get("log_level", "INFO")).upper(),
        )
