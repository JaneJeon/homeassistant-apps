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

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# Where an item can be in its life.
CAPTURED = "captured"          # claimed from the capture folder, nothing done yet
READY = "ready"                # checks passed (or Jane chose to keep it as it is); safe to commit
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


def fsync_dir(path: Path) -> None:
    """Make a rename or a new file in this folder survive a power cut."""
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Every record is written this way: a temporary file, fsync, rename over the old one, fsync of the folder.

    Killed at any point, the file is either the old one or the new one, never half of each.
    A temporary file left by a kill is removed when the app next starts (remove_leftovers).
    """
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def append_line(path: Path, line: str) -> None:
    """One line onto a history file. A kill can leave the last line cut off; readers skip it."""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue                    # the last line of a write a kill cut off
    return lines


def remove_leftovers(folder: Path) -> None:
    """Temporary files that a kill left behind in one record's folder."""
    for path in folder.glob(".*.tmp"):
        path.unlink(missing_ok=True)


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
        append_line(self.root / "events.jsonl", json.dumps({"at": iso(now()), "event": kind, **details},
                                                           sort_keys=True))

    def set(self, status: str, *, reason: str | None = None, reason_kind: str | None = None, **fields) -> None:
        """Record a state change: the event first, then the state file."""
        self.event("state", status=status, reason=reason, reason_kind=reason_kind, **fields)
        self.state.update(fields)
        self.state["status"] = status
        self.state["reason"] = reason
        self.state["reason_kind"] = reason_kind
        self.save()

    def events(self) -> list[dict]:
        return read_lines(self.root / "events.jsonl")


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
        """Move the captured file into a new item. The move is a rename on one filesystem.

        Three durable steps, each safe to be killed after: claim.json says what is being claimed,
        the rename moves the file, state.json makes it an item. recover() finishes or undoes a claim
        a kill cut short, so a scan is never lost between the capture folder and its record.
        """
        root = self.path_for(sha)
        root.mkdir(parents=True, exist_ok=False)
        at = at if at is not None else now()
        claim = {"sha256": sha, "original_name": original_name, "page_count": page_count, "byte_count": byte_count,
                 "captured_at_unix": at}
        write_atomic(root / "claim.json", json.dumps(claim).encode())
        (root / "source").mkdir()
        os.rename(captured_file, root / "source" / "original.pdf")
        fsync_dir(root / "source")
        fsync_dir(captured_file.parent)
        return self._item_from_claim(root, claim)

    @staticmethod
    def _item_from_claim(root: Path, claim: dict) -> Item:
        item = Item(root, {
            "sha256": claim["sha256"],
            "original_name": claim["original_name"],
            "page_count": claim.get("page_count"),
            "byte_count": claim.get("byte_count"),
            "captured_at": iso(claim["captured_at_unix"]),
            "captured_at_unix": claim["captured_at_unix"],
            "google_run": 1,
            "status": CAPTURED,
            "reason": None,
            "reason_kind": None,
        })
        item.save()
        item.event("captured", original_name=claim["original_name"], page_count=claim.get("page_count"),
                   byte_count=claim.get("byte_count"))
        return item

    def recover(self) -> list[str]:
        """At start: finish claims a kill cut short, and remove temporary files kills left. Returns what it did."""
        done = []
        for child in sorted(self.items_dir.iterdir()) if self.items_dir.is_dir() else []:
            if not child.is_dir():
                continue
            remove_leftovers(child)
            if (child / "state.json").exists():
                continue
            source = child / "source" / "original.pdf"
            try:
                claim = json.loads((child / "claim.json").read_text())
            except (OSError, json.JSONDecodeError):
                claim = None
            if not source.exists():
                # The file was never moved, so it is still in the capture folder and is claimed again.
                shutil.rmtree(child, ignore_errors=True)
                done.append(f"undid an unfinished claim of {child.name[:12]}")
                continue
            if claim is None:
                # Claimed by a release before 1.4.0, which wrote no claim.json. The file's name is lost.
                with source.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != child.name:
                        continue
                claim = {"sha256": child.name, "original_name": f"recovered-{child.name[:12]}.pdf",
                         "page_count": None, "byte_count": source.stat().st_size,
                         "captured_at_unix": source.stat().st_mtime}
            self._item_from_claim(child, claim)
            done.append(f"finished the claim of {child.name[:12]}")
        return done

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
