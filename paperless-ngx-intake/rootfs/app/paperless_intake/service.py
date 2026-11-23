"""The running service: claim finished scans, run the queue's jobs one at a time in a worker, tidy up."""

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
from .jobs import TIMED_OUT, Queue
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
    stopping: bool = False
    _child: subprocess.Popen | None = field(default=None, repr=False)

    def __call__(self, item) -> int:
        return self._run(item.sha)

    def document(self, record) -> int:
        return self._run("document", str(record.doc_id))

    def tidy(self, sha: str) -> int:
        """Summarise a committed scan's Google answers and drop them (pipeline._tidy_after_commit)."""
        return self._run("tidy", sha)

    def _run(self, *arguments: str) -> int:
        child = subprocess.Popen([*self.command, *arguments], env=self.env)
        self._child = child
        try:
            return child.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            return TIMED_OUT
        finally:
            self._child = None

    def stop(self) -> None:
        """The app is asked to stop (the Supervisor's SIGTERM). The worker is stopped at once, so the app exits
        well inside the Supervisor's 10 seconds.

        Nothing depends on this. The worker dies as it would in any kill, and the job it had is
        resumed when the app starts again: every step looks before it acts. It only spares the
        Supervisor a kill -9 and the log line that goes with it.
        """
        self.stopping = True
        child = self._child
        if child is not None and child.poll() is None:
            child.terminate()


@dataclass
class Service:
    settings: Settings
    store: Store
    pipeline: Pipeline
    notifier: Notifier
    clock: callable = now
    runner: callable = None        # None: work in this process (the tests); SubprocessRunner in the app
    documents: object = None       # the documents loop (documents.py); None switches it off
    lock: threading.RLock = field(default_factory=threading.RLock)
    queue: Queue | None = None
    poke: callable = None          # told after anything that may change what Home Assistant shows
    _seen: dict = field(default_factory=dict)
    _claim_failed: dict = field(default_factory=dict)
    _last_tidy: float = 0.0

    def __post_init__(self):
        if self.queue is None:
            self.queue = Queue(self.settings, self.store, self.pipeline, self.documents, clock=self.clock)

    def start(self) -> list[str]:
        """Put every record back in a state the queue understands. Run once, before the loop."""
        with self.lock:
            done = self.queue.recover()
            for sha in self.pipeline.left_to_tidy:
                if self.runner is None:
                    self.pipeline._tidy_after_commit(self.store.load(sha))
                else:
                    self.runner.tidy(sha)
                done.append(f"summarised the Google answers of {sha[:12]}, left by a kill after it went in")
            return done

    def _told(self) -> None:
        if self.poke is not None:
            try:
                self.poke()
            except Exception:      # publishing is best effort and never stops the work
                log.exception("couldn't hand the change on for Home Assistant")

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
                claimed += self._claim_if_ready(path)
            except Exception as error:      # a full disk, say: this file waits, and nothing else stops
                failure = f"{type(error).__name__}: {error}"
                if self._claim_failed.get(name) != failure:
                    log.exception("couldn't claim %s; trying again at the next look", name)
                self._claim_failed[name] = failure
        for name in list(self._seen):
            if name not in present:
                self._seen.pop(name, None)
        for name in list(self._claim_failed):
            if name not in present:
                self._claim_failed.pop(name, None)
        return claimed

    def _claim_if_ready(self, path: Path) -> list[str]:
        """Claim one file from the capture folder once it has stopped changing. Returns the claimed item id, if any."""
        name = path.name
        try:
            info = path.stat()
        except FileNotFoundError:
            return []
        seen = self._seen.get(name)
        if seen is None or seen.size != info.st_size or seen.mtime != info.st_mtime:
            self._seen[name] = _Seen(info.st_size, info.st_mtime, self.clock())
            return []
        stable_for = self.clock() - seen.stable_since
        if stable_for < self.settings.stable_seconds:
            return []
        # Read from the file rather than into memory: scans run to tens of MiB, and
        # the service stays small while each scan's work happens in a worker process.
        sha = _file_sha256(path)
        pages = _pdf_pages(path)
        if pages is None and stable_for < UNREADABLE_AFTER_SECONDS:
            return []  # maybe still being written in a way that keeps size and time still
        item_id = self._claim(path, sha, info.st_size, pages)
        self._seen.pop(name, None)
        self._claim_failed.pop(name, None)
        return [item_id] if item_id else []

    def _claim(self, path: Path, sha: str, size: int, pages: int | None) -> str | None:
        if _file_sha256(path) != sha:
            return None  # changed under us; look again next round
        self.store.settle_partial(self.store.path_for(sha))
        if self.store.exists(sha):
            target = self.settings.duplicates_dir / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(self.clock()))}-{path.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(path, target)
            self.store.load(sha).event("duplicate_capture", moved_to=str(target))
            log.info("duplicate of %s moved to %s", sha[:12], target)
            return None
        item = self.store.create(sha, path, original_name=path.name, page_count=pages, byte_count=size,
                                 at=self.clock())
        self.pipeline.announce(item)
        log.info("claimed %s as %s (%s pages)", path.name, sha[:12], pages)
        return sha

    # Work -------------------------------------------------------------------------

    def next_item(self):
        """The first scan that is due, in the order scans arrived. The queue decides whether it may start."""
        for item in self.store.all():
            if self.pipeline.due(item):
                return item
        return None

    def step(self) -> bool:
        """Claim, then run the one job the queue hands out, if any. True if a job ran."""
        with self.lock:
            if self.claim_captures():
                self._told()
            held_before = dict(self.queue.held)
            job = self.queue.next()
            if job is None:
                if self.queue.held != held_before:
                    self._told()
                return False
            log.info("working on %s (%s)", job.label, job.record.status)
            self._told()
            if self.runner is None:
                code = 0
                (self.pipeline.run if job.kind == "scan" else self.documents.run)(job.record)
            else:
                code = self.runner(job.record) if job.kind == "scan" else self.runner.document(job.record)
            self.queue.returned(job, code, stopping=bool(getattr(self.runner, "stopping", False)))
            self._told()
            if job.kind == "scan" and self.store.exists(job.record.sha):
                record = self.store.load(job.record.sha)
            elif job.kind == "document":
                record = self.documents.load(job.record.doc_id)
            else:
                return True
            log.info("%s is now %s%s", job.label, record.status,
                     f": {record.state.get('reason')}" if record.state.get("reason") else "")
            return True

    # Tidy -------------------------------------------------------------------------

    def tidy(self) -> None:
        """Drop local PDF copies some days after a verified commit; drop old discards and duplicates."""
        keep = self.settings.keep_local_copies_days * 86400
        with self.lock:
            for item in self.store.all():
                if item.state.get("outbox"):
                    self.pipeline.settle(item)      # alerts Home Assistant didn't take earlier
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
