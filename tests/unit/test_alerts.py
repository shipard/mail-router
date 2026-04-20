from unittest.mock import MagicMock, patch

from mail_router.alerts import Alerter
from mail_router.config import AlertsConfig


def _config(**kwargs) -> AlertsConfig:
    defaults = {
        "enabled": True,
        "smtp_host": "localhost",
        "smtp_port": 25,
        "from_address": "from@x",
        "to_addresses": ["to@x"],
        "throttle": 1800,
        "queue_size_threshold": 100,
    }
    defaults.update(kwargs)
    return AlertsConfig(**defaults)


@patch("mail_router.alerts.smtplib.SMTP")
def test_alert_sent(mock_smtp: MagicMock):
    alerter = Alerter(_config())
    assert alerter.notify("dead_letter", "subj", "body") is True
    mock_smtp.assert_called_once_with("localhost", 25, timeout=10)


@patch("mail_router.alerts.smtplib.SMTP")
def test_alert_throttled(mock_smtp: MagicMock):
    alerter = Alerter(_config(throttle=3600))
    assert alerter.notify("dead_letter", "a", "b") is True
    assert alerter.notify("dead_letter", "a", "b") is False  # throttled
    # Different event type not throttled.
    assert alerter.notify("backlog", "c", "d") is True


@patch("mail_router.alerts.smtplib.SMTP")
def test_alert_disabled(mock_smtp: MagicMock):
    alerter = Alerter(_config(enabled=False))
    assert alerter.notify("x", "s", "b") is False
    mock_smtp.assert_not_called()


@patch("mail_router.alerts.smtplib.SMTP")
def test_alert_no_recipient(mock_smtp: MagicMock):
    alerter = Alerter(_config(to_addresses=[]))
    assert alerter.notify("x", "s", "b") is False
    mock_smtp.assert_not_called()
