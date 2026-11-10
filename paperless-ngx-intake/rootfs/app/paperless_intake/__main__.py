"""Start the service: python -m paperless_intake"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from . import __version__
from .config import ConfigError, load
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
    if not settings.font_path.exists():
        log.error("font not found at %s", settings.font_path)
        return 1

    app = build(settings, announce=True)
    # Each scan's work runs in a child process, so its memory goes back when it's done.
    service = Service(settings, app.store, app.pipeline, app.notifier, runner=SubprocessRunner())
    server = serve(Page(service, app.pipeline, settings), settings.web_port, settings.web_allowed_clients)
    log.info("paperless-intake %s watching %s; tested Paperless version %s", __version__, settings.capture_dir,
             settings.tested_paperless_version)

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    service.loop(stop)
    server.shutdown()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
