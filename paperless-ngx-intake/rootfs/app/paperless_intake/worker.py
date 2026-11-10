"""Advance one item in a process of its own: python -m paperless_intake.worker <sha256>

The service runs each scan's work in a fresh process. Building and checking the
searchable copy of a 15-page scan peaks around 350 MiB, and CPython keeps most of
that after the work is done. A child process hands all of it back when it exits,
so the app idles small next to Paperless. Item state lives on disk, so the service
just reloads it afterwards.
"""

from __future__ import annotations

import logging
import re
import sys

from .config import ConfigError, load
from .wiring import build


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("paperless_intake.worker")
    if len(argv) != 1 or not re.fullmatch(r"[0-9a-f]{64}", argv[0]):
        log.error("usage: python -m paperless_intake.worker <sha256>")
        return 2
    try:
        app = build(load())
    except ConfigError as error:
        log.error("configuration problem: %s", error)
        return 3
    item = app.store.load(argv[0])
    app.pipeline.run(item)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
