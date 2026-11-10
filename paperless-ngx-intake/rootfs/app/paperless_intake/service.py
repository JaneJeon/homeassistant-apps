"""The running service: claim finished scans, work through items one at a time, tidy up."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from .config import Settings
from .notify import Notifier
from .pipeline import Pipeline
from .store import COMMITTED, COMMITTED_WARNING, HELD, Store, iso, now

log = logging.getLogger(__name__)

IGNORED_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload")
# A file that never becomes a readable PDF is claimed anyway after this long,
# so it can be held with a reason instead of sitting unnoticed.
UNREADABLE_AFTER_SECONDS = 300


@dataclass
class _Seen:
    size: int
    mtime: float
    stable_since: float


@dataclass
class SubprocessRunner:
    """Advance an item in a fresh `python -m paperless_intake.worker` process. Returns its exit code."""

    timeout_seconds: float = 4 * 3600
    command: tuple[str, ...] = (sys.executable, "-m", "paperless_intake.worker")
    env: dict | None = None

    def __call__(self, item) -> int:
        try:
            return subprocess.run([*self.command, item.sha], timeout=self.timeout_seconds, env=self.env,
                                  check=False).returncode
        except subprocess.TimeoutExpired:
            return TIMED_OUT


TIMED_OUT = -1000


@dataclass
class Service:
    settings: Settings
    store: Store
    pipeline: Pipeline
    notifier: Notifier
    clock: callable = now
    runner: callable = None        # None: work in this process (the tests); SubprocessRunner in the app
    lock: threading.RLock = field(default_factory=threading.RLock)
    _seen: dict = field(default_factory=dict)
    _last_tidy: float = 0.0

    # Capture ----------------------------------------------------------------------

    def claim_captures(self) -> list[str]:
        """Claim scans that have stopped changing. Returns the claimed item ids."""
        claimed = []
        capture = self.settings.capture_dir
        if not capture.is_dir():
            return claimed
        present = set()
        for path in sorted(capture.iterdir()):
            name = path.name
            if not path.is_file() or name.startswith((".", "~")) or name.lower().endswith(IGNORED_SUFFIXES):
                continue
            present.add(name)
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            seen = self._seen.get(name)
            if seen is None or seen.size != info.st_size or seen.mtime != info.st_mtime:
                self._seen[name] = _Seen(info.st_size, info.st_mtime, self.clock())
                continue
            stable_for = self.clock() - seen.stable_since
            if stable_for < self.settings.stable_seconds:
                continue
            # Read from the file rather than into memory: scans run to tens of MiB, and
            # the service stays small while each scan's work happens in a worker process.
            sha = _file_sha256(path)
            pages = _pdf_pages(path)
            if pages is None and stable_for < UNREADABLE_AFTER_SECONDS:
                continue  # maybe still being written in a way that keeps size and time still
            item_id = self._claim(path, sha, info.st_size, pages)
            if item_id:
                claimed.append(item_id)
            self._seen.pop(name, None)
        for name in list(self._seen):
            if name not in present:
                self._seen.pop(name, None)
        return claimed

    def _claim(self, path: Path, sha: str, size: int, pages: int | None) -> str | None:
        if _file_sha256(path) != sha:
            return None  # changed under us; look again next round
        if self.store.exists(sha):
            target = self.settings.duplicates_dir / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(self.clock()))}-{path.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(path, target)
            self.store.load(sha).event("duplicate_capture", moved_to=str(target))
            log.info("duplicate of %s moved to %s", sha[:12], target)
            return None
        item = self.store.create(sha, path, original_name=path.name, page_count=pages, byte_count=size,
                                 at=self.clock())
        item.state["started_sent"] = self.notifier.started(sha)
        item.save()
        log.info("claimed %s as %s (%s pages)", path.name, sha[:12], pages)
        return sha

    # Work -------------------------------------------------------------------------

    def next_item(self):
        for item in self.store.all():
            if self.pipeline.due(item):
                return item
        return None

    def step(self) -> bool:
        """Claim, then advance one due item. Returns True if an item was worked on."""
        with self.lock:
            self.claim_captures()
            item = self.next_item()
            if item is None:
                return False
            log.info("working on %s (%s)", item.sha[:12], item.status)
            if self.runner is None:
                self.pipeline.run(item)
            else:
                code = self.runner(item)
                item = self.store.load(item.sha)
                if code != 0:
                    self.pipeline.worker_stopped(item, code, timed_out=code == TIMED_OUT)
            log.info("%s is now %s%s", item.sha[:12], item.status,
                     f": {item.state.get('reason')}" if item.state.get("reason") else "")
            return True

    # Tidy -------------------------------------------------------------------------

    def tidy(self) -> None:
        """Drop local PDF copies some days after a verified commit; drop old discards and duplicates."""
        keep = self.settings.keep_local_copies_days * 86400
        with self.lock:
            for item in self.store.all():
                if item.status not in {COMMITTED, COMMITTED_WARNING} or item.state.get("local_copies_removed_at"):
                    continue
                if self.clock() - float(item.state.get("committed_at_unix", self.clock())) < keep:
                    continue
                for name in ("source", "searchable.pdf", "text.txt", "work"):
                    target = item.root / name
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                item.state["local_copies_removed_at"] = iso(self.clock())
                item.save()
                item.event("local_copies_removed")
            for folder in (self.settings.discarded_dir, self.settings.duplicates_dir):
                if not folder.is_dir():
                    continue
                for child in folder.iterdir():
                    if self.clock() - child.stat().st_mtime > 30 * 86400:
                        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        self._last_tidy = self.clock()

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            worked = False
            try:
                worked = self.step()
                if self.clock() - self._last_tidy > 3600:
                    self.tidy()
            except Exception:
                log.exception("service loop error")
            if not worked:
                stop.wait(self.settings.poll_seconds)

    def summary(self) -> dict:
        counts = {}
        for item in self.store.all():
            counts[item.status] = counts.get(item.status, 0) + 1
        return {"counts": counts, "held": counts.get(HELD, 0)}


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _pdf_pages(path: Path) -> int | None:
    """Page count if the file is a complete, readable PDF; otherwise None."""
    try:
        with path.open("rb") as stream:
            head = stream.read(5)
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 2048))
            tail = stream.read()
            if head != b"%PDF-" or b"%%EOF" not in tail:
                return None
            stream.seek(0)
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                return None
            return len(reader.pages)
    except Exception:
        return None
