"""Start the service: python -m paperless_intake"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from . import __version__
from .config import ConfigError, load
from .gate import describe_memory
from .paperless import Paperless
from .service import Service, SubprocessRunner
from .web import Page, serve
from .wiring import build


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("paperless_intake")
    try:
        settings = load()
    except ConfigError as error:
        log.error("configuration problem: %s", error)
        return 1
    for folder in (settings.capture_dir, settings.intake_dir, settings.items_dir):
        folder.mkdir(parents=True, exist_ok=True)

    line, warnings = describe_memory(settings.meminfo_path, pressure_root=settings.pressure_dir)
    log.info("%s", line)
    for warning in warnings:
        log.warning("%s", warning)

    app = build(settings, announce=True)
    # Each scan's work runs in a child process, so its memory goes back when it's done.
    runner = SubprocessRunner()
    service = Service(settings, app.store, app.pipeline, app.notifier, runner=runner, documents=app.documents)
    # Before anything runs: every record back in a state the queue understands, and whatever a kill
    # left owed to Home Assistant sent. Leased jobs are resumed when the queue chooses them.
    for done in service.start():
        log.info("on start: %s", done)

    publisher = _publisher(service, settings)
    server = serve(Page(service, app.pipeline, settings), settings.web_port, settings.web_allowed_clients)
    log.info("paperless-ngx-scanner %s watching %s; tested Paperless version %s; queue run %s", __version__,
             settings.capture_dir, settings.tested_paperless_version, service.queue.run_id)

    stop = threading.Event()

    def stopping(*_):
        stop.set()
        runner.stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopping)
    service.loop(stop)
    server.shutdown()
    if publisher is not None:
        publisher.stop()
    log.info("stopped")
    return 0


def _publisher(service: Service, settings):
    """The app's numbers in Home Assistant. Never a reason not to start: without it the app works as before."""
    log = logging.getLogger("paperless_intake")
    try:
        from .mqtt import Publisher
        from .report import Reachability, snapshot
    except ImportError as error:
        log.warning("MQTT publishing is off: %s", error)
        return None
    # A client of its own, with a short timeout: the publisher's thread never shares the workers' connection.
    reachable = Reachability(Paperless(settings.paperless_url, settings.paperless_token, timeout=10).server_version)
    publisher = Publisher(lambda: snapshot(service, reachable=reachable()))
    service.poke = publisher.poke
    publisher.start()
    return publisher


if __name__ == "__main__":
    sys.exit(main())
