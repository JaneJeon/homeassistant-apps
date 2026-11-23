"""What the queue looks at before it starts a job: memory, disk, and an upload whose outcome isn't known.

Memory. The app's container sees the Green's own /proc/meminfo: the Supervisor sets no
memory limit on app containers, so the figure is the host's. MemAvailable is what the
kernel could hand out without swapping. A worker peaks at about 350 MiB on a big scan,
and Paperless grows while it takes in a new file version.

An upload whose outcome isn't known. Before the app sends Paperless a file it writes an
intent mark, and it removes the mark once it knows how the upload ended. A mark that is
still there means the app timed out, or was killed, with the upload in flight. Paperless
may still be taking that file in, minutes later. While the mark is there no job starts,
because a job that uploads could send the same file again, or pile more work onto a
Paperless that is already struggling. The mark is settled by looking at Paperless.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from .paperless import COMPLETE, NOT_FOUND, Paperless, PaperlessError
from .store import write_atomic

MEMINFO = Path("/proc/meminfo")
CGROUP_LIMITS = (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
NO_LIMIT_V1 = 1 << 62               # cgroup v1 writes "no limit" as a number this big
HOST_AT_LEAST_MIB = 3.5 * 1024      # the Green has 4 GB. Less than this means the container isn't seeing the host
SETTLE_AFTER_SECONDS = 300          # an upload with no task id is taken not to have landed only after this long
# A worker that dies leaves its task "started". A task still "pending" is in a queue, perhaps a long one,
# so it is taken as lost only much later: its queue message must have been lost.
PENDING_LOST_AFTER_SECONDS = 6 * 3600


def _lost(task: dict, now: float, stale_after: float, since: float) -> tuple[bool, str]:
    """Whether an unfinished task will never finish, and why, in words.

    Paperless's time limit starts when a task starts running, not when it is queued. So a
    started task is aged from date_started, against the short limit (stale_after). A pending
    one, or a started one that doesn't say when it started, is aged from date_created, against
    the six-hour limit: a long queue is not a dead worker. since: when the app sent the file,
    for a task that names no time at all.
    """
    if task["status"] == "started" and task.get("started"):
        age = now - task["started"]
        return age >= stale_after, f"still running {age / 60:.0f} minutes after it started, past Paperless's own time limit"
    age = now - (task.get("created") or since)
    return age >= PENDING_LOST_AFTER_SECONDS, (f"still {task['status']} {age / 60:.0f} minutes after Paperless was "
                                               f"given it, so its queue message must have been lost")


def meminfo(path: Path = MEMINFO) -> dict[str, int]:
    """The fields of /proc/meminfo, in kB."""
    fields = {}
    try:
        for line in path.read_text().splitlines():
            name, _, rest = line.partition(":")
            value = rest.split()
            if value and value[0].isdigit():
                fields[name.strip()] = int(value[0])
    except OSError:
        pass
    return fields


def available_mib(path: Path = MEMINFO) -> int | None:
    kb = meminfo(path).get("MemAvailable")
    return None if kb is None else kb // 1024


def free_disk_mib(path: Path) -> int:
    return shutil.disk_usage(path).free // (1024 * 1024)


# Pressure stall information. /proc/pressure/io and /proc/pressure/memory are the kernel's
# system-wide figures; each cgroup has io.pressure and memory.pressure files of its own
# (Documentation/accounting/psi.rst). The "some" line's avg60 is the share of the last minute,
# in %, in which at least one task waited on that resource. With swap full, memory pressure is
# paid as disk I/O, which MemAvailable doesn't show. Since Linux 5.6 the files exist only when
# PSI is on, so a missing file means: no figure, and the gate goes by MemAvailable alone.
PRESSURE = Path("/proc/pressure")


def pressure(resource: str, root: Path = PRESSURE) -> float | None:
    """avg60 of the "some" line for "io" or "memory", or None without PSI."""
    try:
        text = (root / resource).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("some "):
            for field in line.split()[1:]:
                name, _, value = field.partition("=")
                if name == "avg60":
                    try:
                        return float(value)
                    except ValueError:
                        return None
    return None


def describe_memory(path: Path = MEMINFO, limits: tuple[Path, ...] = CGROUP_LIMITS,
                    pressure_root: Path = PRESSURE) -> tuple[str, list[str]]:
    """One line for the start-up log, and warnings if the figures can't be the Green's or PSI is missing."""
    fields = meminfo(path)
    total = fields.get("MemTotal", 0) // 1024
    limit = None
    for candidate in limits:
        try:
            limit = candidate.read_text().strip()
            break
        except OSError:
            continue
    present = [name for name in ("io", "memory") if (pressure_root / name).exists()]
    line = (f"memory: MemTotal {total} MiB, MemAvailable {fields.get('MemAvailable', 0) // 1024} MiB, "
            f"cgroup memory limit {limit or 'not found'}; pressure files: {', '.join(present) or 'none'}")
    warnings = []
    if len(present) < 2:
        warnings.append(f"{pressure_root} has no io or memory file (a kernel without PSI): the gate goes by "
                        f"MemAvailable alone")
    if total < HOST_AT_LEAST_MIB:
        warnings.append(f"MemTotal is {total} MiB. The Green has 4 GB, so this container may not be seeing "
                        f"the host's memory, and the memory check may be wrong")
    if limit and limit != "max" and limit.isdigit() and int(limit) < NO_LIMIT_V1:
        warnings.append(f"the container has a memory limit of {int(limit) // (1024 * 1024)} MiB, so MemAvailable "
                        f"is the host's, not the container's")
    return line, warnings


class Intent:
    """The mark for an upload whose outcome isn't known yet. At most one: the app runs one job at a time."""

    def __init__(self, path: Path, clock: Callable[[], float]):
        self.path = path
        self.clock = clock

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def open(self, *, what: str, sha256: str, name: str, doc_id: int | None = None) -> None:
        """Written before the upload goes out. what: "original" (a scan's new document) or "version"."""
        write_atomic(self.path, json.dumps({"what": what, "sha256": sha256, "name": name, "doc_id": doc_id,
                                            "task": None, "at": self.clock()}).encode())

    def task(self, task_id: str) -> None:
        """Paperless answered the upload with its task. Kept, so the mark can be settled by asking about the task."""
        intent = self.read()
        if intent is not None:
            intent["task"] = task_id
            write_atomic(self.path, json.dumps(intent).encode())

    def close(self) -> None:
        self.path.unlink(missing_ok=True)

    def settle(self, paperless: Paperless, intake_tag: str, *, stale_after: float) -> tuple[str, str] | None:
        """Look at Paperless. Once it is known how the upload ended: (outcome, in words), and the mark is removed.

        The outcome is "landed" (the file is in Paperless), "ended" (its task ended; the job, which
        has the same task in its record, deals with a failed one by its own rules), or "lost" (it did
        not land, and the job sends it again). None while it isn't known yet.

        A task Paperless left started for longer than stale_after (its own time limit and a margin)
        since it started running is taken as lost: Paperless's worker died with it, in a power cut
        say, and Paperless leaves such a task as it was for good. A task still pending, or started
        without saying when, is taken as lost only six hours after Paperless was given it.

        Raises PaperlessError when Paperless doesn't answer: that is an outside wait, and the caller
        backs off. It never settles a mark by guessing.
        """
        intent = self.read()
        if intent is None:
            return "none", "no upload was in doubt"
        paperless.server_version()                  # the cheap question: is Paperless there at all
        sha = intent["sha256"]
        if intent.get("doc_id"):
            try:
                landed = any(version.checksum == sha for version in paperless.versions(int(intent["doc_id"])))
            except PaperlessError as error:
                if error.kind != NOT_FOUND:
                    raise
                self.close()
                return "lost", "it did not land: the document is no longer in Paperless"
        else:
            landed = paperless.find_by_checksum(sha, with_tag=paperless.tag_id(intake_tag)) is not None
        if landed:
            self.close()
            return "landed", "it landed"
        now = self.clock()
        at = float(intent.get("at") or 0)
        task = paperless.task(intent["task"]) if intent.get("task") else None
        if task is not None:
            if task["status"] in COMPLETE:
                self.close()
                return "ended", f"Paperless's task for it ended ({task['status']}); the job looks for the file itself"
            lost, why = _lost(task, now, stale_after, at)
            if not lost:
                return None                         # Paperless is still working on it, or will be
            self.close()
            return "lost", f"it did not land: Paperless's task for it was {why}"
        if now - at < SETTLE_AFTER_SECONDS:
            return None
        # Only files Paperless was given after the mark went down can be this one. A task stuck from
        # long before (its worker died) must not keep the mark for ever, and nor can one stuck since.
        live = [task for task in paperless.consume_tasks(created_after=at - 60)
                if not _lost(task, now, stale_after, now)[0]]
        if live:
            return None
        self.close()
        return "lost", "it did not land: Paperless has nothing left to take in"

    def forget(self) -> dict | None:
        """Jane's choice on the Scans page: Paperless will never finish this upload. Taken as lost at once."""
        intent = self.read()
        self.close()
        return intent
