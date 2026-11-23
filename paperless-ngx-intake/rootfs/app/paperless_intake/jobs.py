"""The job queue. The records on disk are the queue. This module decides what runs next, and when.

A scan moves through stages, and each stage can be checked from outside: a file, a record,
a document or a version in Paperless. A job is the step from one stage to the next.

  A scan
    a  a PDF in the capture folder
    b  claimed: items/<sha>/ holds the original and its record
    c  checked: the searchable copy and checks.json exist, or the scan is held for Jane
    d  original in Paperless: a document with the scan's checksum and the Mail Intake tag
    e  searchable: that document has a version with the copy's checksum, the note, and reads back right
    f  handed over: documents/<id>/record.json exists
  A stored document
    a  tagged Scanner in Paperless, with no record
    b  fetched: version 1 on disk, its checksum the one Paperless has
    c  copy built
    d  version in Paperless
    e  done: it reads back right, and the note is there

  Jobs: claim a->b (in the service), prepare b->c and commit c->e then f (pipeline.py),
  re-read a->e (documents.py). Prepare, commit and re-read run in a worker process.

What the queue does, and where:

- One job at a time, scans before stored documents (next).
- A lease (lease): before a job is handed to a worker it is marked with this service's run id.
  A job found leased by another run was in hand when the app was killed. It is resumed, and
  that is not a try. Only a worker death the live service saw (a non-zero exit, a signal)
  counts against the job (returned). A job whose work keeps taking the whole app down is
  stopped after RESTART_BOUND restarts that made no progress; any step forward resets the count.
- Tries, waits and dead letters are each job's own (pipeline.py, documents.py): a scan waits
  and is held after a day; a document gets 3 tries for faults in the app and 24 hourly tries
  for outside reasons, then is left for the next release. Held, failed and gave up are the
  dead letters that need Jane.
- Rate limit: Google pages per day (usage.py), three quarters of them for stored documents.
- Backpressure (the gate, between choosing a job and leasing it, so a job the gate stops is
  never leased and never blamed): an upload whose outcome isn't known holds every job;
  memory, disk and pressure stalls hold every job; and for stored documents only, a scan the
  gate is holding goes first, a document waiting on Paperless or Google is the only one that
  may start (its own next try is the health check), and two minutes pass between documents.
- Effects are made safe to repeat in the jobs themselves: each looks before it acts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from . import __version__
from .documents import LATER, NEXT_RELEASE, WORKING, Documents, Record
from .gate import available_mib, free_disk_mib, pressure
from .paperless import Paperless, PaperlessError
from .pipeline import GATE_KINDS, Pipeline
from .store import CAPTURED, COMMITTED, COMMITTED_WARNING, COMMITTING, READY, WAITING, Item, Store, iso

log = logging.getLogger(__name__)

TIMED_OUT = -1000           # the exit code SubprocessRunner reports for a worker it stopped after 4 hours
RESTART_BOUND = 10          # restarts of the whole app, with this job in hand and no step forward, before it is set aside
RECHECK_SECONDS = 120       # how long a job the gate holds waits before the gate looks again


@dataclass
class Job:
    kind: str                       # "scan" or "document"
    record: Item | Record

    @property
    def label(self) -> str:
        return self.record.sha[:12] if self.kind == "scan" else self.record.label


@dataclass
class Hold:
    """The gate's answer when something must not start now."""
    reason: str                     # in plain words, for the Scans page and Home Assistant
    kind: str
    waits_on: str | None            # paperless, google, memory, disk, io; None for planned waits
    until: float
    outside: bool                   # held for an outside reason (Paused in Home Assistant), not by plan


class Queue:
    def __init__(self, settings, store: Store, pipeline: Pipeline, documents: Documents | None, *,
                 clock: Callable[[], float], paperless: Paperless | None = None, run_id: str | None = None):
        self.settings = settings
        self.store = store
        self.pipeline = pipeline
        self.documents = documents
        self.clock = clock
        self.paperless = paperless or pipeline.paperless
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.running: Job | None = None
        self.held: dict[str, Hold | None] = {"scan": None, "document": None}
        self._logged: dict[str, tuple | None] = {"scan": None, "document": None}
        self._documents_not_before = 0.0
        self._last_document_end: float | None = None
        self._settle_at = 0.0
        self._settle_backoff = RECHECK_SECONDS
        self._settle_hold: Hold | None = None

    # Start --------------------------------------------------------------------------------

    def recover(self) -> list[str]:
        """When the app starts: put every record back in a state the queue understands. Safe to run again.

        Nothing is undone here. Leased jobs stay leased, and are resumed when chosen.
        """
        done = list(self.store.recover())
        requeued = self.pipeline.requeue_obsolete_holds()
        if requeued:
            done.append(f"checking {len(requeued)} scan(s) again that an earlier version held")
        settled = self.pipeline.settle_old_states()
        if settled:
            done.append(f"put {len(settled)} scan record(s) back as they were before an earlier release "
                        f"began re-doing them")
        if self.documents is not None:
            done += self.documents.recover()
        again = self.pipeline.reprepare_unfinished()
        if again:
            done.append(f"preparing {len(again)} scan(s) again that hadn't reached Paperless")
        done += self.pipeline.recover()
        # A lease on a job that is no longer in hand: the worker had finished its try, and the app was
        # killed before it took the lease back. Left there, it would read as a restart later.
        for item in self.store.all():
            if item.state.get("lease") and item.status not in {CAPTURED, READY, COMMITTING}:
                item.state.pop("lease")
                item.save()
        for record in self.documents.all() if self.documents is not None else []:
            if record.state.get("lease") and record.status != WORKING:
                record.state.pop("lease")
                record.save()
        return done

    # Choosing -----------------------------------------------------------------------------

    def next(self) -> Job | None:
        """The job to start now, already leased. None when nothing should start; held says why, for each kind."""
        in_doubt = self._uploads()
        item = next((item for item in self.store.all() if self.pipeline.due(item)), None)
        if item is not None:
            hold = in_doubt or self._resources()
            self._decided("scan", hold)
            if hold:
                self.pipeline.held_back(item, hold.reason, hold.kind, hold.waits_on, hold.until)
                return None
            return self._lease(Job("scan", item))
        if not any(item.status == WAITING and item.state.get("reason_kind") in GATE_KINDS for item in self.store.all()):
            self._decided("scan", None)       # otherwise the hold stands until the held scan is due again
        if self.documents is None:
            return None
        if self.clock() < self._documents_not_before:
            return None
        hold = in_doubt or self._scan_first()
        record = None
        if hold is None:
            records = self.documents.all()
            record = self._next_document(records)
            if record is None:
                # Not the gate: what the records say. Each document's own log line says why it waits.
                self._decided("document", self._documents_idle(records), quiet=True)
                return None
            hold = self._resources() or self._pace()
        self._decided("document", hold)
        if hold:
            self._documents_not_before = hold.until
            return None
        return self._lease(Job("document", record))

    def _next_document(self, records: list[Record]) -> Record | None:
        """Stored documents in order: one cut short, then one waiting on a service, then one due, then new ones."""
        leased = next((record for record in records if record.status == WORKING), None)
        if leased is not None:
            return leased
        now = self.clock()
        due = lambda record: float(record.state.get("not_before_unix") or 0) <= now  # noqa: E731
        outside = [record for record in records if record.status == LATER and record.state.get("waits_on")]
        if outside:
            # Paperless or Google failed this one. Nothing new starts: its own next try is the check that
            # the service is back, so one outage can't run through every stored document.
            return next((record for record in outside if due(record)), None)
        chosen = next((record for record in records if record.status == LATER and due(record)), None)
        if chosen is None:
            # Each release tries once what an earlier release left for it.
            chosen = next((record for record in records if record.status == NEXT_RELEASE
                           and record.state.get("by") != __version__), None)
        # While one document waits for tomorrow's Google pages, no new one is started: it would only be
        # fetched to wait as well. The one that waits goes first tomorrow.
        waits_for_pages = any(record.status == LATER and record.state.get("waits_for_pages") for record in records)
        if chosen is None and not waits_for_pages:
            chosen = self.documents.discover(records)
        return chosen

    def _documents_idle(self, records: list[Record]) -> Hold | None:
        """Why no stored document starts when none is due: for the Scans page and Home Assistant, not a gate."""
        waiting = [record for record in records if record.status == LATER and record.state.get("waits_on")]
        if waiting:
            record = waiting[0]
            service = "Paperless" if record.state["waits_on"] == "paperless" else "Google"
            return Hold(f"stored scans wait until {service} answers again ({record.state.get('reason')})",
                        f"waits_on_{record.state['waits_on']}", record.state["waits_on"],
                        float(record.state.get("not_before_unix") or 0), True)
        pages = [record for record in records if record.status == LATER and record.state.get("waits_for_pages")]
        if pages:
            return Hold("stored scans have used today's share of Google pages, and go on tomorrow", "pages",
                        None, float(pages[0].state.get("not_before_unix") or 0), False)
        return None

    # The gate -----------------------------------------------------------------------------

    def _resources(self) -> Hold | None:
        settings = self.settings
        until = self.clock() + RECHECK_SECONDS
        free = free_disk_mib(settings.intake_dir)
        if free < settings.min_free_mib:
            return Hold(f"only {free} MiB free on the Green's disk (the app waits below {settings.min_free_mib})",
                        "disk_low", "disk", until, True)
        available = available_mib(settings.meminfo_path)
        if available is not None and available < settings.min_available_mib:
            return Hold(f"only {available} MiB of memory available on the Green (the app waits below "
                        f"{settings.min_available_mib})", "memory_low", "memory", until, True)
        io = pressure("io", settings.pressure_dir)
        if io is not None and io >= settings.max_io_pressure:
            return Hold(f"the Green's disk is saturated: work waited on disk I/O {io:.0f}% of the last minute "
                        f"(the app waits at {settings.max_io_pressure:.0f}%)", "io_pressure", "io", until, True)
        memory = pressure("memory", settings.pressure_dir)
        if memory is not None and memory >= settings.max_memory_pressure:
            return Hold(f"the Green is short of memory: work waited on memory {memory:.0f}% of the last minute "
                        f"(the app waits at {settings.max_memory_pressure:.0f}%)", "memory_pressure", "memory",
                        until, True)
        return None

    def _uploads(self) -> Hold | None:
        """While an upload's outcome isn't known, no job starts. Paperless is asked, never more than the backoff allows."""
        intent = self.pipeline.intent.read()
        if intent is None:
            self._settle_hold = None
            return None
        now = self.clock()
        if now < self._settle_at and self._settle_hold is not None:
            return self._settle_hold
        try:
            how = self.pipeline.intent.settle(self.paperless, self.settings.intake_tag)
        except PaperlessError as error:
            # Paperless doesn't answer the question: an outside wait, backing off to the scans' retry time.
            self._settle_backoff = min(self._settle_backoff * 2, self.settings.retry_minutes * 60)
            self._settle_at = now + self._settle_backoff
            self._settle_hold = Hold(f"an upload of {intent.get('name')} may not have reached Paperless, and "
                                     f"Paperless isn't answering ({error})", "upload_in_doubt", "paperless",
                                     self._settle_at, True)
            return self._settle_hold
        self._settle_backoff = RECHECK_SECONDS
        if how is not None:
            log.info("the upload of %s that was in doubt is settled: %s", intent.get("name"), how)
            self._note_settled(intent, how)
            self._settle_hold = None
            return None
        self._settle_at = now + RECHECK_SECONDS
        self._settle_hold = Hold(f"Paperless is still taking in an upload of {intent.get('name')} that didn't "
                                 f"finish (the app timed out or was stopped), so nothing else is sent until it has",
                                 "upload_in_doubt", "paperless", self._settle_at, True)
        return self._settle_hold

    def _note_settled(self, intent: dict, how: str) -> None:
        """Written into the history of the job the upload belonged to. Its own rules decide what happens next."""
        if intent.get("what") == "original" and self.store.exists(intent["sha256"]):
            self.store.load(intent["sha256"]).event("upload_settled", how=how)
            return
        for item in self.store.all():
            if intent.get("doc_id") and item.state.get("doc_id") == intent["doc_id"]:
                item.event("upload_settled", how=how)
        if self.documents is not None and intent.get("doc_id") and self.documents.exists(intent["doc_id"]):
            record = self.documents.load(intent["doc_id"])
            record.state["upload_settled"] = {"at": iso(self.clock()), "how": how}
            record.save()

    def _scan_first(self) -> Hold | None:
        held = [item for item in self.store.all() if item.status == WAITING
                and item.state.get("reason_kind") in GATE_KINDS]
        if not held:
            return None
        return Hold("a scan the gate is holding back goes first", "scan_first", held[0].state.get("waits_on"),
                    float(held[0].state.get("next_attempt_at_unix") or self.clock() + RECHECK_SECONDS), False)

    def _pace(self) -> Hold | None:
        pace = self.settings.pace_seconds
        if self._last_document_end is None or self.clock() >= self._last_document_end + pace:
            return None
        return Hold(f"a pause of {pace / 60:g} minutes between stored scans, so Paperless can finish the last one",
                    "pace", None, self._last_document_end + pace, False)

    def _decided(self, lane: str, hold: Hold | None, *, quiet: bool = False) -> None:
        """Remember the gate's decision, and log it only when it changes. The reading that decided is in the reason."""
        self.held[lane] = hold
        key = (hold.kind, hold.waits_on) if hold else None
        if key == self._logged[lane]:
            return
        self._logged[lane] = key
        what = "scans" if lane == "scan" else "stored scans"
        if hold is None:
            log.info("nothing holds %s back now", what)
        elif hold.outside and not quiet:
            log.warning("holding %s back: %s", what, hold.reason)
        else:
            log.info("%s wait: %s", what, hold.reason)

    # Leases -------------------------------------------------------------------------------

    @staticmethod
    def progress(job: Job) -> str:
        """How far the job has got, from what its record says is done. Any change is a step forward."""
        state = job.record.state
        if job.kind == "scan":
            marks = [job.record.status if job.record.status != WAITING else state.get("retry_stage"),
                     state.get("searchable_sha256") or state.get("no_google_copy_reason"), state.get("upload_task"),
                     state.get("doc_id"), state.get("version_task"), state.get("note_added")]
        else:
            marks = [state.get("original_sha256"), state.get("kind"), state.get("pending_sha256"),
                     state.get("version_task"), len(state.get("added") or [])]
        return "|".join("" if mark is None else str(mark) for mark in marks)

    def _lease(self, job: Job) -> Job | None:
        record = job.record
        lease = record.state.get("lease")
        progress = self.progress(job)
        restarts = 0
        leased_before = record.status == WORKING if job.kind == "document" else lease is not None
        if leased_before and (lease is None or lease.get("run") != self.run_id):
            # In hand when the app was killed (or the Green lost power): resumed, not a try.
            restarts = int(lease.get("restarts") or 0) + 1 if lease and lease.get("progress") == progress else 0
            log.info("%s was in hand when the app stopped; resuming it%s", job.label,
                     f" ({restarts} restart(s) without a step forward)" if restarts else "")
            if restarts >= RESTART_BOUND:
                why = (f"the app stopped {restarts} times while working on it, each time at the same step, "
                       f"so it is set aside")
                if job.kind == "document":
                    self.documents._finish(record, NEXT_RELEASE, why, lease=None)
                else:
                    record.state.pop("lease", None)
                    self.pipeline._hold(record, why, "gave_up")
                return None
        lease = {"run": self.run_id, "at": self.clock(), "progress": progress, "restarts": restarts}
        if job.kind == "document":
            record.set(WORKING, by=__version__, lease=lease, waits_for_pages=False, waits_on=None)
        else:
            record.state["lease"] = lease
            record.save()
        self.running = job
        return job

    def returned(self, job: Job, code: int, *, stopping: bool = False) -> None:
        """The worker is done with this job. A death the live service saw is the job's; one while stopping is not."""
        self.running = None
        if job.kind == "document":
            self._last_document_end = self.clock()
            record = self.documents.load(job.record.doc_id)
            if stopping and code != 0:
                return                       # left leased: the next start resumes it, as after any kill
            if code != 0 or record.status == WORKING:
                self.documents.worker_stopped(record, code if code != 0 else -1)
                record = self.documents.load(record.doc_id)
            if "lease" in record.state:
                record.state.pop("lease")
                record.save()
            self.documents.settle(record)    # a worker killed just after the outcome leaves this owed
            return
        if not self.store.exists(job.record.sha):
            return                           # discarded while it ran, and moved aside
        item = self.store.load(job.record.sha)
        if stopping and code != 0:
            return
        if code != 0:
            self.pipeline.worker_stopped(item, code, timed_out=code == TIMED_OUT)
            item = self.store.load(item.sha)
        if "lease" in item.state:
            item.state.pop("lease")
            item.save()
        # A worker killed just after a change of state leaves the rest of it owed: "finished", queued alerts,
        # the hand-over. Each looks before it acts, so this is safe after any exit.
        self.pipeline.settle(item)
        doc_id = item.state.get("doc_id")
        if (self.documents is not None and doc_id and item.status in {COMMITTED, COMMITTED_WARNING}
                and not self.documents.exists(doc_id)):
            self.documents.record_scan(item)
