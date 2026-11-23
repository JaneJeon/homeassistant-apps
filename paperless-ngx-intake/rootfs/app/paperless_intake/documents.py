"""Documents: one record for each document in Paperless, and the loop that keeps them in order.

How a document arrived matters for one thing only: whether anything could be
checked before it was stored. That is the scanner's folder, and pipeline.py.
Once a document is in Paperless the route no longer matters. Every document
gets the same question: was it scanned, and does it lack a searchable version?

This file answers that question, for every document the app can see: scans the
app stored itself, documents that came in through Paperless's web page, phone
app or email, and everything stored before the app existed.

Who owns a document. It belongs to its scan record (store.py) until that scan
has finished going in and has written the document's record here. From then
on it belongs to this loop. A document that an unfinished scan is still
putting in is left alone, so the two can never work on the same document.

The record. One small folder per document, named after its id in Paperless:
intake/documents/<id>/record.json. It says what kind of file version 1 is,
whether the document has its searchable version, which release of the app made
it, and if it has none, why. events.jsonl beside it keeps every change of state.

  done          it has its searchable version, and Paperless reads it back right
  not_needed    for good: the file has its own text, or almost no text at all,
                or someone else added a file version to the document
  gave_up       for good: it can't be done (Google can't read it, or it is
                gone from Paperless). The reason is recorded
  next_release  this release couldn't, and the next release of the app tries
                once: the searchable version reads back badly or couldn't be
                built, the file isn't a PDF, or a day of tries didn't get through
  later         not now: tried again in an hour, or tomorrow when stored
                documents have used their share of the day's Google pages
  working       handed out to be worked on. Found like this when the app
                starts, the work was cut short, and it is started again first

The loop (service.py calls step() when no scan is waiting). One document at a
time. A document cut short is done first. Then one that is due another try.
Then one left for this release by an earlier one. Then the next document in
Paperless that has no record: every few minutes the app asks Paperless for its list, so
nothing has to tell the app that a document arrived. If work stops halfway it
starts again from the top and skips what is already done: is the file here, is
the searchable version built, does the document already have it, is the note
there.

What it may change in Paperless: it adds one file version and one note. It adds
a version only when every other file version on the document is one the app
itself added. It never touches owner, tags or date, and never deletes.

What it never does: hold, ask or alert. The document is already stored. A page
that looks missing goes into the note. The one message it sends closes an
alert that the scan's own route raised earlier, once the cause is gone.
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

from . import __version__, searchable
from .config import Settings
from .paperless import Paperless, PaperlessError, PaperlessTaskFailed
from .pipeline import ORDER_WORDS, ORIGINAL_ONLY, WITH_GOOGLE, Hold, Pipeline, Wait
from .searchable import sha256
from .store import ACTIVE, COMMITTED, COMMITTED_WARNING, HELD, Item, Store, iso, now, write_atomic

log = logging.getLogger(__name__)

DONE = "done"
NOT_NEEDED = "not_needed"
GAVE_UP = "gave_up"
NEXT_RELEASE = "next_release"
LATER = "later"
WORKING = "working"

WORDS = {DONE: "has its searchable version", NOT_NEEDED: "needs nothing from the app",
         GAVE_UP: "is left as it is, for good", NEXT_RELEASE: "is left for the next release of the app"}

LOOK_MINUTES = 5                # how often Paperless is asked what it holds, when there was nothing to do
LATER_MINUTES = 60              # the wait between tries when something outside was in the way
TRIES_WHEN_OUTSIDE = 24         # a day of hourly tries, as a scan gets
TRIES_WHEN_ODD = 3              # tries after something nobody planned for (an error in the app, a dead worker),
                                # and how often work may be cut short together with the whole app
CAP_SHARE = 0.75                # this work stops at this share of the day's Google pages, so scans keep the rest
OWN_TEXT_WORDS_PER_PAGE = 20    # a PDF with this much text inside the file was not scanned, or was read by its scanner
# Release 1.3.0 starts on no more than this many documents that didn't come from the scanner.
# They are looked at by eye before the next release lifts the limit. None: no limit.
NEW_DOCUMENT_LIMIT: int | None = 3
OWN_LABEL = "Google OCR "       # how every file version the app adds is labelled (pipeline.version_label)


class NotNeeded(Exception):
    """This document needs no searchable version from the app, now or later."""


class GiveUp(Exception):
    """This document can't be given its searchable version, by this release or a later one."""


class NextRelease(Exception):
    """This release can't do it. The next release of the app tries once."""


class Later(Exception):
    """Not now. odd: nobody planned for this, so it gets few tries. Otherwise something outside was in the way.

    ask_google_afresh: Google was what failed, so the next try doesn't reuse this try's saved requests.
    """

    def __init__(self, reason: str, *, odd: bool = False, ask_google_afresh: bool = False):
        super().__init__(reason)
        self.odd = odd
        self.ask_google_afresh = ask_google_afresh


class NotToday(Exception):
    """Stored documents have used their share of today's Google pages. Not a failed try."""


@dataclass
class Record:
    root: Path
    state: dict

    @property
    def doc_id(self) -> int:
        return int(self.state["doc_id"])

    @property
    def status(self) -> str | None:
        return self.state.get("status")

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
        """Google's answers for this document are kept here until the work ends, so a second try doesn't pay twice."""
        return self.root / "google" / f"run-{self.state.get('google_run', 1)}"

    def new_work_dir(self) -> Path:
        work = self.root / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        return work

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        write_atomic(self.root / "record.json", (json.dumps(self.state, indent=2, default=str) + "\n").encode())

    def set(self, status: str, **fields) -> None:
        """Record a change of state: the event first, then the record, as a scan's record does."""
        self.root.mkdir(parents=True, exist_ok=True)
        line = {"at": iso(now()), "status": status, **fields}
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, sort_keys=True, default=str) + "\n")
        self.state.update(fields)
        self.state["status"] = status
        self.save()

    def tidy(self) -> None:
        """Drop the working files. Paperless has the document; the record stays."""
        for name in ("source", "work", "google"):
            shutil.rmtree(self.root / name, ignore_errors=True)
        for path in (self.copy_path, self.text_path):
            path.unlink(missing_ok=True)
        if self.state.get("pending_sha256"):
            self.state["pending_sha256"] = None
            self.save()


class Documents:
    def __init__(self, settings: Settings, store: Store, paperless: Paperless, pipeline: Pipeline, *,
                 clock: Callable[[], float] = now, limit: int | None = NEW_DOCUMENT_LIMIT):
        self.settings = settings
        self.store = store
        self.paperless = paperless
        self.pipeline = pipeline
        self.clock = clock
        self.limit = limit
        self.dir = settings.intake_dir / "documents"
        self._look_again_at = 0.0
        self._said: set[str] = set()            # log lines that are said once, not at every look

    # Records --------------------------------------------------------------------------

    def exists(self, doc_id: int) -> bool:
        return (self.dir / str(int(doc_id)) / "record.json").exists()

    def load(self, doc_id: int) -> Record:
        root = self.dir / str(int(doc_id))
        return Record(root, json.loads((root / "record.json").read_text(encoding="utf-8")))

    def all(self) -> list[Record]:
        records = []
        if not self.dir.is_dir():
            return records
        for child in self.dir.iterdir():
            try:
                records.append(Record(child, json.loads((child / "record.json").read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        records.sort(key=lambda record: record.doc_id)
        return records

    def adopt_scans(self) -> int:
        """Give every scan that finished going in under an earlier release its document record. Run at start."""
        made = 0
        for item in self.store.all():
            if (item.status in {COMMITTED, COMMITTED_WARNING} and item.state.get("doc_id")
                    and not self.exists(item.state["doc_id"])):
                self.record_scan(item)
                made += 1
        return made

    def record_scan(self, item: Item) -> Record | None:
        """A scan has finished going into Paperless. Write its document's record from what really happened.

        This is the hand-over: from here on the document belongs to the loop, not to the scan.
        """
        state = item.state
        if not state.get("doc_id"):
            return None
        doc_id = int(state["doc_id"])
        # A scan record from a release too old to say which release prepared it: any release may redo it.
        made_by = state.get("prepared_by") or "an earlier release"
        fields = dict(reason=None, by=made_by)
        added = []
        # Releases before 1.1.3 recorded these two outcomes only as a sentence.
        no_copy = str(state.get("no_google_copy_reason") or "")
        not_built = state.get("copy_not_built") or no_copy.startswith("the searchable copy couldn't be built")
        reads_badly = bool(state["copy_reads_badly"]) if "copy_reads_badly" in state else (
            item.status == COMMITTED_WARNING and "search text differs" in str(state.get("reason") or ""))
        if state.get("mode") == WITH_GOOGLE and state.get("searchable_sha256"):
            if state.get("copy_in_paperless") is False:
                status = NEXT_RELEASE
                fields.update(reason="Paperless didn't list its searchable version after taking it")
            else:
                added = [state["searchable_sha256"]]
                status = NEXT_RELEASE if reads_badly else DONE
                if reads_badly:
                    fields.update(reason="Paperless couldn't read all the words of its searchable version",
                                  reads_badly=True)
        elif state.get("short_text"):
            status = NOT_NEEDED
            fields.update(reason="Google found almost no text in it")
        elif not_built:
            status = NEXT_RELEASE
            fields.update(reason=no_copy or "the app couldn't build its searchable version")
        else:
            status = GAVE_UP
            fields.update(reason=no_copy or "it was stored without a searchable version")
        record = Record(self.dir / str(doc_id), {
            "doc_id": doc_id,
            "source": "scanner",
            "original_sha256": item.sha,        # also names the scan's own record, in intake/items/
            "original_name": state.get("original_name"),
            "page_count": state.get("page_count"),
            "kind": "scanned",
            "added": added,
            "recorded_at": iso(self.clock()),
            "tries": {},
            "google_run": 1,
        })
        record.set(status, **fields)
        return record

    # The loop -------------------------------------------------------------------------

    def step(self) -> Record | None:
        """Choose the one document to work on now, if any, and mark it. The caller runs it (in a worker process)."""
        while True:
            records = self.all()
            chosen = self._choose(records)
            if chosen is None:
                return None
            if chosen.status != WORKING:
                # Marked before the work starts, so a worker that dies at once is seen and counted.
                chosen.set(WORKING, by=__version__, cut_short=0)
                return chosen
            # Still marked from before: the whole app stopped while it had this document in hand.
            cut_short = int(chosen.state.get("cut_short") or 0) + 1
            if cut_short <= TRIES_WHEN_ODD:
                chosen.set(WORKING, by=__version__, cut_short=cut_short)
                return chosen
            self._finish(chosen, NEXT_RELEASE,
                         f"the app stopped {cut_short - 1} times while working on it", cut_short=0)

    def _choose(self, records: list[Record]) -> Record | None:
        chosen = next((record for record in records if record.status == WORKING), None)
        if chosen is None:
            chosen = next((record for record in records if record.status == LATER
                           and float(record.state.get("not_before_unix") or 0) <= self.clock()), None)
        if chosen is None:
            # Each release tries once what an earlier release left for it.
            chosen = next((record for record in records if record.status == NEXT_RELEASE
                           and record.state.get("by") != __version__), None)
        if chosen is None and self.clock() >= self._look_again_at:
            chosen = self._next_unseen(records)
        return chosen

    def _next_unseen(self, records: list[Record]) -> Record | None:
        """The next document in Paperless that has no record. Asks Paperless; nothing is pushed to the app."""
        self._look_again_at = self.clock() + LOOK_MINUTES * 60      # unless this look finds one
        try:
            rows = self.paperless.documents()
        except PaperlessError as error:
            log.info("couldn't ask Paperless what it holds (%s); asking again in %d minutes", error, LOOK_MINUTES)
            return None
        known = {record.doc_id for record in records}
        started = sum(1 for record in records if record.state.get("source") == "paperless"
                      and record.status != NOT_NEEDED and record.state.get("kind") != "not_pdf")
        for row in sorted(rows, key=lambda row: int(row["id"])):
            doc_id = int(row["id"])
            if doc_id in known:
                continue
            versions = row.get("versions") or []
            root = next((version for version in versions if version.get("is_root")), None)
            checksum = str((root or {}).get("checksum") or "")
            if not checksum:
                # Without it the app can't tell whether a scan is still putting this document in.
                self._say_once(f"no file {doc_id}", "Paperless doesn't say which file document %s holds; "
                                                    "leaving it alone", doc_id)
                continue
            if self.store.exists(checksum):
                # The same file went through the scanner's folder. Is this the document that scan made?
                item = self.store.load(checksum)
                theirs = item.state.get("doc_id")
                if theirs is None and (item.status in ACTIVE or item.status == HELD):
                    continue                              # that scan is still going in; this may be its document
                if theirs is not None and int(theirs) == doc_id:
                    if item.status in {COMMITTED, COMMITTED_WARNING}:
                        self.record_scan(item)            # a finished scan whose record is missing
                    continue                              # otherwise the scan still owns it
                # Another document that happens to hold the same file: treated like any other.
            if self.limit is not None and started >= self.limit:
                self._say_once("limit", "this release starts on %d documents that didn't come from the scanner, "
                                        "and it has; the rest wait for the next release", self.limit)
                return None
            log.info("document %s has no record yet; looking at it", doc_id)
            self._look_again_at = 0.0                     # there may be more: look again as soon as this one is done
            # Not written down yet: step() does that, as it hands the document out.
            return Record(self.dir / str(doc_id), {
                "doc_id": doc_id,
                "source": "paperless",
                "original_name": row.get("original_file_name") or f"document-{doc_id}.pdf",
                "page_count": row.get("page_count"),
                "added": [],
                "recorded_at": iso(self.clock()),
                "tries": {},
                "google_run": 1,
            })
        return None

    def _say_once(self, key: str, message: str, *args) -> None:
        if key not in self._said:
            self._said.add(key)
            log.info(message, *args)

    def run(self, record: Record) -> None:
        """Work one document to an outcome. Whatever happens, its record ends in a state the loop understands."""
        try:
            self._work(record)
        except NotNeeded as outcome:
            self._finish(record, NOT_NEEDED, str(outcome))
        except GiveUp as outcome:
            self._finish(record, GAVE_UP, str(outcome))
        except NextRelease as outcome:
            self._finish(record, NEXT_RELEASE, str(outcome))
        except NotToday as outcome:
            # Waiting for the day's allowance is not a failed try. It is picked up after midnight, UTC,
            # which is when the count of pages starts again.
            log.info("%s waits for tomorrow: %s", record.label, outcome)
            record.set(LATER, reason=str(outcome), by=__version__,
                       not_before_unix=(int(self.clock() // 86400) + 1) * 86400 + 60)
        except Later as outcome:
            self._later(record, str(outcome), odd=outcome.odd, ask_google_afresh=outcome.ask_google_afresh)
        except Exception as error:      # a defect in the app must not spin the loop or lose the record
            log.exception("unexpected error on %s", record.label)
            if record.status == WORKING:    # an outcome that is already recorded stands
                self._later(record, f"an unexpected error in the app ({type(error).__name__})", odd=True)

    def worker_stopped(self, record: Record, code: int) -> None:
        """The child process working on this document died. It counts as a try, so it can't go round for ever."""
        if record.status == WORKING:
            self._later(record, f"the work on it stopped with exit code {code}", odd=True)

    def _finish(self, record: Record, status: str, why: str | None, **fields) -> None:
        """Record an outcome, then drop the working files. Trouble with the files can't undo the outcome."""
        fields.setdefault("by", __version__)
        record.set(status, reason=why, finished_at=iso(self.clock()), **fields)
        log.info("%s %s%s", record.label, WORDS[status], f": {why}" if why else "")
        try:
            record.tidy()
        except Exception:
            log.exception("couldn't remove the working files of %s; its outcome is recorded", record.label)

    def _later(self, record: Record, why: str, *, odd: bool, ask_google_afresh: bool = False) -> None:
        tries = dict(record.state.get("tries") or {})
        same = tries.get("release") == __version__
        count = (int(tries.get("count", 0)) if same else 0) + 1
        odd_count = (int(tries.get("odd", 0)) if same else 0) + (1 if odd else 0)
        tries = {"release": __version__, "count": count, "odd": odd_count}
        if odd_count >= TRIES_WHEN_ODD or count >= TRIES_WHEN_OUTSIDE:
            self._finish(record, NEXT_RELEASE, f"{why} (after {count} tries)", tries=tries)
            return
        log.info("%s waits and is tried again: %s", record.label, why)
        # Google's saved answers are kept for the next try, so it never pays twice. Only when Google
        # itself failed does the next try start a new run: this run's retry budget may be spent.
        record.set(LATER, reason=why, tries=tries, by=__version__,
                   not_before_unix=self.clock() + LATER_MINUTES * 60,
                   google_run=int(record.state.get("google_run", 1)) + (1 if ask_google_afresh else 0))

    def _work(self, record: Record) -> None:
        doc_id = record.doc_id
        if record.status != WORKING:        # step() hands it out marked; this is for a caller that didn't use step()
            record.set(WORKING, by=__version__)

        # Nothing is written unless Paperless is at a version the tests passed on, as for scans.
        live = self._ask(self.paperless.server_version)
        if not self.pipeline.version_accepted(live):
            raise Later(f"Paperless is {live}, but the app was tested on {self.settings.tested_paperless_version}")

        # 1. The document as Paperless has it now.
        try:
            versions = self.paperless.versions(doc_id)
        except PaperlessError as error:
            if "HTTP 404" in str(error):
                raise GiveUp("the document is no longer in Paperless, or the app may no longer see it")
            raise Later(str(error))
        root = next((version for version in versions if version.is_root), None)
        if root is None:
            raise GiveUp("Paperless doesn't say which file version is the first")
        mine = set(record.state.get("added") or [])
        pending = record.state.get("pending_sha256")
        # The app's own file versions: the ones this record lists, and any carrying the app's label
        # (earlier releases added some that no record lists).
        labelled = [v for v in versions if not v.is_root and str(v.label or "").startswith(OWN_LABEL)]
        if any(not v.is_root and v.checksum not in mine and v.checksum != pending and v not in labelled
               for v in versions):
            raise NotNeeded("someone else added a file version to it, so the app leaves it alone")
        if not mine and not pending and labelled and versions[0] in labelled:
            # The app has no record of adding anything, and the document's newest file version carries
            # the app's label. The document is what counts: it has its searchable version already.
            # (This is what a document looks like if the app's own records are ever lost.)
            release = str(versions[0].label).rsplit("intake ", 1)[-1] if "intake " in str(versions[0].label) else None
            self._finish(record, DONE, None, added=[v.checksum for v in labelled], kind="scanned",
                         by=release or "an earlier release", found=str(versions[0].label))
            return

        # 2. Version 1, the file as it was first stored.
        if not record.source_path.exists():
            data = self._ask(lambda: self.paperless.download_original(doc_id, root.id))
            record.source_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(record.source_path, data)
        data = record.source_path.read_bytes()
        fingerprint = sha256(data)
        if root.checksum and fingerprint != root.checksum:
            record.source_path.unlink()
            raise Later("the file Paperless sent doesn't match Paperless's own checksum for it", odd=True)
        if record.state.get("original_sha256") not in {None, fingerprint}:
            raise GiveUp("version 1 in Paperless is no longer the file that was stored")
        if record.state.get("original_sha256") != fingerprint:
            record.state["original_sha256"] = fingerprint
            record.save()
        if not record.state.get("kind"):
            if not data.startswith(b"%PDF-"):
                record.state["kind"] = "not_pdf"
                record.save()
                raise NextRelease("it isn't a PDF, and this release reads only PDFs")
            # Tested on the file itself. Paperless's own text for the document is no guide:
            # for an old document it holds Tesseract's reading of a scan.
            record.state["kind"] = "has_text" if _has_own_text(record.source_path) else "scanned"
            record.save()
        if record.state["kind"] == "has_text":
            raise NotNeeded("the file already has text inside it, so there is nothing to read off the page images")
        if record.state["kind"] != "scanned":
            raise NextRelease("it isn't a PDF, and this release reads only PDFs")

        # 3. The searchable version. Built once and kept until the work ends, so work that is
        #    started again offers Paperless the same bytes and can ask whether it has them.
        if not (pending and record.copy_path.exists() and record.text_path.exists()
                and sha256(record.copy_path.read_bytes()) == pending):
            # Scans may use the whole of the day's Google pages. Stored documents stop at their share,
            # on every try, so that a scan arriving later in the day always has room.
            share = int(self.settings.daily_page_cap * CAP_SHARE)
            if _page_count(record.source_path) > share:
                raise NextRelease(f"it has more pages than the app sends to Google in a day "
                                  f"for stored documents ({share})")
            try:
                made = self.pipeline.make_copy(record.source_path, lambda: self.pipeline.backend_factory(record),
                                               record.new_work_dir, record.label, page_allowance=share)
            except Hold as hold:
                raise GiveUp(hold.reason)
            except Wait as wait:
                if wait.kind == "page_cap":
                    raise NotToday(f"stored documents have used their share of today's Google pages ({share})")
                raise Later(wait.reason, ask_google_afresh=wait.kind in {"google", "auth"})
            write_atomic(record.root / "checks.json",
                         (json.dumps({"at": iso(self.clock()), "release": __version__, **made.record},
                                     indent=2, default=str) + "\n").encode())
            if made.why_not:
                raise NextRelease(made.why_not) if made.ours else GiveUp(made.why_not)
            if made.fields["mode"] == ORIGINAL_ONLY:
                raise NotNeeded("Google found almost no text in it")
            write_atomic(record.copy_path, made.searchable)
            write_atomic(record.text_path, made.text.encode("utf-8"))
            pending = made.fields["searchable_sha256"]
            record.state.update(pending_sha256=pending, page_count=made.page_count, gap=made.gap,
                                decided=made.fields, version_task=None)
            record.save()
        redo = bool(mine) and pending not in mine
        if pending in mine:
            # This release built exactly the file the document already has. Nothing to add.
            badly = bool(record.state.get("reads_badly"))
            self._finish(record, NEXT_RELEASE if badly else DONE,
                         "Paperless couldn't read all the words of its searchable version, "
                         "and this release builds the same one" if badly else None)
            return

        # 4. Add it, unless Paperless already has it, and read it back.
        def remember(task):
            record.state["version_task"] = task
            record.save()

        stem = Path(record.state.get("original_name") or f"document-{doc_id}").stem
        try:
            added = searchable.ensure_added(self.paperless, doc_id, pending, record.copy_path.read_bytes,
                                            f"{stem}.searchable.pdf", self.pipeline.version_label(),
                                            task=record.state.get("version_task"), remember=remember)
        except PaperlessTaskFailed as error:
            raise Later(f"Paperless didn't take the new file version ({error}: {error.result[:200]})", odd=True)
        except PaperlessError as error:
            raise Later(str(error))
        if not added:
            raise Later("the searchable version on disk changed before it was added", odd=True)
        try:
            warnings, measured = searchable.read_back(self.paperless, doc_id, pending,
                                                      record.text_path.read_text(encoding="utf-8"))
        except PaperlessError as error:
            raise Later(str(error))
        if "version_id" not in measured:
            raise Later("Paperless took the searchable version but doesn't list it", odd=True)
        reads_badly = not measured["text_match"]["ok"]

        # 5. The note, once. It names the file version, as Paperless's Versions menu does.
        mark = f"file version {pending[:8]}"
        notes = self._ask(lambda: self.paperless.notes(doc_id))
        if not any(mark in str(note.get("note") or "") for note in notes):
            self._ask(lambda: self.paperless.add_note(doc_id, self.note_text(record, warnings, mark, redo=redo)))

        self._finish(record, NEXT_RELEASE if reads_badly else DONE, "; ".join(warnings) or None,
                     added=[*(record.state.get("added") or []), pending], version_id=measured["version_id"],
                     warnings=warnings, reads_badly=reads_badly)
        if not warnings:
            try:
                self._close_alert(record)
            except Exception:       # the work is done and recorded; a message that didn't go out can't undo it
                log.exception("couldn't close the earlier alert about %s", record.label)

    def _ask(self, call):
        """A call to Paperless whose failing only means: try this document again later."""
        try:
            return call()
        except PaperlessError as error:
            raise Later(str(error))

    def _close_alert(self, record: Record) -> None:
        """The scan's own route told Jane something was wrong with this document. It no longer is."""
        sha = record.state.get("original_sha256")
        if record.state.get("source") == "scanner" and sha and self.store.exists(sha):
            self.pipeline.close_alert(self.store.load(sha))

    def note_text(self, record: Record, warnings: list[str], mark: str, *, redo: bool) -> str:
        decided = record.state.get("decided") or {}
        pages = record.state.get("page_count")
        processor = self.settings.processor_version.rsplit("/", 1)[-1]
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if record.state.get("source") == "scanner":
            lines = [f"paperless-intake {__version__} "
                     f"{'built the searchable version again' if redo else 'added the searchable version'} on {day} "
                     f"({mark}, Google OCR {processor})."]
            lines.append("An earlier note here describes the one it replaces, which is still in the version history."
                         if redo else
                         "An earlier note here says only the original was stored; that was true then.")
        else:
            lines = [f"This document came into Paperless another way than the scanner's folder. "
                     f"paperless-intake {__version__} gave it a searchable version on {day} "
                     f"({mark}, Google OCR {processor}).",
                     "The newest version is the upright, searchable copy; version 1 is the file as it was first stored."]
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
        if record.state.get("gap") and record.state.get("source") != "scanner":
            lines.append(f"The printed page numbers say a page may be missing: {record.state['gap']}. "
                         f"Nothing was held for it, because the document was already stored.")
        if warnings:
            lines.append("Read-back warnings: " + "; ".join(warnings) + ".")
        return " ".join(lines)

def _page_count(path: Path) -> int:
    try:
        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0            # not readable here: make_copy says so in its own words


def _has_own_text(path: Path) -> bool:
    """Whether a PDF carries real text inside the file: made on a computer, or already read by its scanner."""
    try:
        reader = PdfReader(str(path))
        pages = reader.pages[:5]
        words = sum(len((page.extract_text() or "").split()) for page in pages)
        return bool(pages) and words >= OWN_TEXT_WORDS_PER_PAGE * len(pages)
    except Exception:
        return False
