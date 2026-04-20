from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .alerts import Alerter
from .client import ShpdClient
from .config import Config
from .logging_setup import configure as configure_logging
from .lookup import LookupTable
from .policy import PolicyServer
from .queue import Queue
from .receiver import LMTPReceiver
from .worker import Worker

DEFAULT_CONFIG_PATH = Path(os.environ.get("SHIPARD_MAIL_ROUTER_CONFIG",
                                          "/etc/shipard-mail-router/config.yaml"))

log = logging.getLogger("mail_router.cli")


def _parse_args(argv: list[str], desc: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)


async def _run_policy(config: Config) -> int:
    lookup = LookupTable(config.lookup_file, auto_reload=config.lookup_reload)
    server = PolicyServer(socket_path=config.policy_socket, lookup=lookup)
    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    await stop.wait()
    serve_task.cancel()
    await server.stop()
    return 0


async def _run_receiver(config: Config) -> int:
    lookup = LookupTable(config.lookup_file, auto_reload=config.lookup_reload)
    queue = Queue(config.queue_db)
    receiver = LMTPReceiver(
        socket_path=config.lmtp_socket,
        queue=queue,
        lookup=lookup,
    )
    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    await receiver.start()
    serve_task = asyncio.create_task(receiver.serve_forever())
    await stop.wait()
    serve_task.cancel()
    await receiver.stop()
    queue.close()
    return 0


async def _run_worker(config: Config) -> int:
    lookup = LookupTable(config.lookup_file, auto_reload=config.lookup_reload)
    queue = Queue(config.queue_db)
    client = ShpdClient(timeout=config.worker.http_timeout)
    alerter = Alerter(config.alerts)
    worker = Worker(
        queue=queue,
        lookup=lookup,
        client=client,
        alerter=alerter,
        config=config.worker,
        queue_size_threshold=config.alerts.queue_size_threshold,
    )
    stop = asyncio.Event()

    def _on_signal() -> None:
        worker.stop()
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    try:
        await worker.run_forever()
    finally:
        await client.close()
        queue.close()
    return 0


def run_policy(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:], "Shipard mail-router policy server")
    config = Config.load(args.config)
    configure_logging(config.log_level)
    return asyncio.run(_run_policy(config))


def run_receiver(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:], "Shipard mail-router LMTP receiver")
    config = Config.load(args.config)
    configure_logging(config.log_level)
    return asyncio.run(_run_receiver(config))


def run_worker(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:], "Shipard mail-router delivery worker")
    config = Config.load(args.config)
    configure_logging(config.log_level)
    return asyncio.run(_run_worker(config))


def run_admin(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Shipard mail-router admin utilities")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="print queue counts")
    p_dlq = sub.add_parser("list-dlq", help="list recent dead-letter items")
    p_dlq.add_argument("--limit", type=int, default=50)
    p_prune = sub.add_parser("prune-delivered", help="delete delivered items older than N days")
    p_prune.add_argument("--days", type=int, default=7)

    args = parser.parse_args(argv)
    config = Config.load(args.config)
    configure_logging(config.log_level)
    queue = Queue(config.queue_db)

    if args.cmd == "stats":
        print(json.dumps(queue.stats(), indent=2))
        return 0
    if args.cmd == "list-dlq":
        for item in queue.list_dead_letters(args.limit):
            print(json.dumps({
                "id": item.id,
                "received_at": item.received_at.isoformat(),
                "ds_id": item.ds_id,
                "recipient": item.recipient_email,
                "reason": item.dead_letter_reason,
                "attempts": item.attempt_count,
            }))
        return 0
    if args.cmd == "prune-delivered":
        n = queue.prune_delivered(args.days)
        print(f"pruned {n} delivered items older than {args.days} days")
        return 0
    return 2
