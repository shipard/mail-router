from __future__ import annotations

import logging
import smtplib
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from time import monotonic

from .config import AlertsConfig

log = logging.getLogger(__name__)


@dataclass
class _ThrottleState:
    last_sent: float = 0.0


class Alerter:
    """Sends alert e-mails through the local Postfix. Throttles per event type
    so a flood of dead-letter events collapses into one notification."""

    def __init__(self, config: AlertsConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._throttle: dict[str, _ThrottleState] = {}

    def notify(self, event: str, subject: str, body: str) -> bool:
        if not self._config.enabled:
            return False
        if not self._config.to_addresses:
            log.warning("alert_skipped_no_recipient", extra={"event": event})
            return False

        with self._lock:
            state = self._throttle.setdefault(event, _ThrottleState())
            now = monotonic()
            if state.last_sent and (now - state.last_sent) < self._config.throttle:
                log.info("alert_throttled", extra={"event": event})
                return False
            state.last_sent = now

        msg = EmailMessage()
        msg["From"] = self._config.from_address
        msg["To"] = ", ".join(self._config.to_addresses)
        msg["Subject"] = f"[mail-router] {subject}"
        msg.set_content(body)
        try:
            with smtplib.SMTP(self._config.smtp_host, self._config.smtp_port, timeout=10) as s:
                s.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            log.error("alert_send_failed", extra={"event": event, "err": str(exc)})
            return False
        log.info("alert_sent", extra={"event": event})
        return True
