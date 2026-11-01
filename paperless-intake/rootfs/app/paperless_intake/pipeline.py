"""What happens to one scan: OCR and checks, then commit and read-back.

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
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__, checks
from .config import Settings
from .docai.errors import ParserError
from .docai.google_backend import GoogleBackendConfig, GoogleBackendError, GoogleDocumentAIBackend
from .docai.page_order import plan_page_order
from .docai.pdf_renderer import ReviewRequired
from .docai.renderer_adapter import AutomaticSourcePreservedRenderer
from .docai.validation import assert_unchanged, document_for_renderer, inspect_source, ordered_text, validate_result
from .notify import Notifier
from .paperless import Paperless, PaperlessError, PaperlessTaskFailed
from .store import (COMMITTED, COMMITTED_WARNING, COMMITTING, CAPTURED, DISCARDED, FAILED, HELD, READY, WAITING,
                    Item, Store,
                    iso, now, write_atomic)

log = logging.getLogger(__name__)

WITH_GOOGLE = "with_google"
ORIGINAL_ONLY = "original_only"

ORDER_WORDS = {
    "complete_numbering_in_order": "as scanned, confirmed by the printed page numbers",
    "reorder_from_complete_numbering": "reordered to follow the printed page numbers",
    "partial_in_order": "as scanned; the printed page numbers seen agree",
    "unknown_no_pagination_evidence": "as scanned (no printed page numbers to check)",
}

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
            self._wait(item, Wait(f"unexpected error ({type(error).__name__})", "unexpected",
                                  "commit" if item.status in {READY, COMMITTING} else "ocr"))

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
        backend = self.backend_factory(item)
        try:
            result = backend.process(source, work_dir)
        except GoogleBackendError as error:
            if error.code in TRANSIENT_GOOGLE:
                raise Wait(f"Google OCR didn't answer ({error.code})", "google", "ocr")
            if error.code in AUTH_GOOGLE:
                raise Wait(f"Google refused the app's key ({error.code})", "auth", "ocr", alert=True)
            raise Hold(f"Google couldn't process this scan ({error.code})", "google_rejected")
        cache_hits = int(result.provenance.get("cache_hits", 0))
        if cache_hits < len(result.chunks):
            self._count_pages(source.page_count)

        try:
            validate_result(result, source)
            document = document_for_renderer(result, source)
        except ParserError as error:
            raise Hold(f"Google's answer didn't match the scan's pages ({error})", "google_mismatch")

        plan = plan_page_order(document)
        gap = checks.missing_pages(plan, source.page_count)
        coverage = checks.coverage(document)
        if gap:
            self._write_checks(item, {"plan": plan.to_dict(), "missing_pages": gap, "coverage": coverage})
            raise Hold(gap, "missing_page")

        renderer = AutomaticSourcePreservedRenderer(font_path=self.settings.font_path)
        try:
            rendered = renderer.render(source, result, work_dir, produce_archive=True)
        except (ReviewRequired, ParserError, ValueError) as error:
            self._write_checks(item, {"plan": plan.to_dict(), "coverage": coverage, "render_error": str(error)})
            raise Hold(self._plain_render_error(str(error)), "render_check")
        assert_unchanged(source)
        order = tuple(rendered.page_order or range(1, source.page_count + 1))
        text = ordered_text(result, source, order)
        searchable = rendered.archive_path.read_bytes()
        write_atomic(item.searchable_path, searchable)
        write_atomic(item.root / "text.txt", text.encode("utf-8"))

        mode = ORIGINAL_ONLY if checks.short_text(text) else WITH_GOOGLE
        provenance = rendered.provenance
        self._write_checks(item, {
            "plan": plan.to_dict(),
            "missing_pages": None,
            "coverage": coverage,
            "render_checks": provenance.get("checks"),
            "orientation_decisions": provenance.get("orientation_decisions"),
            "page_order_decision": provenance.get("page_order_decision", {}).get("status"),
            "google": {
                "processor_version": self.settings.processor_version,
                "responses": [chunk.response_sha256 for chunk in result.chunks],
                "cache_hits": cache_hits,
            },
            "text_sha256": sha256(text.encode("utf-8")),
            "searchable_sha256": sha256(searchable),
            "mode": mode,
        })
        item.set(READY, mode=mode, page_order=list(order),
                 order_status=provenance.get("page_order_decision", {}).get("status"),
                 rotations=list(provenance.get("cardinalClockwiseBySourcePage") or []),
                 searchable_sha256=sha256(searchable),
                 short_text=mode == ORIGINAL_ONLY,
                 waiting_since_unix=None, alerted_reason_kind=None)

    @staticmethod
    def _plain_render_error(message: str) -> str:
        if message.startswith("Page order needs review"):
            return "the printed page numbers don't give one clear page order"
        if "registration" in message or "raster" in message or "mapping" in message:
            return "Google's text couldn't be lined up with the scanned page exactly"
        if "JPEG" in message or "image" in message.lower() or "Source rotation" in message or "cropped" in message:
            return "the scan's page format isn't one the app supports (expected one full-page image per page)"
        if "Font does not cover" in message:
            return "the scan has characters the app's font can't write into the searchable copy"
        if "rotation" in message.lower():
            return "a page is turned at an angle the app won't correct automatically"
        return f"the searchable copy failed a check ({message})"

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
        original = item.source_path.read_bytes()
        if sha256(original) != item.sha:
            item.set(FAILED, reason="the local copy of the scan changed on disk", reason_kind="local_integrity")
            self._finish(item, alert_kind="failed")
            return

        try:
            doc_id = self._ensure_original(item, original, tag_ids)
            self._ensure_unowned(item, doc_id)
            if mode == WITH_GOOGLE:
                self._ensure_version(item, doc_id)
            problems, warnings = self._read_back(item, doc_id, tag_ids, mode)
            if not problems:
                self._ensure_note(item, doc_id, mode, warnings)
        except PaperlessTaskFailed as error:
            raise Wait(f"Paperless couldn't take the upload ({str(error)}: {error.result[:200]})",
                       "paperless_task", "commit", alert=True)
        except PaperlessError as error:
            raise Wait(str(error), "paperless", "commit")

        if problems:
            item.set(FAILED, reason="; ".join(problems), reason_kind="read_back", doc_id=doc_id)
            self._finish(item, alert_kind="failed")
            return
        status = COMMITTED_WARNING if warnings else COMMITTED
        item.set(status, reason="; ".join(warnings) or None, reason_kind="read_back" if warnings else None,
                 doc_id=doc_id, committed_at=iso(self.clock()), committed_at_unix=self.clock(),
                 waiting_since_unix=None, alerted_reason_kind=None)
        self._tidy_after_commit(item)
        self._finish(item, alert_kind="warning" if warnings else None)

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
            task_id = self.paperless.post_document(item.state["original_name"], original, tag_ids=tag_ids)
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

    def _ensure_version(self, item: Item, doc_id: int) -> None:
        wanted = item.state["searchable_sha256"]
        if any(v.checksum == wanted for v in self.paperless.versions(doc_id)):
            return
        task_id = item.state.get("version_task")
        if task_id:
            try:
                self.paperless.wait_for_task(task_id)
            except PaperlessTaskFailed:
                item.state["version_task"] = None
                item.save()
            if any(v.checksum == wanted for v in self.paperless.versions(doc_id)):
                return
        data = item.searchable_path.read_bytes()
        if sha256(data) != wanted:
            raise Hold("the local searchable copy changed on disk", "local_integrity")
        stem = Path(item.state["original_name"]).stem
        task_id = self.paperless.update_version(doc_id, f"{stem}.searchable.pdf", data, label=self.version_label())
        item.event("uploaded_version", task=task_id)
        item.state["version_task"] = task_id
        item.save()
        self.paperless.wait_for_task(task_id)

    def _read_back(self, item: Item, doc_id: int, tag_ids: list[int], mode: str) -> tuple[list[str], list[str]]:
        """Problems mean the original isn't safely in Paperless. Warnings concern version 2 only."""
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
        for name, tag_id in zip((self.settings.physical_mail_tag, self.settings.intake_tag), tag_ids):
            if tag_id not in present:
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
                    warnings.append("Paperless re-ran its own OCR on the searchable copy")
                google_text = (item.root / "text.txt").read_text(encoding="utf-8")
                match = checks.text_matches(google_text, document.get("content") or "")
                record["text_match"] = match
                if not match["ok"]:
                    warnings.append(f"Paperless's search text differs from Google's "
                                    f"({match['missing_words']} of {match['google_words']} words missing)")
                record["version_id"] = version.id
        item.event("read_back", doc_id=doc_id, problems=problems, warnings=warnings, **record)
        return problems, warnings

    def _ensure_note(self, item: Item, doc_id: int, mode: str, warnings: list[str]) -> None:
        if item.state.get("note_added"):
            return
        pages = item.state.get("page_count")
        lines = [f"Scanned {item.state.get('captured_at')} and checked by paperless-intake {__version__}."]
        if mode == WITH_GOOGLE:
            processor = self.settings.processor_version.rsplit("/", 1)[-1]
            lines.append(f"Google OCR ({processor}) is version 2; the original scan is version 1.")
            order = ORDER_WORDS.get(item.state.get("order_status") or "", "as scanned")
            lines.append(f"Pages: {pages}. Page order: {order}.")
            rotations = [r for r in item.state.get("rotations") or [] if r]
            if rotations:
                lines.append("Some pages were turned upright in the searchable copy.")
        elif item.state.get("jane_decision") == "keep_original":
            lines.append(f"Kept as the original only, by Jane's choice. Unresolved: {item.state.get('held_reason')}.")
        else:
            lines.append("Google found almost no text, so only the original was stored and Paperless did its own OCR.")
        if warnings:
            lines.append("Read-back warnings: " + "; ".join(warnings) + ".")
        self.paperless.add_note(doc_id, " ".join(lines))
        item.state["note_added"] = True
        item.save()

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
        item.set(HELD, reason=reason, reason_kind=kind, held_reason=reason, held_at=iso(self.clock()))
        self._finish(item, alert_kind="held")

    def _wait(self, item: Item, wait: Wait) -> None:
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

    def keep_original_only(self, item: Item) -> None:
        if item.status != HELD:
            raise ValueError("only held scans can be kept as the original")
        item.set(READY, mode=ORIGINAL_ONLY, jane_decision="keep_original")

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
