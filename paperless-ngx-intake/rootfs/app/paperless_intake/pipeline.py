"""What happens to one scan: OCR and checks, then commit and read-back.

What can stop a scan, and why only this. A scan is held for Jane when
something bears on whether the paper can be let go: printed page numbers show
a page the scan doesn't have, or the file isn't a readable PDF. A lost page can
only be recovered from the paper, so that question has to be answered first.

Everything else is about the searchable copy: which way up a page is, what
order the pages go in, where the invisible text sits, whether Google could
read the scan at all. None of that can lose anything. The original scan goes
into Paperless byte for byte either way, and a better copy can be made from it
later. So those are decided as well as the evidence allows, recorded in the
document's note, and never hold a scan. (Version 1.0 held on all of them, and
held both of the first two real scans.)

Every step records its state before and after it runs, so a restart resumes
where it stopped:
- Google responses are cached per item and attempt, so a restart never pays twice.
- Before uploading, Paperless is asked for the original's checksum, and a
  recorded upload task is waited for, so a restart never uploads twice.
- Before adding version 2, the versions list is checked for its checksum.

This file never deletes or edits anything in Paperless except the document it
just created. Its tags are set at upload; after that it clears the owner Paperless
gives an API upload, and adds a version and a note. When a scan has finished going
in, its document is handed over to documents.py, which is what looks after every
document once it is stored.
"""

from __future__ import annotations

import gzip
import json
import re
import logging
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from . import __version__, checks
from .config import Settings
from .docai.errors import ParserError
from .docai.google_backend import GoogleBackendConfig, GoogleBackendError, GoogleDocumentAIBackend
from .docai.page_order import plan_page_order
from .docai.pdf_renderer import ReviewRequired, describe_source
from .docai.renderer_adapter import StackRenderer
from .docai.validation import assert_unchanged, document_for_renderer, inspect_source, ordered_text, validate_result
from .gate import Intent
from .notify import Notifier
from . import searchable
from .paperless import NO_TAG, Paperless, PaperlessError, PaperlessTaskFailed
from .searchable import sha256
from .store import (COMMITTED, COMMITTED_WARNING, COMMITTING, CAPTURED, DISCARDED, FAILED, HELD, READY, WAITING,
                    Item, Store,
                    copy_atomic, iso, now, write_atomic)
from .usage import GoogleUsage

# A scan has ended, for the watchdog's count, once it is in one of these.
ENDED = {HELD, COMMITTED, COMMITTED_WARNING, FAILED, DISCARDED}
# Waits the queue's gate records for a scan it keeps from starting (jobs.py).
GATE_KINDS = {"memory_low", "disk_low", "io_pressure", "memory_pressure", "upload_in_doubt"}

log = logging.getLogger(__name__)

# The scanner names a file after its own clock: 20261122_144510_000353.pdf.
_SCANNER_NAME = re.compile(r"^(\d{4})(\d{2})(\d{2})_\d{6}(?:[_.]|$)")


def scan_date(original_name: str, captured_at_unix: float) -> str:
    """The day the paper was scanned, which the app gives Paperless as the document's date.

    Taken from the scanner's file name. Without one, or with one from a clock that is plainly
    wrong (ahead of the day the app picked the file up), it is the day the app picked the file
    up, in the app's own time zone. Paperless would otherwise guess a date from its own reading
    of the raw scan, and that reading is not the one anybody sees.
    """
    picked_up = datetime.fromtimestamp(captured_at_unix).date()
    match = _SCANNER_NAME.match(original_name or "")
    if match:
        try:
            named = date(*(int(part) for part in match.groups()))
        except ValueError:
            named = None
        if named and date(2000, 1, 1) <= named <= picked_up + timedelta(days=1):
            return named.isoformat()
    return picked_up.isoformat()

WITH_GOOGLE = "with_google"
ORIGINAL_ONLY = "original_only"

ORDER_WORDS = {
    "complete_numbering_in_order": "as scanned, confirmed by the printed page numbers",
    "reorder_from_complete_numbering": "reordered to follow the printed page numbers",
    "reversed_stack_from_printed_numbers": "reversed, because the printed page numbers that could be read ran "
                                           "backwards (the stack went in flipped)",
    "partial_in_order": "as scanned; the printed page numbers seen agree",
    "unverified_mixed_evidence": "as scanned; the printed page numbers disagree about the order, so it is unverified",
    "unverified_numbers_inconclusive": "as scanned; the printed page numbers that were read don't settle the order",
    "unknown_no_pagination_evidence": "as scanned (no printed page numbers to check)",
}

# Reasons version 1.0 held a scan for that no longer hold one. Scans still held for
# them are put back through the checks once, when this version starts.
@dataclass
class Made:
    """What one try at a searchable copy came to: the copy, or the reason there is none."""

    page_count: int
    record: dict = field(default_factory=dict)      # what was measured, for checks.json
    gap: str | None = None                          # a page the printed numbers say is missing
    why_not: str | None = None                      # no copy: why, in plain words
    ours: bool = False                              # ...and the fault was in this app's own handling
    searchable: bytes | None = None
    text: str | None = None
    fields: dict = field(default_factory=dict)      # what was decided: order, turns, remarks


OBSOLETE_HOLD_KINDS = {"render_check", "google_rejected", "google_mismatch"}

# Google backend failure codes worth retrying later rather than asking Jane.
TRANSIENT_GOOGLE = {
    "post_retry_budget_exhausted", "total_processing_deadline_exceeded", "post_outcome_unknown",
    "request_in_progress", "request_lock_unavailable", "private_directory_unavailable",
    "private_provenance_write_failed",
}
AUTH_GOOGLE = {
    "authentication_failed_before_post", "authentication_rejected_after_refresh",
    "authentication_refresh_failed_requires_configuration", "authentication_refresh_unavailable",
    "invalid_access_token", "adc_authentication_failed",
}
# Failures that belong to this one scan: Google can't take it, and asking again won't help.
# The original goes into Paperless without a Google copy. Any other refusal (permission,
# a retired processor version, a damaged local cache) is about the setup, so the scan
# waits and Jane is told, because every later scan would fail the same way.
THIS_SCAN_GOOGLE = {
    "source_exceeds_40000000_bytes_no_cloud_fallback", "response_missing_document",
    "response_document_validation_failed",
}
THIS_SCAN_HTTP = {400, 413, 422}


@dataclass
class Wait(Exception):
    """Something outside was unavailable. Try again later.

    waits_on: "paperless" or "google" when the failure is about that service, not about this
    scan, so the next job would meet it too. None otherwise, and for the day's page cap.
    """
    reason: str
    kind: str
    stage: str
    alert: bool = False
    waits_on: str | None = None


def _on_paperless(error: PaperlessError) -> str | None:
    return "paperless" if error.outside else None


def _refused_as_duplicate(error: PaperlessTaskFailed) -> bool:
    """Paperless refused an upload because it already holds the file. Only when it is set to refuse duplicates."""
    result = str(error.result or "").lower()
    return "duplicate" in result or "already exists" in result


@dataclass
class Hold(Exception):
    """A check found a gap. Jane decides."""
    reason: str
    kind: str


class Pipeline:
    def __init__(self, settings: Settings, store: Store, paperless: Paperless, notifier: Notifier, *,
                 token_provider=None, backend_factory: Callable[[Item], object] | None = None,
                 clock: Callable[[], float] = now):
        self.settings = settings
        self.store = store
        self.paperless = paperless
        self.notifier = notifier
        self.token_provider = token_provider
        self.backend_factory = backend_factory or self._google_backend
        self.renderer = StackRenderer()
        self.clock = clock
        self.usage = GoogleUsage(settings.intake_dir / "google-usage.json", clock)
        self.intent = Intent(settings.intake_dir / "upload-in-doubt.json", clock)
        # Set by wiring: where a scan's document is handed over once the scan has finished going in.
        self.documents = None
        self.left_to_tidy: list[str] = []      # committed scans whose Google answers a worker summarises (recover)

    # Dispatch -------------------------------------------------------------------

    def due(self, item: Item) -> bool:
        if item.status in {CAPTURED, READY, COMMITTING}:
            return True
        if item.status == WAITING:
            return float(item.state.get("next_attempt_at_unix") or 0) <= self.clock()
        return False

    def run(self, item: Item) -> None:
        """Advance one item as far as it can go now."""
        try:
            if item.status == WAITING:
                stage = item.state.get("retry_stage", "ocr")
                if stage == "ocr" and item.state.get("retry_new_google_run"):
                    item.state["google_run"] = int(item.state.get("google_run", 1)) + 1
                    item.state["retry_new_google_run"] = False
                item.set(CAPTURED if stage == "ocr" else READY, reason=None, reason_kind=None, waits_on=None)
            if item.status == CAPTURED:
                self.prepare(item)
            if item.status in {READY, COMMITTING}:
                self.commit(item)
        except Hold as hold:
            self._hold(item, hold.reason, hold.kind)
        except Wait as wait:
            self._wait(item, wait)
        except Exception as error:  # never lose an item to a bug: wait, keep evidence, retry
            log.exception("unexpected error on %s", item.sha[:12])
            # Alerted once. An error nobody expected is likely to hit every scan the same way,
            # and retrying it quietly for a day would leave them all outside Paperless unnoticed.
            self._wait(item, Wait(f"an unexpected error in the app ({type(error).__name__})", "unexpected",
                                  "commit" if item.status in {READY, COMMITTING} else "ocr", alert=True))

    # Stage 1: OCR, searchable copy, checks -------------------------------------------

    def _google_backend(self, item: Item):
        config = GoogleBackendConfig(self.settings.processor_version, item.google_dir())
        return GoogleDocumentAIBackend(config, token_provider=self.token_provider)

    def pages_today(self) -> int:
        """Pages sent to Google so far today (the local day), for scans and for stored documents together."""
        return self.usage.today()

    def prepare(self, item: Item) -> None:
        def seen(pages: int) -> None:
            if item.state.get("page_count") != pages:
                item.state["page_count"] = pages
                item.save()

        def started() -> Path:
            item.event("ocr_started", google_run=item.state.get("google_run", 1))
            return item.new_work_dir()

        made = self.make_copy(item.source_path, lambda: self.backend_factory(item), started, item.sha[:12], seen)
        if made.why_not:
            self._ready_without_google(item, made.why_not, checks_record=made.record or None, gap=made.gap,
                                       ours=made.ours)
            return
        write_atomic(item.searchable_path, made.searchable)
        write_atomic(item.root / "text.txt", made.text.encode("utf-8"))
        self._write_checks(item, made.record)
        fields = dict(made.fields, no_google_copy_reason=None, copy_not_built=False, prepared_by=__version__,
                      waiting_since_unix=None, alerted_reason_kind=None)
        if made.gap and not self._settled(item):
            # The searchable copy is ready, so "keep as it is" can commit both versions.
            item.state.update(fields)
            item.save()
            raise Hold(made.gap, "missing_page")
        item.set(READY, **fields)

    def make_copy(self, source_path: Path, backend_for: Callable[[], object], work_dir_for: Callable[[], Path],
                  label: str, on_pages: Callable[[int], None] | None = None,
                  page_allowance: int | None = None) -> "Made":
        """One try at the searchable copy of one file. It writes nothing about any document.

        Used for a scan on its way in (prepare) and for a document that is already in
        Paperless (documents.py). Raises Hold when the file isn't a PDF the app can read, and
        Wait when something outside the file is in the way (the Google key, the day's page
        cap, Google not answering). Otherwise returns what it made: the copy, or the reason
        there is none. Free disk and memory are the queue's to check, before the job starts.
        """
        try:
            source = inspect_source(source_path)
        except ParserError as error:
            raise Hold(f"this isn't a PDF the app can read ({error})", "unsupported_file")
        if on_pages:
            on_pages(source.page_count)
        if self.token_provider is None and self.backend_factory == self._google_backend:
            raise Wait("the Google key isn't set in the app options", "config", "ocr", alert=True, waits_on="google")
        # The one place the day's Google pages are rationed. A scan may use all of them. A caller
        # working on stored documents passes its smaller share.
        allowance = self.settings.daily_page_cap if page_allowance is None else page_allowance
        if self.usage.today() + source.page_count > allowance:
            raise Wait(f"today's Google page cap ({self.settings.daily_page_cap}) is reached", "page_cap", "ocr")

        work_dir = work_dir_for()
        try:
            # Building the backend checks the app's Google settings, and a bad setting
            # must be reported the same way as Google refusing a request.
            result = backend_for().process(source, work_dir)
        except GoogleBackendError as error:
            if error.code in TRANSIENT_GOOGLE:
                raise Wait(f"Google OCR didn't answer ({error.code})", "google", "ocr", waits_on="google")
            if error.code in AUTH_GOOGLE:
                raise Wait(f"Google refused the app's key ({error.code})", "auth", "ocr", alert=True,
                           waits_on="google")
            status = getattr(error, "http_status", None)
            if error.code in THIS_SCAN_GOOGLE or (error.code == "post_http_error_fatal" and status in THIS_SCAN_HTTP):
                detail = f"{error.code}, HTTP {status}" if status else error.code
                return Made(source.page_count, why_not=f"Google couldn't read this scan ({detail})")
            detail = f"{error.code}, HTTP {status}" if status else error.code
            raise Wait(f"Google OCR isn't working ({detail})", "google", "ocr", alert=True, waits_on="google")
        # Counted once per answer, by the file Google's answer was saved in. A job resumed after a
        # kill reads the same files again and adds nothing. (Before 1.4.0: counted when any part
        # of the answer was new, which could miss pages a kill had already paid for.)
        cache_hits = int(result.provenance.get("cache_hits", 0))
        self.usage.count((str(chunk.provenance.get("response_path") or chunk.response_sha256), len(chunk.source_pages))
                         for chunk in result.chunks)

        try:
            validate_result(result, source)
            document = document_for_renderer(result, source)
        except ParserError as error:
            return Made(source.page_count, why_not=f"Google's answer didn't match the scan's pages ({error})", ours=True)

        plan = self._plan(label, document)
        gap = checks.missing_pages(plan, source.page_count)
        record = {"plan": plan.to_dict(), "missing_pages": gap, "coverage": checks.coverage(document),
                  "google": {"processor_version": self.settings.processor_version,
                             "responses": [chunk.response_sha256 for chunk in result.chunks],
                             "cache_hits": cache_hits}}
        try:
            rendered = self.renderer.render(source, result, work_dir, plan=plan)
        except Exception as error:
            # Whatever went wrong, it went wrong in making the copy. The scan itself is fine and
            # still goes in. A defect here must never leave a scan stranded outside Paperless.
            expected = isinstance(error, (ReviewRequired, ParserError, ValueError))
            log.warning("%s: no searchable copy: %s", label, error, exc_info=not expected)
            try:
                shape = describe_source(source.path)
            except Exception as failure:
                shape = f"couldn't be read ({type(failure).__name__})"
            record["source_shape"] = shape
            log.warning("%s: the scan's pages as the scanner wrote them: %s", label, shape)
            why = str(error) if expected else f"an unexpected {type(error).__name__} in the app"
            return Made(source.page_count, why_not=f"the searchable copy couldn't be built ({why})", ours=True, gap=gap,
                        record={**record, "render_error": f"{type(error).__name__}: {error}"})
        assert_unchanged(source)
        order = tuple(rendered.page_order)
        text = ordered_text(result, source, order)
        searchable = rendered.archive_path.read_bytes()

        mode = ORIGINAL_ONLY if checks.short_text(text) else WITH_GOOGLE
        provenance = rendered.provenance
        decision = provenance.get("page_order_decision", {})
        rotations = provenance.get("orientation_decisions") or []
        notes = list(provenance.get("quality_notes") or [])
        record = {
            **record,
            "render_checks": provenance.get("checks"),
            "render_pages": provenance.get("pages"),
            "orientation_decisions": rotations,
            "page_order_decision": decision,
            "quality_notes": notes,
            "text_sha256": sha256(text.encode("utf-8")),
            "searchable_sha256": sha256(searchable),
            "mode": mode,
        }
        log.info("%s: order %s (forward votes %s, reverse votes %s); %s", label,
                 decision.get("status"), decision.get("forwardVotes"), decision.get("reverseVotes"),
                 "; ".join(notes) or "text layer placed without remarks")
        log.info("%s: pages as scanned: %s", label, self._page_summary(provenance))
        fields = dict(mode=mode, page_order=list(order), order_status=decision.get("status"),
                      rotations=[r.get("appliedCardinalClockwiseDegrees", 0) for r in rotations],
                      rotations_from_stack=sum(1 for r in rotations if r.get("followedStack")),
                      quality_notes=notes, searchable_sha256=sha256(searchable), short_text=mode == ORIGINAL_ONLY)
        return Made(source.page_count, record=record, gap=gap, searchable=searchable, text=text, fields=fields)

    @staticmethod
    def _plan(label: str, document: dict):
        """The printed page numbers, read as evidence. A failure to read them is no evidence, not a stop."""
        try:
            return plan_page_order(document)
        except Exception as error:
            log.warning("%s: page numbers couldn't be read as evidence", label, exc_info=True)
            from .docai.page_order import OrderPlan
            pages = len(document.get("pages") or [])
            return OrderPlan(tuple(range(1, pages + 1)), "needs_review", (),
                             f"Planner failed with {type(error).__name__}; no pagination evidence was used.")

    @staticmethod
    def _page_summary(provenance: dict) -> str:
        """One compact entry per source page: turn, where the turn came from, word count, skew, image
        format, image match.

        Numbers and format names only, never document text. This is what lets a scan's handling be
        checked from the log.
        """
        by_source = {page.get("sourcePage"): page for page in provenance.get("pages") or []}
        parts = []
        for decision in provenance.get("orientation_decisions") or []:
            number = decision.get("sourcePage")
            page = by_source.get(number, {})
            match = page.get("registration") or {}
            text = page.get("text") or {}
            entry = (f"p{number} turn {decision.get('appliedCardinalClockwiseDegrees')} "
                     f"({'stack' if decision.get('followedStack') else decision.get('source')}), "
                     f"{decision.get('words')} words, {text.get('placed', 0)} placed")
            labels = "+".join(f"{count} {label}" for label, count in sorted((decision.get("lineLabels") or {}).items()))
            entry += f", Google labels page {decision.get('pageLabel')} lines {labels or 'none'}"
            if decision.get("followedStack"):
                entry += f" (Google read {decision.get('cardinal')})"
            if text.get("leftOut"):
                entry += f", {text['leftOut']} left out"
            if decision.get("skewDegrees") is not None:
                entry += f", skew {decision['skewDegrees']:+.2f}"
            formats = "+".join(f"{image.get('format')} {image.get('bitsPerComponent')}-bit"
                               for image in page.get("images") or []) or "no image"
            entry += f", {formats}"
            if match.get("measured"):
                entry += f", image match {match['correlation']:.3f} (gray difference {match['meanAbsoluteGrayDifference']:.1f})"
            elif match.get("reason"):
                entry += f", image match not measured: {match['reason']}"
            entry += f", {page.get('rasterMapping', 'no mapping')}"
            parts.append(entry)
        return " | ".join(parts)

    def _ready_without_google(self, item: Item, why: str, *, checks_record: dict | None = None,
                              gap: str | None = None, ours: bool = False) -> None:
        """No searchable copy can be made. The original still goes in, and Jane is told once it has.

        ours: the failure was in this app's own handling, not in Google refusing the scan.
        A later release of the app may do better, so the documents loop tries again then.
        """
        self._write_checks(item, {**(checks_record or {}), "mode": ORIGINAL_ONLY, "no_google_copy_reason": why})
        fields = dict(mode=ORIGINAL_ONLY, no_google_copy_reason=why, copy_not_built=ours, prepared_by=__version__,
                      quality_notes=[], short_text=False, waiting_since_unix=None, alerted_reason_kind=None)
        if gap and not self._settled(item):
            item.state.update(fields)
            item.save()
            raise Hold(gap, "missing_page")
        item.set(READY, **fields)

    @staticmethod
    def _settled(item: Item) -> bool:
        """Whether the question of a missing page is already closed for this scan.

        It is closed once Jane has chosen to keep the scan, or once its original is in Paperless.
        Only a scan being prepared again (see reprepare_unfinished) can get here in that state,
        and holding it a second time would ask Jane what she has already answered.
        """
        state = item.state
        return bool(state.get("doc_id") or state.get("upload_task")
                    or state.get("jane_decision") in {"keep", "keep_original"})

    def _write_checks(self, item: Item, data: dict) -> None:
        data = {"at": iso(self.clock()), "google_run": item.state.get("google_run", 1), **data}
        write_atomic(item.root / "checks.json", (json.dumps(data, indent=2, default=str) + "\n").encode())

    # Stage 2: commit and read back --------------------------------------------------

    def _accepted_path(self) -> Path:
        return self.settings.intake_dir / "accepted-paperless-versions.json"

    def _version_accepted(self, live: str) -> bool:
        """The tested version, or one this app already committed under while it was the tested version.

        The second part lets the pin move ahead (a merged Renovate update) before Paperless
        itself is updated, without blocking scans on the version that was tested before.
        """
        try:
            accepted = set(json.loads(self._accepted_path().read_text()))
        except (OSError, json.JSONDecodeError):
            accepted = set()
        if live == self.settings.tested_paperless_version:
            if live not in accepted:
                write_atomic(self._accepted_path(), json.dumps(sorted(accepted | {live})).encode())
            return True
        return live in accepted

    def version_accepted(self, live: str) -> bool:
        """Whether Paperless is at a version the app may write to. The same rule for scans and for stored documents."""
        return self._version_accepted(live)

    def version_label(self) -> str:
        processor = self.settings.processor_version.rsplit("/", 1)[-1]
        return f"Google OCR {processor}, intake {__version__}"[:64]

    def commit(self, item: Item) -> None:
        try:
            live = self.paperless.server_version()
        except PaperlessError as error:
            raise Wait(str(error), "paperless", "commit", waits_on=_on_paperless(error))
        if not self._version_accepted(live):
            raise Wait(f"Paperless is {live}, but the app was tested on {self.settings.tested_paperless_version}",
                       "version_mismatch", "commit", alert=True, waits_on="paperless")
        try:
            # The intake tag marks the upload as the app's own. Paperless's "document added"
            # trigger ignores the source filter (3.3.4 checks sources only for "consumption
            # started"), so Jane's ingest workflow excludes this tag instead.
            tag_ids = [self.paperless.tag_id(self.settings.scanner_tag),
                       self.paperless.tag_id(self.settings.intake_tag)]
        except PaperlessError as error:
            raise Wait(str(error), "paperless", "commit", alert=error.kind == NO_TAG, waits_on=_on_paperless(error))
        if item.status != COMMITTING:
            item.set(COMMITTING)
        mode = item.state.get("mode", WITH_GOOGLE)
        original = item.source_path.read_bytes()
        if sha256(original) != item.sha:
            reason = "the local copy of the scan changed on disk"
            item.set(FAILED, reason=reason, reason_kind="local_integrity", **self._queued(item, "failed", reason))
            self.settle(item)
            return

        try:
            doc_id = self._ensure_original(item, original, tag_ids)
            self._ensure_unowned(item, doc_id)
            if mode == WITH_GOOGLE and not self._ensure_version(item, doc_id):
                # The original is already safely in Paperless. A damaged local copy of the
                # searchable version is a reason to say so, not a reason to hold the scan.
                mode = ORIGINAL_ONLY
                item.state.update(mode=ORIGINAL_ONLY, copy_not_built=True,
                                  no_google_copy_reason="the local searchable copy changed on disk before upload")
                item.save()
            problems, warnings = self._read_back(item, doc_id, tag_ids, mode)
            if item.state.get("no_google_copy_reason"):
                # Not a per-scan nuisance: if this happens, Google's part of the pipeline isn't
                # working, and silence would let every later scan go in without it.
                warnings.insert(0, f"it has no Google text or upright copy, because "
                                   f"{item.state['no_google_copy_reason']}")
            if not problems:
                self._ensure_note(item, doc_id, mode, warnings)
        except PaperlessTaskFailed as error:
            raise Wait(f"Paperless couldn't take the upload ({str(error)}: {error.result[:200]})",
                       "paperless_task", "commit", alert=True)
        except PaperlessError as error:
            raise Wait(str(error), "paperless", "commit", waits_on=_on_paperless(error))

        if problems:
            reason = "; ".join(problems)
            item.set(FAILED, reason=reason, reason_kind="read_back", doc_id=doc_id,
                     **self._queued(item, "failed", reason))
            self.settle(item)
            return
        status = COMMITTED_WARNING if warnings else COMMITTED
        reason = "; ".join(warnings) or None
        # The alert (or the message closing an earlier one) is written with the new state, in the same
        # write, and sent from there. Killed in between, it is still owed, and is sent when the app is back.
        told = self._queued(item, "warning", reason) if warnings else self._resolution(item, "committed")
        item.set(status, reason=reason, reason_kind="read_back" if warnings else None,
                 doc_id=doc_id, committed_at=iso(self.clock()), committed_at_unix=self.clock(),
                 waiting_since_unix=None, alerted_reason_kind=None, waits_on=None, **told)
        self._tidy_after_commit(item)
        self.settle(item)
        # The hand-over. The scan has finished going in. From here its document belongs to the
        # documents loop, which is also what gives it a better searchable version later, if
        # this one reads back badly or couldn't be built. Killed before this, the app writes the
        # record when it starts again (documents.adopt_scans).
        if self.documents is not None:
            self.documents.record_scan(item)

    def _ensure_original(self, item: Item, original: bytes, tag_ids: list[int]) -> int:
        doc_id = item.state.get("doc_id")
        if doc_id:
            return int(doc_id)
        name = item.state["original_name"]
        task_id = item.state.get("upload_task")
        if task_id:
            try:
                task = searchable.await_upload(self.paperless, self.intent, task_id, what="original",
                                               sha=item.sha, name=name)
                doc_id = self.paperless.task_document_id(task)
            except PaperlessTaskFailed as error:
                if not _refused_as_duplicate(error):
                    item.state["upload_task"] = None
                    item.save()
                    raise
        # Only a document the app stored itself is taken to be this scan's: it carries the intake tag.
        # The same file stored some other way is someone else's document. It is never this scan's, and it is
        # the loop's only if it carries the Scanner tag.
        mine = tag_ids[-1]
        doc_id = doc_id or self.paperless.find_by_checksum(item.sha, with_tag=mine)
        if not doc_id:
            created = scan_date(name, float(item.state.get("captured_at_unix") or self.clock()))
            # The intent mark goes down before the file goes out. Killed or timed out before the task id is
            # written, the queue starts no upload until Paperless shows how this one ended (gate.Intent.settle),
            # and then this step finds the document by its checksum instead of sending the file again.
            self.intent.open(what="original", sha256=item.sha, name=name)
            task_id = searchable.send(self.intent, lambda: self.paperless.post_document(name, original, tag_ids=tag_ids,
                                                                                       created=created))
            item.event("uploaded_original", task=task_id)
            item.state["upload_task"] = task_id
            item.save()
            self.intent.task(task_id)
            try:
                task = searchable.await_upload(self.paperless, self.intent, task_id, what="original",
                                               sha=item.sha, name=name)
                doc_id = self.paperless.task_document_id(task)
            except PaperlessTaskFailed as error:
                if not _refused_as_duplicate(error):
                    raise
            doc_id = doc_id or self.paperless.find_by_checksum(item.sha, with_tag=mine)
            if not doc_id:
                raise PaperlessError("Paperless finished the upload but the app can't find its document by checksum "
                                     "(if Paperless is set to refuse duplicates, the same file is already stored "
                                     "as a document the app didn't upload)")
        item.state["doc_id"] = int(doc_id)
        item.save()
        item.event("original_in_paperless", doc_id=int(doc_id))
        return int(doc_id)

    def _ensure_unowned(self, item: Item, doc_id: int) -> None:
        """Leave the document with no owner, like every scan from the consume folder.

        Paperless makes an API upload the uploader's own, and a document with an
        owner is hidden from other users who have no superuser rights or explicit
        permission. Clearing the owner keeps the app's documents visible to the
        same people as before. Only the owner may clear it, and that is this app.
        """
        if self.paperless.document(doc_id).get("owner") is None:
            return
        self.paperless.set_owner(doc_id, None)
        item.event("owner_cleared", doc_id=doc_id)

    def _ensure_version(self, item: Item, doc_id: int) -> bool:
        """Make sure the searchable version is in Paperless. False when the local copy can no longer be trusted."""
        def remember(task):
            if task:
                item.event("uploaded_version", task=task)
            item.state["version_task"] = task
            item.save()

        stem = Path(item.state["original_name"]).stem
        return searchable.ensure_added(self.paperless, doc_id, item.state["searchable_sha256"],
                                       item.searchable_path.read_bytes, f"{stem}.searchable.pdf", self.version_label(),
                                       task=item.state.get("version_task"), remember=remember, intent=self.intent)

    def _read_back(self, item: Item, doc_id: int, tag_ids: list[int], mode: str) -> tuple[list[str], list[str]]:
        """Problems mean the original isn't safely in Paperless. Warnings concern the searchable version only."""
        problems, warnings, record = [], [], {}
        versions = self.paperless.versions(doc_id)
        root = next((v for v in versions if v.is_root), None)
        if root is None or root.checksum != item.sha:
            problems.append("Paperless's checksum for the stored original doesn't match the scan")
        else:
            stored = sha256(self.paperless.download_original(doc_id, root.id))
            record["original_download_sha256"] = stored
            if stored != item.sha:
                problems.append("the original downloaded from Paperless doesn't match the scan byte for byte")
        document = self.paperless.document(doc_id)
        if document.get("owner") is not None:
            warnings.append("the document still has an owner, so other Paperless users may not see it")
        present = set(document.get("tags") or [])
        for name, tag_id in zip((self.settings.scanner_tag, self.settings.intake_tag), tag_ids):
            if tag_id not in present:
                warnings.append(f"the {name} tag is missing")
        if mode == WITH_GOOGLE and not problems:
            google_text = (item.root / "text.txt").read_text(encoding="utf-8")
            found, measured = searchable.read_back(self.paperless, doc_id, item.state["searchable_sha256"], google_text,
                                                   versions=versions, document=document)
            warnings.extend(found)
            record.update(measured)
            item.state["copy_in_paperless"] = "version_id" in measured
            if "text_match" in measured:
                item.state["copy_reads_badly"] = not measured["text_match"]["ok"]
        if mode == ORIGINAL_ONLY:
            # With Paperless's own OCR switched off, a scan that goes in without its copy has no text at all.
            item.state["paperless_read_it"] = bool((document.get("content") or "").strip())
            record["paperless_read_it"] = item.state["paperless_read_it"]
        item.event("read_back", doc_id=doc_id, problems=problems, warnings=warnings, **record)
        return problems, warnings

    def _ensure_note(self, item: Item, doc_id: int, mode: str, warnings: list[str]) -> None:
        if item.state.get("note_added"):
            return
        text = self.note_text(item, mode, warnings)
        # Looked for before it is written: the app may have been cut short just after writing it,
        # and updated since, so the opening is taken without the release's number.
        opening = text.split("paperless-intake", 1)[0]
        if not any(str(note.get("note") or "").startswith(opening) for note in self.paperless.notes(doc_id)):
            self.paperless.add_note(doc_id, text)
        item.state["note_added"] = True
        item.save()

    def note_text(self, item: Item, mode: str, warnings: list[str]) -> str:
        """What was decided for this scan and on what evidence, where Jane reads the document."""
        state = item.state
        pages = state.get("page_count")
        lines = [f"Scanned {state.get('captured_at')} and checked by paperless-intake {__version__}."]
        if mode == WITH_GOOGLE:
            processor = self.settings.processor_version.rsplit("/", 1)[-1]
            lines.append(f"The newest version is the upright, searchable copy (Google OCR {processor}); "
                         f"version 1 is the original scan.")
            order = ORDER_WORDS.get(state.get("order_status") or "", "as scanned")
            lines.append(f"Pages: {pages}. Page order: {order}.")
            turned = sum(1 for turn in state.get("rotations") or [] if turn)
            if turned:
                sentence = f"{turned} of {pages} pages were turned upright in the searchable copy"
                followed = int(state.get("rotations_from_stack") or 0)
                if followed:
                    sentence += (f"; {followed} with too little text to judge alone "
                                 f"{'was' if followed == 1 else 'were'} turned like the rest of the stack")
                lines.append(sentence + ".")
            if state.get("quality_notes"):
                lines.append("Text layer: " + "; ".join(state["quality_notes"]) + ". The page images aren't affected.")
        elif state.get("no_google_copy_reason"):
            # Only a copy the app itself failed to build is tried again, by the next version of the app.
            own = ("Paperless did its own OCR" if state.get("paperless_read_it", True)
                   else "It has no search text until a newer version of the app builds the copy"
                   if state.get("copy_not_built") else "It has no search text")
            lines.append(f"Only the original was stored, because {state['no_google_copy_reason']}. "
                         f"{own}, and the pages are as the scanner saved them.")
        elif state.get("short_text"):
            own = (" and Paperless did its own OCR" if state.get("paperless_read_it", True)
                   else ". Paperless has no search text for it")
            lines.append(f"Google found almost no text, so only the original was stored{own}.")
        else:
            lines.append("Only the original was stored.")
        if state.get("jane_decision") in {"keep", "keep_original"}:      # "keep_original" was 1.0's name for it
            lines.append(f"Kept as it is, by Jane's choice. Unresolved: {state.get('held_reason')}.")
        if warnings:
            lines.append("Read-back warnings: " + "; ".join(warnings) + ".")
        return " ".join(lines)

    def _tidy_after_commit(self, item: Item) -> None:
        """Keep Google's responses without the page images; drop raw responses and scratch."""
        summary = []
        google_root = item.root / "google"
        for response in sorted(google_root.glob("run-*/*/attempt-*/response.json")):
            try:
                body = json.loads(response.read_bytes())
                pages = (body.get("document") or body).get("pages") or []
                for page in pages:
                    image = page.get("image") or {}
                    if "content" in image:
                        image["contentSha256"] = sha256(image.pop("content").encode())
                manifest = json.loads((response.parent / "manifest.json").read_bytes())
                summary.append({"manifest": manifest, "response": body})
            except (OSError, json.JSONDecodeError):
                continue
        if summary:
            data = gzip.compress(json.dumps(summary).encode())
            write_atomic(item.root / "google-summary.json.gz", data)
        shutil.rmtree(google_root, ignore_errors=True)
        shutil.rmtree(item.root / "work", ignore_errors=True)

    # Outcomes ------------------------------------------------------------------------

    # Home Assistant: the watchdog's pair of webhooks, and alerts -------------------------
    #
    # Webhooks can't be taken back or asked about, so each is written down before it is sent.
    # "started" and "finished" drive a counter in Home Assistant (minimum 0) that must stay
    # paired. Each is sent at most once: the app writes that it is sending, sends, and writes the
    # result. Killed in the instant between, the app takes it as sent. A lost "finished" costs a
    # false 45-minute warning about one scan. A second one would hide another scan in flight.
    # Alerts go out at least once. Each is queued in the scan's record (its outbox) in the same
    # write as the change of state it is about, then sent. Killed before it went, it is sent when
    # the app starts again. A repeated alert is a repeated message; a lost one could be a lost page.

    @staticmethod
    def _started(state: dict) -> bool:
        """Whether "started" went out, or may have: written as being sent, with no result after it."""
        return bool(state.get("started_sent")) or (bool(state.get("started_sending"))
                                                    and state.get("started_sent") is None)

    def announce(self, item: Item) -> None:
        """Tell the watchdog a scan has started. Never twice for one scan."""
        state = item.state
        if state.get("started_sending") or state.get("started_sent") is not None:
            return
        if not self.notifier.has("started"):
            state["started_sent"] = False
            item.save()
            return
        state.update(started_sending=True, started_at_unix=self.clock())
        item.save()
        state["started_sent"] = self.notifier.started(item.sha)
        item.save()

    def settle(self, item: Item) -> None:
        """Send what is owed to Home Assistant about this scan: "finished" once it has ended, and its queued alerts.

        Safe to call at any time and as often as wanted. The app calls it after every change of
        state, and for every scan when it starts.
        """
        state = item.state
        if item.status not in ENDED and state.get("started_sent") is None and not state.get("started_sending"):
            self.announce(item)                         # its claim failed before "started" could go out
        if state.get("finished_sending") and not state.get("finished_sent"):
            state["finished_sent"] = True               # killed while sending: taken as sent
            item.save()
        if item.status in ENDED and self._started(state) and not state.get("finished_sent"):
            started_at = float(state.get("started_at_unix") or state.get("captured_at_unix") or 0)
            if (self.clock() - started_at) / 60 <= self.settings.watchdog_minutes:
                state["finished_sending"] = True
                item.save()
                self.notifier.finished(item.sha)
            else:
                item.event("finished_not_sent", reason="the Home Assistant watchdog already reported this scan")
            state["finished_sent"] = True
            item.save()
        self._flush(item)

    def _flush(self, item: Item) -> None:
        outbox = list(item.state.get("outbox") or [])
        if not outbox:
            return
        if not self.notifier.has("alert"):
            item.state["outbox"] = []
            item.save()
            return
        while outbox:
            entry = outbox[0]
            if not self.notifier.alert(kind=entry["kind"], item_id=item.sha[:12], scanned_at=self._scanned_at(item),
                                       pages=item.state.get("page_count"), gap=entry.get("gap") or ""):
                return                                  # Home Assistant didn't take it: sent again later
            outbox.pop(0)
            item.state["outbox"] = outbox
            item.save()

    @staticmethod
    def _queued(item: Item, kind: str, gap: str | None) -> dict:
        """The fields that queue an alert. Written together with the change of state it is about."""
        return {"outbox": [*(item.state.get("outbox") or []), {"kind": kind, "gap": gap or ""}],
                "alert_open": kind != "resolved"}

    def _resolution(self, item: Item, outcome: str) -> dict:
        """The fields that close an earlier alert about this scan, if one is open."""
        return self._queued(item, "resolved", outcome) if item.state.get("alert_open") else {}

    def close_alert(self, item: Item) -> None:
        """For documents.py: the cause of an earlier alert about this scan's document is gone."""
        told = self._resolution(item, "committed")
        if told:
            item.state.update(told)
            item.save()
        self._flush(item)

    @staticmethod
    def _scanned_at(item: Item) -> str:
        return item.state.get("captured_at") or ""

    def _hold(self, item: Item, reason: str, kind: str) -> None:
        item.set(HELD, reason=reason, reason_kind=kind, held_reason=reason, held_at=iso(self.clock()),
                 waiting_since_unix=None, waits_on=None, **self._queued(item, "held", reason))
        self.settle(item)

    def _wait(self, item: Item, wait: Wait, *, retry_at: float | None = None) -> None:
        since = float(item.state.get("waiting_since_unix") or self.clock())
        if self.clock() - since > self.settings.retry_hours * 3600:
            self._hold(item, f"kept failing for {self.settings.retry_hours} hours: {wait.reason}", "gave_up")
            return
        already_alerted = item.state.get("alerted_reason_kind") == wait.kind
        retry_at = retry_at if retry_at is not None else self.clock() + self.settings.retry_minutes * 60
        told = {}
        if wait.alert and not already_alerted:
            told = dict(self._queued(item, "waiting", wait.reason), alerted_reason_kind=wait.kind)
        # A wait the queue's gate records keeps what Google's last failure asked for: a fresh run.
        fresh_google = wait.kind in {"google", "auth"} or (wait.kind in GATE_KINDS
                                                           and bool(item.state.get("retry_new_google_run")))
        item.set(WAITING, reason=wait.reason, reason_kind=wait.kind, retry_stage=wait.stage,
                 retry_new_google_run=fresh_google, waits_on=wait.waits_on,
                 waiting_since_unix=since, next_attempt_at_unix=retry_at, next_attempt_at=iso(retry_at), **told)
        if told:
            self.settle(item)

    def held_back(self, item: Item, reason: str, kind: str, waits_on: str | None, until: float) -> None:
        """The queue's gate kept this scan from starting (memory, disk, I/O, an upload in doubt).

        Recorded as a wait, not run: its next try, the 24-hour hold, and the Scans page stay true.
        No alert of its own. If it lasts a day, the hold alerts.
        """
        if item.status == WAITING:
            stage = item.state.get("retry_stage", "ocr")
        else:
            stage = "commit" if item.status in {READY, COMMITTING} else "ocr"
        self._wait(item, Wait(reason, kind, stage, waits_on=waits_on), retry_at=until)

    def worker_stopped(self, item: Item, code: int, *, timed_out: bool = False) -> None:
        """The child process for this item died without finishing. Wait and retry, never loop."""
        if item.status not in {CAPTURED, READY, COMMITTING, WAITING}:
            log.warning("worker for %s exited with %s; item is already %s", item.sha[:12], code, item.status)
            return
        if timed_out:
            reason = "the work on this scan took longer than 4 hours and was stopped"
        elif code < 0:
            reason = f"the work on this scan was stopped by signal {-code}"
            if -code == 9:
                reason += ", most likely for running out of memory"
        else:
            reason = f"the work on this scan stopped with exit code {code}"
        if item.status == WAITING:          # it died before it could even pick the item up
            stage = item.state.get("retry_stage", "ocr")
        else:
            stage = "commit" if item.status in {READY, COMMITTING} else "ocr"
        self._wait(item, Wait(reason, "worker", stage, alert=True))

    # Jane's choices on the Scans page ---------------------------------------------------

    def keep_as_it_is(self, item: Item) -> None:
        """Jane has judged the paper can go with this scan as it stands. Commit what there is.

        A scan held for a missing page already has its searchable copy, so both
        versions go in. A scan held before any copy could be made goes in as the original only.
        """
        if item.status != HELD:
            raise ValueError("only held scans can be kept as they are")
        has_copy = item.searchable_path.exists() and item.state.get("mode") == WITH_GOOGLE
        item.set(READY, mode=WITH_GOOGLE if has_copy else ORIGINAL_ONLY, jane_decision="keep")

    def requeue_obsolete_holds(self) -> list[str]:
        """Put scans held for a reason this version no longer holds for back through the checks.

        Version 1.0 held scans over the searchable copy. Their originals have been
        waiting outside Paperless since. Google's saved answer is reused, so
        nothing is sent to Google again unless that answer is gone.
        """
        requeued = []
        for item in self.store.all():
            if item.status == HELD and item.state.get("reason_kind") in OBSOLETE_HOLD_KINDS:
                item.event("requeued", by=__version__, was_held_for=item.state.get("reason"))
                item.set(CAPTURED, requeued_by=__version__, waiting_since_unix=None, alerted_reason_kind=None)
                requeued.append(item.sha)
                log.info("%s was held by an earlier version (%s); checking it again", item.sha[:12],
                         item.state.get("held_reason"))
        return requeued

    def reprepare_unfinished(self) -> list[str]:
        """When a new release starts, put scans that haven't reached Paperless yet back through the checks.

        They get the newer handling, and Google's saved answer is reused. A scan whose
        original has already been handed to Paperless is not prepared again: it finishes
        going in with the searchable copy it has, and the documents loop improves that
        later if it needs it.
        """
        again = []
        for item in self.store.all():
            state = item.state
            if state.get("prepared_by") == __version__:      # none recorded: a release too old to say, so not this one
                continue
            in_flight = item.status in {READY, COMMITTING} or (item.status == WAITING
                                                                and state.get("retry_stage") == "commit")
            if not in_flight or state.get("doc_id") or state.get("upload_task"):
                continue
            earlier = str(state.get("prepared_by") or "earlier-release")
            for name in ("checks.json", "google-summary.json.gz"):      # keep what the earlier try recorded
                path = item.root / name
                kept = item.root / f"{name.split('.')[0]}.by-{earlier}.{name.split('.', 1)[1]}"
                if path.exists() and not kept.exists():
                    copy_atomic(path, kept)
            item.event("prepared_again", by=__version__, was_by=earlier)
            item.set(CAPTURED, waiting_since_unix=None, alerted_reason_kind=None)
            again.append(item.sha)
            log.info("%s was prepared by release %s and hasn't reached Paperless; preparing it again",
                     item.sha[:12], earlier)
        return again

    def settle_old_states(self) -> list[str]:
        """Scan records an earlier release left part-way through re-doing a stored document's copy.

        Releases before 1.3.0 re-did a stored document's searchable copy by moving its scan
        record back to "captured" with a snapshot to restore. That is the documents loop's job
        now. Any record still in that state is put back as it was when it was committed.
        """
        settled = []
        for item in self.store.all():
            state = item.state
            if not (state.get("copy_added_later") or state.get("copy_retry_restore")) or not state.get("doc_id"):
                continue
            was = dict(state.get("copy_retry_restore") or {})
            status = was.pop("status", None) or COMMITTED_WARNING
            was.setdefault("mode", ORIGINAL_ONLY)
            was.setdefault("copy_not_built", True)
            item.event("old_state_settled", by=__version__, status=status)
            item.set(status, **was, copy_added_later=False, copy_note_needed=False, copy_retry_restore=None,
                     copy_retry_previous_reason=None, waiting_since_unix=None, alerted_reason_kind=None)
            settled.append(item.sha)
        return settled

    def can_retry(self, item: Item) -> bool:
        return item.status == WAITING or (item.status == HELD and item.state.get("reason_kind") == "gave_up")

    def retry(self, item: Item) -> None:
        if not self.can_retry(item):
            raise ValueError("only waiting scans, or scans held after 24 hours of retries, can be retried")
        stage = item.state.get("retry_stage", "ocr")
        item.set(WAITING, reason="retry requested", reason_kind="manual", retry_stage=stage,
                 retry_new_google_run=stage == "ocr", waiting_since_unix=self.clock(),
                 next_attempt_at_unix=0, alerted_reason_kind=None, jane_decision="retry")

    def discard(self, item: Item) -> Path:
        if item.status not in {HELD, WAITING}:
            raise ValueError("only held or waiting scans can be discarded")
        item.set(DISCARDED, reason=item.state.get("reason"), reason_kind=item.state.get("reason_kind"),
                 jane_decision="discard", **self._resolution(item, "discarded"))
        self.settle(item)
        item.event("discarded")
        return self._move_discarded(item)

    def _move_discarded(self, item: Item) -> Path:
        target = self.settings.discarded_dir / f"{item.sha}-{int(self.clock())}"
        target.parent.mkdir(parents=True, exist_ok=True)
        item.root.rename(target)
        return target

    # Starting again after a kill ---------------------------------------------------------

    def recover(self) -> list[str]:
        """When the app starts: finish what a kill left owed for every scan. Safe to run any number of times."""
        done = []
        self.left_to_tidy = []
        for item in self.store.all():
            state = item.state
            if state.get("started_sending") and state.get("started_sent") is None:
                state["started_sent"] = True             # killed while sending "started": taken as sent
                item.save()
            if item.status not in ENDED and state.get("started_sent") is None and not state.get("started_sending"):
                self.announce(item)                      # claimed, then killed before "started" went out
                done.append(f"sent the started webhook for {item.sha[:12]}")
            owed = bool(state.get("outbox")) or (item.status in ENDED and self._started(state)
                                                 and not state.get("finished_sent"))
            self.settle(item)
            if owed:
                done.append(f"sent what was owed to Home Assistant for {item.sha[:12]}")
            if item.status in {COMMITTED, COMMITTED_WARNING} and (item.root / "google").exists():
                if (item.root / "google-summary.json.gz").exists():
                    shutil.rmtree(item.root / "google", ignore_errors=True)       # nothing in them is needed
                    shutil.rmtree(item.root / "work", ignore_errors=True)
                else:
                    # The summary is made from the raw answers, page images and all. That is a worker's job:
                    # the service never loads large files (Service.start runs it).
                    self.left_to_tidy.append(item.sha)
            if item.status == DISCARDED:
                self._move_discarded(item)               # discarded, then killed before it was moved aside
                done.append(f"moved discarded {item.sha[:12]} aside")
        return done
