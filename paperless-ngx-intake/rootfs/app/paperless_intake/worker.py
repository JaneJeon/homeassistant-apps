"""Advance one scan, or one stored document, in a process of its own.

    python -m paperless_intake.worker <sha256>          a scan
    python -m paperless_intake.worker document <id>     a document in Paperless

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
    document = len(argv) == 2 and argv[0] == "document" and argv[1].isdigit()
    if not document and (len(argv) != 1 or not re.fullmatch(r"[0-9a-f]{64}", argv[0])):
        log.error("usage: python -m paperless_intake.worker <sha256> | document <id>")
        return 2
    try:
        app = build(load())
    except ConfigError as error:
        log.error("configuration problem: %s", error)
        return 3
    if document:
        app.documents.run(app.documents.load(int(argv[1])))
    else:
        app.pipeline.run(app.store.load(argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
