"""Copy jobs: give a document that is already in Paperless a Google copy.

The scanner's folder is one way into Paperless. The web page, the phone app and
email are others, and everything stored before this app existed came in by one
of them. A document that came in another way has no Google copy. A copy job
gives it one: the same OCR, the same decisions about the stack, the same copy,
added to the document as a new version with a note.

This is a small job queue, and it is written to be read as one.

  Job       One try at one thing: make the copy of one document's file and add
            it to that document.
  Key       The document's id in Paperless. One job folder per document, so a
            document can't be queued twice.
  Queue     intake/jobs/<document id>/job.json. A job is due while it is
            "queued" or was cut short while "working".
  Producer  The sweep. Every few minutes it asks Paperless what it holds and
            queues a job for the next document that has only its first version
            and didn't come through the scanner's folder. It asks; it doesn't
            wait to be told. So nothing is missed while the app is off.
  Consumer  The service's one loop: one job at a time, and only when no scan
            is waiting.
  Delivery  At least once. A job that was cut short is run again from the
            top, and every step looks before it acts: is the file already
            here, is the copy already made, is it already a version of the
            document, is the note already written.
  Outcomes  done   The copy is the document's newest version.
            left   This version of the app can't do it, and the job says why.
                   A newer version of the app tries again.
            later  Something outside was in the way (Google, Paperless, the
                   disk, the day's page cap). Tried again after a wait.
  Writes    A job changes its document in one way: it adds one version and one
            note. It adds the version only while the document has exactly one,
            so it can never add two.

What a job never does: hold, ask, alert, or touch the document's owner, tags
or date. The document is already stored, and the paper it came from may be
long gone. A page the printed numbers say is missing is written into the
note, where it can be read, and stops nothing.

A scan from the scanner's folder is not a job in this file. It is an item
(store.py), because it has a step these documents can't have: the check before
anything is stored, while the paper is still at hand.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pypdf import PdfReader

from . import __version__, checks
from .config import Settings
from .paperless import Paperless, PaperlessError, PaperlessTaskFailed
from .pipeline import COPY_RESAVED, ORDER_WORDS, ORIGINAL_ONLY, Hold, Pipeline, Wait, sha256
from .store import Store, iso, now, write_atomic

log = logging.getLogger(__name__)

QUEUED = "queued"
WORKING = "working"
DONE = "done"
LEFT = "left"
LATER = "later"

TRIES_WHEN_ODD = 3              # tries per app version after something nobody planned for (an error, a dead worker)
TRIES_WHEN_OUTSIDE = 24         # ...and after something outside was in the way: a day of hourly tries, as for a scan
LATER_MINUTES = 60              # how long a job waits between tries
OFFERED_WAIT_SECONDS = 120      # how long to look for a copy that may already have been handed to Paperless
SWEEP_MINUTES = 10              # how often Paperless is asked what it holds, when there was nothing to do
CAP_SHARE = 0.75                # the sweep stops at this share of the day's Google pages, so scans keep the rest
OWN_TEXT_WORDS_PER_PAGE = 20    # a PDF with this much text of its own was made on a computer, not scanned
NOTE_MARK = "gave it a searchable copy"


class Leave(Exception):
    """This version of the app can't give this document a copy. Not an error: the reason is the outcome."""


class Later(Exception):
    """Not now. Worth another try after a wait.

    odd: nobody planned for this (an error in the app, a refusal that makes no sense). Such
    a job gets few tries. Otherwise something outside was in the way (Google, Paperless, the
    disk, the day's page cap), and the job keeps trying for a day.
    """

    def __init__(self, reason: str, *, odd: bool = False):
        super().__init__(reason)
        self.odd = odd


@dataclass
class Job:
    root: Path
    state: dict

    @property
    def doc_id(self) -> int:
        return int(self.state["doc_id"])

    @property
    def status(self) -> str:
        return self.state.get("status", QUEUED)

    @property
    def label(self) -> str:
        return f"document {self.doc_id}"

    @property
    def source_path(self) -> Path:
        return self.root / "source" / "original.pdf"

    @property
    def copy_path(self) -> Path:
        return self.root / "searchable.pdf"

    @property
    def text_path(self) -> Path:
        return self.root / "text.txt"

    def google_dir(self) -> Path:
        """Where Google's answers for this job are kept, so a second try never pays Google twice."""
        return self.root / "google" / f"run-{int(self.state.get('google_run', 1)):03d}"

    def new_work_dir(self) -> Path:
        work = self.root / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        return work

    def save(self) -> None:
        write_atomic(self.root / "job.json", (json.dumps(self.state, indent=2, default=str) + "\n").encode())

    def set(self, status: str, **fields) -> None:
        self.state.update(fields)
        self.state["status"] = status
        self.save()

    def tidy(self) -> None:
        """Drop the files a finished job no longer needs. The document has them; the job keeps its record."""
        for name in ("source", "work", "google"):
            shutil.rmtree(self.root / name, ignore_errors=True)
        for path in (self.copy_path, self.text_path):
            path.unlink(missing_ok=True)


class Jobs:
    def __init__(self, settings: Settings, store: Store, paperless: Paperless, pipeline: Pipeline, *,
                 clock: Callable[[], float] = now, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.store = store
        self.paperless = paperless
        self.pipeline = pipeline
        self.clock = clock
        self.sleep = sleep
        self.dir = settings.intake_dir / "jobs"
        self._look_again_at = 0.0

    # The queue ------------------------------------------------------------------------

    def load(self, doc_id: int) -> Job:
        root = self.dir / str(int(doc_id))
        return Job(root, json.loads((root / "job.json").read_text(encoding="utf-8")))

    def all(self) -> list[Job]:
        jobs = []
        if not self.dir.is_dir():
            return jobs
        for child in self.dir.iterdir():
            try:
                jobs.append(Job(child, json.loads((child / "job.json").read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        jobs.sort(key=lambda job: job.doc_id)
        return jobs

    def holds_file(self, sha: str) -> bool:
        """Whether a job already knows this file, by its fingerprint. Such a file is in Paperless already."""
        return any(job.state.get("sha256") == sha for job in self.all())

    def next_due(self) -> Job | None:
        """The job to work on now: one that is queued, or one that was cut short while being worked on."""
        for job in self.all():
            if job.status in {QUEUED, WORKING}:
                return job
        return None

    # The producer ---------------------------------------------------------------------

    def sweep(self) -> Job | None:
        """Queue a job for one document that has no Google copy, if there is one. Returns the job."""
        if self.clock() < self._look_again_at:
            return None
        known = {job.doc_id: job for job in self.all()}
        if any(job.status in {QUEUED, WORKING} for job in known.values()):
            return None                                   # one at a time
        for job in known.values():                        # a wait that is over
            if (job.status == LATER and job.state.get("by") == __version__
                    and float(job.state.get("not_before_unix") or 0) <= self.clock()):
                job.set(QUEUED)
                return job
        try:
            intake_tag = self.paperless.tag_id(self.settings.intake_tag)
            rows = self.paperless.documents()
        except PaperlessError as error:
            log.info("couldn't ask Paperless what it holds (%s); asking again later", error)
            self._look_again_at = self.clock() + SWEEP_MINUTES * 60
            return None
        for row in sorted(rows, key=lambda row: int(row["id"])):
            doc_id = int(row["id"])
            if intake_tag in (row.get("tags") or []):
                continue                                  # it came through the scanner's folder
            versions = row.get("versions")
            if versions is None or len(versions) != 1:
                continue                                  # it has a second version already, whoever added it
            checksum = str(versions[0].get("checksum") or "")
            if checksum and self.store.exists(checksum):
                continue                                  # the scanner's route knows this file
            job = known.get(doc_id)
            if job is not None:
                if job.status == DONE:
                    continue
                if job.state.get("by") == __version__:
                    continue                              # this version has had its try, or is waiting to try again
            pages = int(row.get("page_count") or 0)
            if self.pipeline.pages_today() + pages > self.settings.daily_page_cap * CAP_SHARE:
                log.info("the sweep has used its share of today's Google pages; carrying on tomorrow")
                self._look_again_at = self.clock() + 3600
                return None
            return self._queue(doc_id, row, job)
        self._look_again_at = self.clock() + SWEEP_MINUTES * 60
        return None

    def _queue(self, doc_id: int, row: dict, earlier: Job | None) -> Job:
        root = self.dir / str(doc_id)
        root.mkdir(parents=True, exist_ok=True)
        tries = (earlier.state.get("tries") if earlier else None) or {}
        state = {
            "doc_id": doc_id,
            "original_name": row.get("original_file_name") or f"document-{doc_id}.pdf",
            "page_count": row.get("page_count"),
            "queued_at": iso(self.clock()),
            "by": __version__,
            "google_run": int((earlier.state.get("google_run") if earlier else 0) or 0) + 1,
            "tries": tries if tries.get("version") == __version__ else {"version": __version__, "count": 0},
            "reason": None,
        }
        job = Job(root, state)
        job.set(QUEUED)
        log.info("document %s has no Google copy; queued", doc_id)
        return job

    # The consumer ---------------------------------------------------------------------

    def run(self, job: Job) -> None:
        """Work one job to an outcome. Whatever happens, the job ends in a state the queue understands."""
        try:
            self._work(job)
        except Leave as leave:
            log.info("%s is left as it was: %s", job.label, leave)
            job.set(LEFT, reason=str(leave), by=__version__, finished_at=iso(self.clock()))
            job.tidy()
        except Later as later:
            self._later(job, str(later), odd=later.odd)
        except Exception as error:      # a defect in the app must not spin the queue, or lose the job
            log.exception("unexpected error on %s", job.label)
            self._later(job, f"an unexpected error in the app ({type(error).__name__})", odd=True)

    def worker_stopped(self, job: Job, code: int) -> None:
        """The child process working this job died. Count it as a try, so a job that kills its worker can't loop."""
        if job.status in {QUEUED, WORKING}:
            self._later(job, f"the work on it stopped with exit code {code}", odd=True)

    def _later(self, job: Job, why: str, *, odd: bool) -> None:
        tries = dict(job.state.get("tries") or {})
        same = tries.get("version") == __version__
        count = (int(tries.get("count", 0)) if same else 0) + 1
        odd_count = (int(tries.get("odd", 0)) if same else 0) + (1 if odd else 0)
        tries = {"version": __version__, "count": count, "odd": odd_count}
        if odd_count >= TRIES_WHEN_ODD or count >= TRIES_WHEN_OUTSIDE:
            log.info("%s is left as it was after %d tries: %s", job.label, count, why)
            job.set(LEFT, reason=why, tries=tries, by=__version__, finished_at=iso(self.clock()))
            job.tidy()
            return
        log.info("%s waits and is tried again: %s", job.label, why)
        job.set(LATER, reason=why, tries=tries, by=__version__,
                not_before_unix=self.clock() + LATER_MINUTES * 60,
                # A new Google run for the next try: this run's retry budget may be spent.
                google_run=int(job.state.get("google_run", 1)) + 1)

    def _work(self, job: Job) -> None:
        doc_id = job.doc_id
        job.set(WORKING, by=__version__, started_at=job.state.get("started_at") or iso(self.clock()))

        # 1. The file, as Paperless stores it.
        try:
            versions = self.paperless.versions(doc_id)
        except PaperlessError as error:
            if "HTTP 404" in str(error):
                raise Leave("the document is no longer in Paperless")
            raise Later(str(error))
        root = next((version for version in versions if version.is_root), None)
        if root is None:
            raise Leave("Paperless doesn't say which version is the first")
        if not job.source_path.exists():
            data = self._paperless(lambda: self.paperless.download_original(doc_id, root.id))
            job.source_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(job.source_path, data)
        data = job.source_path.read_bytes()
        fingerprint = sha256(data)
        if job.state.get("sha256") != fingerprint:
            job.state["sha256"] = fingerprint
            job.save()
        if self.store.exists(fingerprint):
            raise Leave("the scanner's route already has this file")
        if not data.startswith(b"%PDF-"):
            raise Leave("it isn't a PDF, and this version of the app reads only PDFs")

        # 2. The copy. Made once and kept until the job ends, so a job that is run again offers
        #    Paperless the same bytes, and Paperless can be asked whether it already has them.
        copy_sha = job.state.get("copy_sha256")
        if not (copy_sha and job.copy_path.exists() and job.text_path.exists()
                and sha256(job.copy_path.read_bytes()) == copy_sha):
            if _has_own_text(job.source_path):
                raise Leave("it already has text of its own, so it was not scanned")
            try:
                made = self.pipeline.make_copy(job.source_path, lambda: self.pipeline.backend_factory(job),
                                               job.new_work_dir, job.label)
            except Hold as hold:
                raise Leave(hold.reason)
            except Wait as wait:
                raise Later(wait.reason, odd=wait.kind in {"unexpected", "worker"})
            write_atomic(job.root / "checks.json",
                         (json.dumps({"at": iso(self.clock()), **made.record}, indent=2, default=str) + "\n").encode())
            if made.why_not:
                raise Leave(made.why_not)
            if made.fields["mode"] == ORIGINAL_ONLY:
                raise Leave("Google found almost no text in it")
            write_atomic(job.copy_path, made.searchable)
            write_atomic(job.text_path, made.text.encode("utf-8"))
            copy_sha = made.fields["searchable_sha256"]
            job.state.update(copy_sha256=copy_sha, page_count=made.page_count, gap=made.gap, decided=made.fields,
                             version_task=None)
            job.save()

        # 3. The version. Added only while the document has exactly one, so never twice.
        mine = next((v for v in versions if v.checksum == copy_sha and not v.is_root), None)
        if mine is None:
            if len(versions) != 1:
                raise Leave("the document got another version while its copy was being made")
            task = job.state.get("version_task")
            if not task and job.state.get("version_offered"):
                # An earlier run was cut short somewhere around handing the copy over, and didn't
                # get to write down Paperless's receipt. Paperless may have it and still be
                # filing it. Look for it before handing it over a second time.
                mine = self._look_for(doc_id, copy_sha)
            if mine is None:
                if not task:
                    stem = Path(job.state.get("original_name") or f"document-{doc_id}").stem
                    job.state["version_offered"] = True
                    job.save()
                    task = self._paperless(lambda: self.paperless.update_version(
                        doc_id, f"{stem}.searchable.pdf", job.copy_path.read_bytes(),
                        label=self.pipeline.version_label()))
                    job.state["version_task"] = task
                    job.save()
                try:
                    self.paperless.wait_for_task(task)
                except PaperlessTaskFailed as error:
                    job.state.update(version_task=None)
                    job.save()
                    raise Later(f"Paperless couldn't take the copy ({error}: {error.result[:200]})", odd=True)
                except PaperlessError as error:
                    raise Later(str(error))
                versions = self._paperless(lambda: self.paperless.versions(doc_id))
                mine = next((v for v in versions if v.checksum == copy_sha and not v.is_root), None)
                if mine is None:
                    raise Later("Paperless took the copy but doesn't list it among the document's versions", odd=True)
            else:
                versions = self._paperless(lambda: self.paperless.versions(doc_id))

        # 4. Read it back.
        warnings = []
        if versions[0].id != mine.id:
            warnings.append("the searchable copy isn't the newest version, so search may not use Google's text")
        stored = self._paperless(lambda: self.paperless.download_original(doc_id, mine.id))
        if sha256(stored) != copy_sha:
            warnings.append("the searchable copy in Paperless doesn't match byte for byte")
        metadata = self._paperless(lambda: self.paperless.metadata(doc_id, mine.id))
        if metadata.get("has_archive_version"):
            warnings.append(f"{COPY_RESAVED} (see Paperless's OCR mode and archive file generation settings)")
        document = self._paperless(lambda: self.paperless.document(doc_id))
        match = checks.text_matches(job.text_path.read_text(encoding="utf-8"), document.get("content") or "")
        if not match["ok"]:
            warnings.append(f"Paperless's search text differs from Google's "
                            f"({match['missing_words']} of {match['google_words']} words missing)")

        # 5. The note, once.
        notes = self._paperless(lambda: self.paperless.notes(doc_id))
        if not any(NOTE_MARK in str(note.get("note") or "") for note in notes):
            self._paperless(lambda: self.paperless.add_note(doc_id, self.note_text(job, warnings)))
        job.set(DONE, version_id=mine.id, warnings=warnings, text_match=match, reason="; ".join(warnings) or None,
                finished_at=iso(self.clock()))
        job.tidy()
        log.info("%s has its Google copy%s", job.label, f" ({'; '.join(warnings)})" if warnings else "")

    def _look_for(self, doc_id: int, copy_sha: str):
        """Wait a while for this copy to show up among the document's versions. None if it doesn't."""
        deadline = self.clock() + OFFERED_WAIT_SECONDS
        while True:
            versions = self._paperless(lambda: self.paperless.versions(doc_id))
            mine = next((v for v in versions if v.checksum == copy_sha and not v.is_root), None)
            if mine is not None or self.clock() >= deadline:
                return mine
            self.sleep(5)

    @staticmethod
    def _paperless(call):
        """A call to Paperless that failing only means: try this job again later."""
        try:
            return call()
        except PaperlessError as error:
            raise Later(str(error))

    def note_text(self, job: Job, warnings: list[str]) -> str:
        decided = job.state.get("decided") or {}
        pages = job.state.get("page_count")
        processor = self.settings.processor_version.rsplit("/", 1)[-1]
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        lines = [f"This document came into Paperless another way than the scanner's folder. "
                 f"paperless-intake {__version__} {NOTE_MARK} on {day}.",
                 f"The newest version is the upright, searchable copy (Google OCR {processor}); "
                 f"version 1 is the file as it was first stored."]
        order = ORDER_WORDS.get(decided.get("order_status") or "", "as scanned")
        lines.append(f"Pages: {pages}. Page order: {order}.")
        turned = sum(1 for turn in decided.get("rotations") or [] if turn)
        if turned:
            sentence = f"{turned} of {pages} pages were turned upright in the searchable copy"
            followed = int(decided.get("rotations_from_stack") or 0)
            if followed:
                sentence += (f"; {followed} with too little text to judge alone "
                             f"{'was' if followed == 1 else 'were'} turned like the rest of the stack")
            lines.append(sentence + ".")
        if decided.get("quality_notes"):
            lines.append("Text layer: " + "; ".join(decided["quality_notes"]) + ". The page images aren't affected.")
        if job.state.get("gap"):
            lines.append(f"The printed page numbers say a page may be missing: {job.state['gap']}. "
                         f"Nothing was held for it, because the document was already stored.")
        if warnings:
            lines.append("Read-back warnings: " + "; ".join(warnings) + ".")
        return " ".join(lines)

    # For the Mail intake page -----------------------------------------------------------

    def summary(self) -> dict:
        jobs = self.all()
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        return {"counts": counts, "jobs": jobs}


def _has_own_text(path: Path) -> bool:
    """Whether a PDF carries real text of its own: made on a computer, or already OCRed by its scanner."""
    try:
        reader = PdfReader(str(path))
        pages = reader.pages[:5]
        words = sum(len((page.extract_text() or "").split()) for page in pages)
        return bool(pages) and words >= OWN_TEXT_WORDS_PER_PAGE * len(pages)
    except Exception:
        return False
