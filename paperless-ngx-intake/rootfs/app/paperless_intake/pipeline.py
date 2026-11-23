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

The app never deletes or edits anything in Paperless except the document it
just created. Its tags are set at upload; after that it clears the owner Paperless
gives an API upload, and adds a version and a note.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import logging
import shutil
import time
from dataclasses import dataclass
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
from .notify import Notifier
from .paperless import Paperless, PaperlessError, PaperlessTaskFailed
from .store import (COMMITTED, COMMITTED_WARNING, COMMITTING, CAPTURED, DISCARDED, FAILED, HELD, READY, WAITING,
                    Item, Store,
                    iso, now, write_atomic)

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
COPY_RESAVED = "Paperless made its own archive file of the searchable copy and shows that in its place"
# The same warning as versions before 1.2.0 worded it, so a document that already carries it isn't alerted again.
_EARLIER_WORDING = {"Paperless re-ran its own OCR on the searchable copy": COPY_RESAVED}


def _kinds(reason: str | None) -> set[str]:
    """The warnings in a reason, without the figures in brackets: what they are about, not how much."""
    kinds = {re.sub(r"\s*\(.*?\)", "", part).strip() for part in (reason or "").split("; ") if part.strip()}
    return {_EARLIER_WORDING.get(kind, kind) for kind in kinds}


COPY_RETRIES = 3            # tries per app version to add a copy an earlier version couldn't build
# What the app records about the copy that is stored in Paperless. Put back if a later try at a better copy fails.
RESTORED = ("reason", "reason_kind", "no_google_copy_reason", "mode", "copy_not_built", "copy_reads_badly",
            "searchable_sha256", "page_order", "rotations", "order_status", "order_verified", "quality_notes",
            "short_text")
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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Wait(Exception):
    """Something outside was unavailable. Try again later."""
    reason: str
    kind: str
    stage: str
    alert: bool = False


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
                item.set(CAPTURED if stage == "ocr" else READY, reason=None, reason_kind=None)
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

    def _usage_path(self) -> Path:
        return self.settings.intake_dir / "google-usage.json"

    def _pages_today(self) -> int:
        try:
            usage = json.loads(self._usage_path().read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        return int(usage.get(time.strftime("%Y-%m-%d", time.gmtime(self.clock())), 0))

    def _count_pages(self, pages: int) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        try:
            usage = json.loads(self._usage_path().read_text())
        except (OSError, json.JSONDecodeError):
            usage = {}
        usage = {day: int(usage.get(day, 0)) + pages}
        write_atomic(self._usage_path(), json.dumps(usage).encode())

    def prepare(self, item: Item) -> None:
        free_mib = shutil.disk_usage(self.settings.intake_dir).free // (1024 * 1024)
        if free_mib < self.settings.min_free_mib:
            raise Wait(f"only {free_mib} MiB free on the Green", "disk_low", "ocr")
        try:
            source = inspect_source(item.source_path)
        except ParserError as error:
            raise Hold(f"this isn't a PDF the app can read ({error})", "unsupported_file")
        if item.state.get("page_count") != source.page_count:
            item.state["page_count"] = source.page_count
            item.save()
        if self.token_provider is None and self.backend_factory == self._google_backend:
            raise Wait("the Google key isn't set in the app options", "config", "ocr", alert=True)
        if self._pages_today() + source.page_count > self.settings.daily_page_cap:
            raise Wait(f"today's Google page cap ({self.settings.daily_page_cap}) is reached", "page_cap", "ocr")

        item.event("ocr_started", google_run=item.state.get("google_run", 1))
        work_dir = item.new_work_dir()
        try:
            # Building the backend checks the app's Google settings, and a bad setting
            # must be reported the same way as Google refusing a request.
            result = self.backend_factory(item).process(source, work_dir)
        except GoogleBackendError as error:
            if error.code in TRANSIENT_GOOGLE:
                raise Wait(f"Google OCR didn't answer ({error.code})", "google", "ocr")
            if error.code in AUTH_GOOGLE:
                raise Wait(f"Google refused the app's key ({error.code})", "auth", "ocr", alert=True)
            status = getattr(error, "http_status", None)
            if error.code in THIS_SCAN_GOOGLE or (error.code == "post_http_error_fatal" and status in THIS_SCAN_HTTP):
                detail = f"{error.code}, HTTP {status}" if status else error.code
                self._ready_without_google(item, f"Google couldn't read this scan ({detail})")
                return
            detail = f"{error.code}, HTTP {status}" if status else error.code
            raise Wait(f"Google OCR isn't working ({detail})", "google", "ocr", alert=True)
        cache_hits = int(result.provenance.get("cache_hits", 0))
        if cache_hits < len(result.chunks):
            self._count_pages(source.page_count)

        try:
            validate_result(result, source)
            document = document_for_renderer(result, source)
        except ParserError as error:
            self._ready_without_google(item, f"Google's answer didn't match the scan's pages ({error})", ours=True)
            return

        plan = self._plan(item, document)
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
            log.warning("%s: no searchable copy: %s", item.sha[:12], error, exc_info=not expected)
            try:
                shape = describe_source(source.path)
            except Exception as failure:
                shape = f"couldn't be read ({type(failure).__name__})"
            record["source_shape"] = shape
            log.warning("%s: the scan's pages as the scanner wrote them: %s", item.sha[:12], shape)
            why = str(error) if expected else f"an unexpected {type(error).__name__} in the app"
            self._ready_without_google(item, f"the searchable copy couldn't be built ({why})",
                                       checks_record={**record, "render_error": f"{type(error).__name__}: {error}"},
                                       gap=gap, ours=True)
            return
        assert_unchanged(source)
        order = tuple(rendered.page_order)
        text = ordered_text(result, source, order)
        searchable = rendered.archive_path.read_bytes()
        write_atomic(item.searchable_path, searchable)
        write_atomic(item.root / "text.txt", text.encode("utf-8"))

        mode = ORIGINAL_ONLY if checks.short_text(text) else WITH_GOOGLE
        provenance = rendered.provenance
        decision = provenance.get("page_order_decision", {})
        rotations = provenance.get("orientation_decisions") or []
        notes = list(provenance.get("quality_notes") or [])
        self._write_checks(item, {
            **record,
            "render_checks": provenance.get("checks"),
            "render_pages": provenance.get("pages"),
            "orientation_decisions": rotations,
            "page_order_decision": decision,
            "quality_notes": notes,
            "text_sha256": sha256(text.encode("utf-8")),
            "searchable_sha256": sha256(searchable),
            "mode": mode,
        })
        log.info("%s: order %s (forward votes %s, reverse votes %s); %s", item.sha[:12],
                 decision.get("status"), decision.get("forwardVotes"), decision.get("reverseVotes"),
                 "; ".join(notes) or "text layer placed without remarks")
        log.info("%s: pages as scanned: %s", item.sha[:12], self._page_summary(provenance))
        fields = dict(mode=mode, page_order=list(order), order_status=decision.get("status"),
                      rotations=[r.get("appliedCardinalClockwiseDegrees", 0) for r in rotations],
                      rotations_from_stack=sum(1 for r in rotations if r.get("followedStack")),
                      quality_notes=notes, no_google_copy_reason=None, copy_not_built=False,
                      prepared_by=__version__,
                      searchable_sha256=sha256(searchable), short_text=mode == ORIGINAL_ONLY,
                      waiting_since_unix=None, alerted_reason_kind=None)
        if gap and not self._settled(item):
            # The searchable copy is ready, so "keep as it is" can commit both versions.
            item.state.update(fields)
            item.save()
            raise Hold(gap, "missing_page")
        item.set(READY, **fields)

    @staticmethod
    def _plan(item: Item, document: dict):
        """The printed page numbers, read as evidence. A failure to read them is no evidence, not a stop."""
        try:
            return plan_page_order(document)
        except Exception as error:
            log.warning("%s: page numbers couldn't be read as evidence", item.sha[:12], exc_info=True)
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
        A later version of the app may do better, so it tries again (see retry_copies).
        """
        self._write_checks(item, {**(checks_record or {}), "mode": ORIGINAL_ONLY, "no_google_copy_reason": why})
        if item.state.get("copy_added_later"):
            # The document is already in Paperless, with or without an earlier copy. This try
            # made nothing to add, so nothing about the document or its record changes.
            self._back_to_committed(item, why)
            return
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

        It is closed once Jane has chosen to keep the scan, or once the scan is in Paperless.
        Only a scan being prepared again (see retry_copies) can get here in that state, and
        holding it a second time would ask Jane what she has already answered.
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

    def version_label(self) -> str:
        processor = self.settings.processor_version.rsplit("/", 1)[-1]
        return f"Google OCR {processor}, intake {__version__}"[:64]

    def commit(self, item: Item) -> None:
        try:
            live = self.paperless.server_version()
        except PaperlessError as error:
            raise Wait(str(error), "paperless", "commit")
        if not self._version_accepted(live):
            raise Wait(f"Paperless is {live}, but the app was tested on {self.settings.tested_paperless_version}",
                       "version_mismatch", "commit", alert=True)
        try:
            # The intake tag marks the upload as the app's own. Paperless's "document added"
            # trigger ignores the source filter (3.3.4 checks sources only for "consumption
            # started"), so Jane's ingest workflow excludes this tag instead.
            tag_ids = [self.paperless.tag_id(self.settings.physical_mail_tag),
                       self.paperless.tag_id(self.settings.intake_tag)]
        except PaperlessError as error:
            raise Wait(str(error), "paperless", "commit", alert="tag" in str(error))
        if item.status != COMMITTING:
            item.set(COMMITTING)
        mode = item.state.get("mode", WITH_GOOGLE)
        # A document that has been in Paperless for a while and is only getting its copy now.
        # Jane may have changed it since. The app adds the version and a note, and judges
        # and changes nothing else about it.
        later = bool(item.state.get("copy_added_later"))
        original = item.source_path.read_bytes()
        if sha256(original) != item.sha:
            if later:
                self._back_to_committed(item, "the local copy of the scan changed on disk")
                return
            item.set(FAILED, reason="the local copy of the scan changed on disk", reason_kind="local_integrity")
            self._finish(item, alert_kind="failed")
            return

        try:
            doc_id = self._ensure_original(item, original, tag_ids)
            if not later:
                self._ensure_unowned(item, doc_id)
            if mode == WITH_GOOGLE and not self._ensure_version(item, doc_id):
                # The original is already safely in Paperless. A damaged local copy of the
                # searchable version is a reason to say so, not a reason to hold the scan.
                mode = ORIGINAL_ONLY
                item.state.update(mode=ORIGINAL_ONLY,
                                  no_google_copy_reason="the local searchable copy changed on disk before upload")
                item.save()
            problems, warnings = self._read_back(item, doc_id, tag_ids, mode, later=later)
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
            raise Wait(str(error), "paperless", "commit")

        if problems:
            if later:
                self._back_to_committed(item, "; ".join(problems))
                return
            item.set(FAILED, reason="; ".join(problems), reason_kind="read_back", doc_id=doc_id)
            self._finish(item, alert_kind="failed")
            return
        status = COMMITTED_WARNING if warnings else COMMITTED
        reason = "; ".join(warnings) or None
        # A warning Jane already has about this document isn't sent again because a number in it changed.
        said_before = bool(warnings) and _kinds(reason) <= _kinds(item.state.get("copy_retry_previous_reason"))
        item.set(status, reason=reason, reason_kind="read_back" if warnings else None,
                 doc_id=doc_id, committed_at=iso(self.clock()), committed_at_unix=self.clock(),
                 waiting_since_unix=None, alerted_reason_kind=None, copy_retry_previous_reason=None,
                 copy_retry_restore=None, copy_added_later=False, copy_note_needed=False)
        self._tidy_after_commit(item)
        self._finish(item, alert_kind="warning" if warnings and not said_before else None)

    def _ensure_original(self, item: Item, original: bytes, tag_ids: list[int]) -> int:
        doc_id = item.state.get("doc_id")
        if doc_id:
            return int(doc_id)
        task_id = item.state.get("upload_task")
        if task_id:
            try:
                task = self.paperless.wait_for_task(task_id)
                doc_id = self.paperless.task_document_id(task)
            except PaperlessTaskFailed as error:
                if "duplicate" not in error.result.lower():
                    item.state["upload_task"] = None
                    item.save()
                    raise
        doc_id = doc_id or self.paperless.find_by_checksum(item.sha)
        if not doc_id:
            created = scan_date(item.state["original_name"], float(item.state.get("captured_at_unix") or self.clock()))
            task_id = self.paperless.post_document(item.state["original_name"], original, tag_ids=tag_ids,
                                                   created=created)
            item.event("uploaded_original", task=task_id)
            item.state["upload_task"] = task_id
            item.save()
            try:
                task = self.paperless.wait_for_task(task_id)
                doc_id = self.paperless.task_document_id(task)
            except PaperlessTaskFailed as error:
                if "duplicate" not in error.result.lower():
                    raise
            doc_id = doc_id or self.paperless.find_by_checksum(item.sha)
            if not doc_id:
                raise PaperlessError("Paperless finished the upload but the document can't be found by checksum")
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
        """Make sure version 2 is in Paperless. False when the local copy can no longer be trusted."""
        wanted = item.state["searchable_sha256"]
        if any(v.checksum == wanted for v in self.paperless.versions(doc_id)):
            return True
        task_id = item.state.get("version_task")
        if task_id:
            try:
                self.paperless.wait_for_task(task_id)
            except PaperlessTaskFailed:
                item.state["version_task"] = None
                item.save()
            if any(v.checksum == wanted for v in self.paperless.versions(doc_id)):
                return True
        try:
            data = item.searchable_path.read_bytes()
        except OSError:
            return False
        if sha256(data) != wanted:
            return False
        stem = Path(item.state["original_name"]).stem
        task_id = self.paperless.update_version(doc_id, f"{stem}.searchable.pdf", data, label=self.version_label())
        item.event("uploaded_version", task=task_id)
        item.state["version_task"] = task_id
        item.save()
        self.paperless.wait_for_task(task_id)
        return True

    def _read_back(self, item: Item, doc_id: int, tag_ids: list[int], mode: str, *,
                   later: bool = False) -> tuple[list[str], list[str]]:
        """Problems mean the original isn't safely in Paperless. Warnings concern version 2 only.

        later: the copy is being added to a document that was stored some time ago. Its owner
        and tags are Jane's to have changed since, so they aren't checked.
        """
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
        if document.get("owner") is not None and not later:
            warnings.append("the document still has an owner, so other Paperless users may not see it")
        present = set(document.get("tags") or [])
        for name, tag_id in zip((self.settings.physical_mail_tag, self.settings.intake_tag), tag_ids):
            if tag_id not in present and not later:
                warnings.append(f"the {name} tag is missing")
        if mode == WITH_GOOGLE and not problems:
            wanted = item.state["searchable_sha256"]
            version = next((v for v in versions if v.checksum == wanted and not v.is_root), None)
            if version is None:
                warnings.append("the searchable copy isn't among the document's versions")
            else:
                if versions[0].id != version.id:
                    warnings.append("the searchable copy isn't the newest version, so search may not use Google's text")
                stored = sha256(self.paperless.download_original(doc_id, version.id))
                record["version_download_sha256"] = stored
                if stored != wanted:
                    warnings.append("the searchable copy in Paperless doesn't match byte for byte")
                metadata = self.paperless.metadata(doc_id, version.id)
                record["version_has_archive"] = metadata.get("has_archive_version")
                if metadata.get("has_archive_version"):
                    # Paperless leaves a PDF that has text alone only while its OCR mode is auto or
                    # off and its archive file generation is auto or never. This is how the app
                    # notices that one of them changed: by what Paperless did, not by what it is set to.
                    warnings.append(f"{COPY_RESAVED} (see Paperless's OCR mode and archive file generation settings)")
                google_text = (item.root / "text.txt").read_text(encoding="utf-8")
                match = checks.text_matches(google_text, document.get("content") or "")
                record["text_match"] = match
                item.state["copy_reads_badly"] = not match["ok"]
                if not match["ok"]:
                    warnings.append(f"Paperless's search text differs from Google's "
                                    f"({match['missing_words']} of {match['google_words']} words missing)")
                record["version_id"] = version.id
        if mode == ORIGINAL_ONLY:
            # With Paperless's own OCR switched off, a scan that goes in without its copy has no text at all.
            item.state["paperless_read_it"] = bool((document.get("content") or "").strip())
            record["paperless_read_it"] = item.state["paperless_read_it"]
        item.event("read_back", doc_id=doc_id, problems=problems, warnings=warnings, **record)
        return problems, warnings

    def _ensure_note(self, item: Item, doc_id: int, mode: str, warnings: list[str]) -> None:
        # A copy added to a document that already has its first note gets one more note, once.
        later = bool(item.state.get("copy_note_needed")) and mode == WITH_GOOGLE
        key = "copy_note_added" if later else "note_added"
        if item.state.get(key):
            return
        self.paperless.add_note(doc_id, self.note_text(item, mode, warnings))
        item.state[key] = True
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
        if state.get("copy_note_needed") and mode == WITH_GOOGLE:
            if state.get("copy_note_kind") == "rebuilt":
                lines.append("The searchable copy was built again by a newer version of the app. An earlier note here "
                             "describes the copy it replaced, which is still in the version history.")
            else:
                lines.append("The searchable copy was added after the document was first stored. An earlier note here "
                             "says only the original was stored; that was true then.")
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

    def _finish(self, item: Item, *, alert_kind: str | None) -> None:
        """The scan left the in-flight count: tell the watchdog, unless it has already given up."""
        if item.state.get("started_sent") and not item.state.get("finished_sent"):
            age_minutes = (self.clock() - float(item.state.get("captured_at_unix", 0))) / 60
            if age_minutes <= self.settings.watchdog_minutes:
                self.notifier.finished(item.sha)
            else:
                item.event("finished_not_sent", reason="the Home Assistant watchdog already reported this scan")
            item.state["finished_sent"] = True
            item.save()
        if alert_kind:
            self._alert(item, alert_kind, item.state.get("reason") or "")
        elif item.status in {COMMITTED, DISCARDED}:
            self._resolve(item)

    def _alert(self, item: Item, kind: str, gap: str) -> None:
        self.notifier.alert(kind=kind, item_id=item.sha[:12], scanned_at=self._scanned_at(item),
                            pages=item.state.get("page_count"), gap=gap)
        item.state["alert_open"] = True
        item.save()

    def _resolve(self, item: Item) -> None:
        """Close an earlier alert about this scan, so Home Assistant can clear its notification."""
        if not item.state.get("alert_open"):
            return
        outcome = "discarded" if item.status == DISCARDED else "committed"
        self.notifier.alert(kind="resolved", item_id=item.sha[:12], scanned_at=self._scanned_at(item),
                            pages=item.state.get("page_count"), gap=outcome)
        item.state["alert_open"] = False
        item.save()

    @staticmethod
    def _scanned_at(item: Item) -> str:
        return item.state.get("captured_at") or ""

    def _hold(self, item: Item, reason: str, kind: str) -> None:
        if item.state.get("copy_added_later"):
            self._back_to_committed(item, reason)       # it is in Paperless; there is no paper to keep
            return
        item.set(HELD, reason=reason, reason_kind=kind, held_reason=reason, held_at=iso(self.clock()))
        self._finish(item, alert_kind="held")

    def _back_to_committed(self, item: Item, why: str, *, again: bool = False) -> None:
        """A document already in Paperless couldn't be given its copy this time. Leave it as it was.

        Adding the copy later is a favour to a document that is already safe. Whatever
        goes wrong while trying must not turn that document into a held or waiting scan,
        and must not tell Jane anything: she was told when it first went in without a copy.

        again: the cause was outside the scan (Google or Paperless unreachable, disk low), so
        the next start of this same version may try once more, up to COPY_RETRIES times.
        """
        was = item.state.get("copy_retry_restore") or {}
        item.event("copy_retry_stopped", reason=why, again=again)
        log.warning("%s: its searchable copy couldn't be added this time (%s); the document stays as it was",
                    item.sha[:12], why)
        kept = {key: was[key] for key in RESTORED if key in was}        # what described the copy that is stored
        kept.setdefault("mode", ORIGINAL_ONLY)
        kept.setdefault("copy_not_built", True)
        item.set(was.get("status") or COMMITTED_WARNING, **kept,
                 copy_added_later=False, copy_note_needed=False, copy_retry_previous_reason=None,
                 copy_retry_restore=None,
                 copy_retry_by=None if again else __version__,
                 prepared_by=None if again else item.state.get("prepared_by"),
                 # A new Google run for the next try, as a waiting scan gets: this run's retry budget is spent.
                 google_run=int(item.state.get("google_run", 1)) + (1 if again else 0),
                 waiting_since_unix=None, alerted_reason_kind=None)

    def _wait(self, item: Item, wait: Wait) -> None:
        if item.state.get("copy_added_later"):
            # Nothing is waiting to get into Paperless here, at either stage. See _back_to_committed.
            # An unexpected error while committing may have come after the new copy was uploaded.
            # Trying again at the next start finishes the job: the same copy is found, not uploaded twice.
            self._back_to_committed(item, wait.reason, again=wait.kind != "unexpected" or wait.stage == "commit")
            return
        since = float(item.state.get("waiting_since_unix") or self.clock())
        if self.clock() - since > self.settings.retry_hours * 3600:
            item.state["waiting_since_unix"] = None
            self._hold(item, f"kept failing for {self.settings.retry_hours} hours: {wait.reason}", "gave_up")
            return
        already_alerted = item.state.get("alerted_reason_kind") == wait.kind
        item.set(WAITING, reason=wait.reason, reason_kind=wait.kind, retry_stage=wait.stage,
                 retry_new_google_run=wait.kind in {"google", "auth"},
                 waiting_since_unix=since,
                 next_attempt_at_unix=self.clock() + self.settings.retry_minutes * 60,
                 next_attempt_at=iso(self.clock() + self.settings.retry_minutes * 60))
        if wait.alert and not already_alerted:
            item.state["alerted_reason_kind"] = wait.kind
            item.save()
            self._alert(item, "waiting", wait.reason)

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

    # Jane's choices on the Mail intake page ---------------------------------------------

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

    def retry_copies(self) -> list[str]:
        """Prepare again what an earlier version of the app prepared, where that can help.

        Two cases. A scan that hasn't finished going into Paperless is prepared again by
        every new version. A scan already in Paperless is prepared again only if the
        earlier version couldn't build its searchable copy.

        The original scan is the evidence and the copy is made from it, so the copy can be
        made again for as long as the local scan is kept. A scan still on its way into
        Paperless goes back one step and carries on from there. A scan already in Paperless
        gets the copy added as a new version of the same document, with a note.

        Each version of the app tries once. When the try is cut short by something outside
        the scan (Google or Paperless unreachable), the next start tries again, COPY_RETRIES
        times at most. Only failures in the app's own handling are retried, not scans Google
        itself refused.
        """
        retried = []
        for item in self.store.all():
            state = item.state
            if __version__ in {state.get("prepared_by"), state.get("copy_retry_by")}:
                continue
            in_flight = item.status in {READY, COMMITTING} or (item.status == WAITING
                                                                and state.get("retry_stage") == "commit")
            ours = state.get("copy_not_built") or str(state.get("no_google_copy_reason") or "").startswith(
                "the searchable copy couldn't be built")            # 1.1.0 recorded only the sentence
            # 1.1.2 recorded a copy that read back badly only as the sentence in its warning.
            reads_badly = bool(state.get("copy_reads_badly")) or "search text differs" in str(state.get("reason") or "")
            # A scan that hasn't finished going in is always prepared again by a new version, so it
            # gets the newer handling. Google's saved answer is reused when there is one. A scan that
            # is already in Paperless is touched only when the fault was the app's: it has no copy,
            # or it has one that Paperless couldn't read all the words of.
            if not in_flight and not reads_badly and (state.get("mode") != ORIGINAL_ONLY or not ours):
                continue
            tries = state.get("copy_retries") or {}
            count = int(tries.get("count", 0)) if tries.get("version") == __version__ else 0
            if count >= COPY_RETRIES:
                continue
            committed = item.status in {COMMITTED, COMMITTED_WARNING} and bool(state.get("doc_id"))
            if not (in_flight or committed) or not item.source_path.exists():
                continue
            item.event("copy_retry", by=__version__, was=state.get("no_google_copy_reason"))
            restore = {key: state.get(key) for key in RESTORED}
            # Keep what the earlier try recorded: the new try writes these files again.
            earlier = str(state.get("prepared_by") or "earlier-version")
            for name in ("checks.json", "google-summary.json.gz"):
                path = item.root / name
                kept = item.root / f"{name.split('.')[0]}.by-{earlier}.{name.split('.', 1)[1]}"
                if path.exists() and not kept.exists():
                    shutil.copy2(path, kept)
            # An unfinished try to add a copy to a document that is already in Paperless (the app
            # was restarted part way) is still that: it keeps what was recorded when it began.
            resumed = not committed and bool(state.get("copy_added_later"))
            item.set(CAPTURED, copy_retry_by=__version__, copy_added_later=committed or resumed,
                     copy_retries={"version": __version__, "count": count + 1},
                     # A first note already says only the original was stored, so the copy needs its own.
                     copy_note_needed=bool(state.get("copy_note_needed")) or (bool(state.get("note_added")) and (
                         state.get("mode") == ORIGINAL_ONLY or (committed and reads_badly))),
                     copy_note_kind=state.get("copy_note_kind") if resumed else (
                         "added" if state.get("mode") == ORIGINAL_ONLY else "rebuilt"),
                     copy_note_added=bool(state.get("copy_note_added")) if resumed else False,
                     copy_retry_previous_reason=state.get("reason") if committed else (
                         state.get("copy_retry_previous_reason") if resumed else None),
                     copy_retry_restore={"status": item.status, **restore} if committed else (
                         state.get("copy_retry_restore") if resumed else None),
                     waiting_since_unix=None, alerted_reason_kind=None)
            retried.append(item.sha)
            if state.get("no_google_copy_reason"):
                log.info("%s has no searchable copy from an earlier version (%s); building it again", item.sha[:12],
                         state.get("no_google_copy_reason"))
            elif committed:
                log.info("%s has a searchable copy that Paperless couldn't read all of (%s); building it again",
                         item.sha[:12], state.get("reason"))
            else:
                log.info("%s was prepared by an earlier version and hasn't finished going in; preparing it again",
                         item.sha[:12])
        return retried

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
                 jane_decision="discard")
        self._finish(item, alert_kind=None)
        item.event("discarded")
        target = self.settings.discarded_dir / f"{item.sha}-{int(self.clock())}"
        target.parent.mkdir(parents=True, exist_ok=True)
        item.root.rename(target)
        return target
