"""One folder per scan, named by the SHA-256 of the original bytes.

items/<sha256>/
  source/original.pdf     the scan, byte-for-byte as the scanner wrote it
  google/run-<n>/         raw Google responses for OCR attempt n (deleted after commit)
  google-summary.json.gz  responses with page images removed, kept after commit
  work/<uuid>/            renderer scratch for one attempt
  searchable.pdf          the checked searchable copy (when one is committed)
  checks.json             what each check observed
  state.json              current state, rewritten atomically
  events.jsonl            append-only history
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# Where an item can be in its life.
CAPTURED = "captured"          # claimed from the capture folder, nothing done yet
READY = "ready"                # checks passed (or Jane chose original only); safe to commit
COMMITTING = "committing"      # upload under way; resume by looking Paperless up
COMMITTED = "committed"
COMMITTED_WARNING = "committed_warning"
HELD = "held"                  # a check found a gap; Jane decides
WAITING = "waiting"            # something outside was unavailable; retried automatically
FAILED = "failed"              # commit read-back found the original wrong in Paperless
DISCARDED = "discarded"

ACTIVE = {CAPTURED, READY, COMMITTING, WAITING}
FINAL = {COMMITTED, COMMITTED_WARNING, FAILED, DISCARDED}


def now() -> float:
    return time.time()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Item:
    root: Path
    state: dict

    @property
    def sha(self) -> str:
        return self.state["sha256"]

    @property
    def status(self) -> str:
        return self.state["status"]

    @property
    def source_path(self) -> Path:
        return self.root / "source" / "original.pdf"

    @property
    def searchable_path(self) -> Path:
        return self.root / "searchable.pdf"

    def google_dir(self) -> Path:
        return self.root / "google" / f"run-{self.state.get('google_run', 1)}"

    def new_work_dir(self) -> Path:
        path = self.root / "work" / uuid.uuid4().hex
        path.mkdir(parents=True, mode=0o700)
        return path

    def save(self) -> None:
        self.state["updated_at"] = iso(now())
        write_atomic(self.root / "state.json", (json.dumps(self.state, indent=2, sort_keys=True) + "\n").encode())

    def event(self, kind: str, **details) -> None:
        record = {"at": iso(now()), "event": kind, **details}
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def set(self, status: str, *, reason: str | None = None, reason_kind: str | None = None, **fields) -> None:
        """Record a state change: the event first, then the state file."""
        self.event("state", status=status, reason=reason, reason_kind=reason_kind, **fields)
        self.state.update(fields)
        self.state["status"] = status
        self.state["reason"] = reason
        self.state["reason_kind"] = reason_kind
        self.save()

    def events(self) -> list[dict]:
        path = self.root / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Store:
    def __init__(self, items_dir: Path):
        self.items_dir = items_dir
        items_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha: str) -> Path:
        return self.items_dir / sha

    def exists(self, sha: str) -> bool:
        return (self.path_for(sha) / "state.json").exists()

    def create(self, sha: str, captured_file: Path, *, original_name: str, page_count: int | None,
               byte_count: int, at: float | None = None) -> Item:
        """Move the captured file into a new item. The move is a rename on one filesystem."""
        root = self.path_for(sha)
        (root / "source").mkdir(parents=True, exist_ok=False)
        os.rename(captured_file, root / "source" / "original.pdf")
        item = Item(root, {
            "sha256": sha,
            "original_name": original_name,
            "page_count": page_count,
            "byte_count": byte_count,
            "captured_at": iso(at if at is not None else now()),
            "captured_at_unix": at if at is not None else now(),
            "google_run": 1,
            "status": CAPTURED,
            "reason": None,
            "reason_kind": None,
        })
        item.save()
        item.event("captured", original_name=original_name, page_count=page_count, byte_count=byte_count)
        return item

    def load(self, sha: str) -> Item:
        root = self.path_for(sha)
        return Item(root, json.loads((root / "state.json").read_text(encoding="utf-8")))

    def all(self) -> list[Item]:
        items = []
        for child in self.items_dir.iterdir():
            if (child / "state.json").exists():
                try:
                    items.append(Item(child, json.loads((child / "state.json").read_text(encoding="utf-8"))))
                except (OSError, json.JSONDecodeError):
                    continue
        items.sort(key=lambda item: item.state.get("captured_at_unix", 0))
        return items
