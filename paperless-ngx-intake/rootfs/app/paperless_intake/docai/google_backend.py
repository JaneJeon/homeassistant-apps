"""Synchronous, pinned Google Document AI OCR with durable private provenance.

This module does not choose page order, mutate a PDF, concatenate text anchors,
call a batch API, or use GCS. A successful chunk keeps its own Document.text.
No backend is created on import. An operator must supply the processor version
and a durable directory outside Paperless's temporary parser work directory.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import stat
import time
from typing import Callable, Protocol
import urllib.error
import urllib.request
from uuid import uuid4

from .types import OCRChunk, ProcessingResult, SourcePDF
from .errors import ResultValidationError
from .validation import validate_chunk_document


PROJECT_ID = "paperlss-document-pipeline"
PROJECT_NUMBER = "589584677302"
MAX_REQUEST_PAGES = 15
MAX_REQUEST_BYTES = 40_000_000
MAX_POST_ATTEMPTS = 3
RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
_PROCESSOR_VERSION = re.compile(
    rf"projects/({PROJECT_ID}|{PROJECT_NUMBER})/locations/us/"
    r"processors/[A-Za-z0-9_-]+/processorVersions/"
    r"pretrained-ocr-v\d+(?:\.\d+){1,2}-\d{4}-\d{2}-\d{2}\Z"
)


class GoogleBackendError(RuntimeError):
    """A fixed non-sensitive failure code, never a provider exception message."""

    def __init__(self, code: str, *, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise GoogleBackendError("configuration_is_not_json_serializable") from None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class GoogleBackendConfig:
    processor_version: str
    provenance_dir: Path
    process_options: dict = field(default_factory=dict)
    pages_per_request: int = MAX_REQUEST_PAGES
    timeout_seconds: float = 180.0
    total_deadline_seconds: float = 600.0
    reuse_successful_cache: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.processor_version, str) or not _PROCESSOR_VERSION.fullmatch(
            self.processor_version,
        ):
            raise GoogleBackendError("pinned_ocr_version_in_allowed_project_us_required")
        if (
            not isinstance(self.pages_per_request, int)
            or isinstance(self.pages_per_request, bool)
            or not 1 <= self.pages_per_request <= MAX_REQUEST_PAGES
        ):
            raise GoogleBackendError("pages_per_request_must_be_1_to_15")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 600
        ):
            raise GoogleBackendError("invalid_request_timeout")
        if not isinstance(self.reuse_successful_cache, bool):
            raise GoogleBackendError("invalid_cache_reuse_flag")
        if (
            not isinstance(self.total_deadline_seconds, (int, float))
            or isinstance(self.total_deadline_seconds, bool)
            or not math.isfinite(self.total_deadline_seconds)
            or not 0 < self.total_deadline_seconds <= 1200
        ):
            raise GoogleBackendError("total_deadline_must_be_at_most_1200_seconds")
        if not isinstance(self.process_options, dict) or set(self.process_options) - {"ocrConfig"}:
            raise GoogleBackendError("only_ocr_config_process_options_are_allowed")
        if "ocrConfig" in self.process_options and not isinstance(
            self.process_options["ocrConfig"], dict,
        ):
            raise GoogleBackendError("ocr_config_must_be_an_object")
        # Take a private copy: mutation of the caller's options cannot change a run.
        object.__setattr__(self, "process_options", json.loads(_json_bytes(self.process_options)))
        try:
            path = Path(self.provenance_dir).expanduser().absolute()
        except (TypeError, ValueError, OSError):
            raise GoogleBackendError("invalid_provenance_directory") from None
        object.__setattr__(self, "provenance_dir", path)


TokenProvider = Callable[[], str]


class ADCTokenProvider:
    """Lazy official google-auth ADC; no CLI login or credential-file writes."""

    def __init__(self) -> None:
        self._credentials = None
        self._request = None

    def __call__(self) -> str:
        return self._get_token(force_refresh=False)

    def refresh(self) -> str:
        """Force one credential refresh after a server-side HTTP 401."""
        return self._get_token(force_refresh=True)

    def _get_token(self, *, force_refresh: bool) -> str:
        try:
            import google.auth
            from google.auth.transport.requests import Request

            if self._credentials is None:
                self._credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                self._request = Request()
            if force_refresh or not self._credentials.valid:
                self._credentials.refresh(self._request)
            token = self._credentials.token
        except Exception:
            raise GoogleBackendError("adc_authentication_failed") from None
        return _validate_token(token)


def _validate_token(token: object) -> str:
    if (
        not isinstance(token, str)
        or not token
        or not token.isascii()
        or any(character.isspace() for character in token)
        or any(ord(character) < 33 or ord(character) == 127 for character in token)
    ):
        raise GoogleBackendError("invalid_access_token")
    return token


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    retry_after_seconds: float | None = None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


class SyncTransport(Protocol):
    def post(
        self, endpoint: str, body: bytes, token: str, timeout_seconds: float,
    ) -> HTTPResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibSyncTransport:
    """Exactly one POST attempt; redirects and automatic retries are absent."""

    def post(
        self, endpoint: str, body: bytes, token: str, timeout_seconds: float,
    ) -> HTTPResponse:
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "x-goog-user-project": PROJECT_ID,
            },
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                return HTTPResponse(status=response.status, body=response.read())
        except urllib.error.HTTPError as error:
            status = error.code
            retry_after = _retry_after(error.headers.get("Retry-After"))
            error.close()  # Provider error bodies may contain document content.
            return HTTPResponse(status=status, body=b"", retry_after_seconds=retry_after)
        except Exception:
            raise GoogleBackendError("post_outcome_unknown") from None


def _private_directory(path: Path) -> None:
    try:
        if path.is_symlink():
            raise GoogleBackendError("symlink_provenance_directory_not_allowed")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise GoogleBackendError("provenance_directory_required")
        path.chmod(0o700)
    except GoogleBackendError:
        raise
    except OSError:
        raise GoogleBackendError("private_directory_unavailable") from None


def _durable_directory(provenance_dir: Path, work_dir: Path, source_path: Path) -> Path:
    """Require a dedicated resolved subtree disjoint from temporary inputs.

    Both directions are rejected: using the scratch root as the cache is also
    invalid. Path-component ancestry, not string prefixes, defines overlap.
    """
    try:
        durable = provenance_dir.resolve(strict=False)
        # A scratch-local symlink to an external target is also unsuitable:
        # cleanup removes that configured path, even if its target survives.
        candidates = {Path(os.path.abspath(provenance_dir)), durable}
        forbidden_paths = {
            Path(os.path.abspath(work_dir)), work_dir.resolve(strict=False),
            Path(os.path.abspath(source_path)).parent,
            source_path.resolve(strict=False).parent,
        }
    except (OSError, RuntimeError):
        raise GoogleBackendError("provenance_path_could_not_be_resolved") from None
    for candidate in candidates:
        for forbidden in forbidden_paths:
            if candidate.is_relative_to(forbidden) or forbidden.is_relative_to(candidate):
                raise GoogleBackendError("durable_provenance_overlaps_temporary_or_source_tree")
    return durable


@contextmanager
def _request_lock(directory: Path):
    """The kernel releases this lock on process death; the empty file may stay."""
    descriptor = None
    try:
        descriptor = os.open(
            directory / ".request-lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise GoogleBackendError("request_lock_permissions_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GoogleBackendError("request_in_progress") from None
        yield
    except GoogleBackendError:
        raise
    except OSError:
        raise GoogleBackendError("request_lock_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_write(path: Path, content: bytes) -> None:
    """Atomic private write, fsynced before a request is allowed to start."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except OSError:
        raise GoogleBackendError("private_provenance_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _private_json(path: Path, value: object) -> None:
    _private_write(path, _json_bytes(value) + b"\n")


def _read_private(path: Path) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise GoogleBackendError("cached_provenance_permissions_invalid")
        return path.read_bytes()
    except GoogleBackendError:
        raise
    except OSError:
        raise GoogleBackendError("cached_provenance_unreadable") from None


def _read_manifest(path: Path) -> dict:
    try:
        value = json.loads(_read_private(path))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise GoogleBackendError("cached_manifest_invalid") from None
    if not isinstance(value, dict):
        raise GoogleBackendError("cached_manifest_invalid")
    return value


def validate_response_pages(response: object, source_pages: tuple[int, ...]) -> dict:
    """Use the same coverage/mapping policy as cache replay and the parser."""
    if not isinstance(response, dict) or not isinstance(response.get("document"), dict):
        raise GoogleBackendError("response_missing_document")
    document = response["document"]
    try:
        validate_chunk_document(document, source_pages)
    except ResultValidationError:
        raise GoogleBackendError("response_document_validation_failed") from None
    return document


class GoogleDocumentAIBackend:
    """Sync OCR, with at most three POSTs per chunk retry cycle.

    A successful cache is reused by default. Disabling successful-cache reuse
    explicitly starts a new cycle after a prior success. A failed cycle keeps
    its count across process restarts. There is no daemon or later redrive:
    failures beyond the inline budget still need a separate task-level policy.
    ADC's optional dependencies and a production Paperless deployment have not
    been qualified by this prototype's injected-token/offline tests.
    """

    def __init__(
        self,
        config: GoogleBackendConfig,
        *,
        token_provider: TokenProvider | None = None,
        token_refresher: TokenProvider | None = None,
        transport: SyncTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        self.config = config
        self._options = json.loads(_json_bytes(config.process_options))
        self._token_provider = token_provider if token_provider is not None else ADCTokenProvider()
        self._token_refresher = token_refresher or getattr(self._token_provider, "refresh", None)
        self._transport = transport if transport is not None else UrllibSyncTransport()
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._random_fraction = random_fraction
        self.endpoint = (
            "https://us-documentai.googleapis.com/v1/" + config.processor_version + ":process"
        )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise GoogleBackendError("total_processing_deadline_exceeded")
        return remaining

    def _wait_until(self, when: float, deadline: float) -> None:
        delay = max(0.0, when - self._wall_clock())
        remaining = self._remaining(deadline)
        if delay >= remaining:
            raise GoogleBackendError("total_processing_deadline_exceeded")
        if delay:
            self._sleep(delay)
        self._remaining(deadline)

    def _retry_time(self, post_count: int, retry_after: float | None = None) -> float:
        cap = min(30.0, 2.0 * (2 ** max(0, post_count - 1)))
        fraction = min(1.0, max(0.0, self._random_fraction()))
        delay = cap * fraction
        if isinstance(retry_after, (float, int)) and math.isfinite(retry_after):
            delay = max(delay, max(0.0, retry_after))
        return self._wall_clock() + delay

    def process(self, source: SourcePDF, work_dir: Path) -> ProcessingResult:
        # Location validation precedes mkdir/chmod, authentication and all POSTs.
        durable = _durable_directory(
            self.config.provenance_dir, Path(work_dir), Path(source.path),
        )
        if not isinstance(source.data, bytes) or not source.data.startswith(b"%PDF-"):
            raise GoogleBackendError("pdf_bytes_required")
        if len(source.data) > MAX_REQUEST_BYTES:
            raise GoogleBackendError("source_exceeds_40000000_bytes_no_cloud_fallback")
        if _sha(source.data) != source.sha256:
            raise GoogleBackendError("source_sha256_mismatch")
        if (
            not isinstance(source.page_count, int)
            or isinstance(source.page_count, bool)
            or source.page_count < 1
        ):
            raise GoogleBackendError("positive_source_page_count_required")
        _private_directory(durable)
        _private_directory(Path(work_dir))
        # Recheck resolved ancestry after creating a previously absent directory.
        if _durable_directory(durable, Path(work_dir), Path(source.path)) != durable:
            raise GoogleBackendError("provenance_directory_changed")
        deadline = self._monotonic() + self.config.total_deadline_seconds
        chunks = []
        for first in range(1, source.page_count + 1, self.config.pages_per_request):
            self._remaining(deadline)
            selection = tuple(range(first, min(
                first + self.config.pages_per_request, source.page_count + 1,
            )))
            chunks.append(self._process_chunk(source, selection, durable, deadline))
        self._remaining(deadline)
        provenance = {
            "backend": "google_document_ai_sync_raw_pdf_selectors_v2",
            "processor_version": self.config.processor_version,
            "source_sha256": source.sha256,
            "source_byte_count": len(source.data),
            "source_page_count": source.page_count,
            "durable_provenance_directory": str(durable),
            "page_number_semantics": "Original source PDF page positions, 1-based.",
            "text_anchor_semantics": "Each chunk indexes only its own Document.text.",
            "chunk_manifests": [chunk.provenance["manifest_path"] for chunk in chunks],
            "cache_hits": sum(bool(chunk.provenance["cache_hit"]) for chunk in chunks),
            "retry_policy": {
                "max_post_attempts_per_chunk_cycle": MAX_POST_ATTEMPTS,
                "total_deadline_seconds": self.config.total_deadline_seconds,
                "later_redrive": False,
            },
        }
        _private_json(Path(work_dir) / "google-backend-result.json", provenance)
        return ProcessingResult(chunks=tuple(chunks), provenance=provenance)

    def _history(
        self, directory: Path, identity: dict, selection: tuple[int, ...],
    ) -> list[tuple[Path, dict]]:
        records = []
        for manifest_path in directory.glob("attempt-*/manifest.json"):
            manifest = _read_manifest(manifest_path)
            if manifest.get("identity") != identity:
                raise GoogleBackendError("cached_request_identity_mismatch")
            records.append((manifest_path, manifest))
        records.sort(key=lambda pair: (
            pair[1].get("sequence_number", 0), pair[1].get("created_at", ""), str(pair[0]),
        ))
        # Under the OS lock, an unfinished request belongs to a dead caller.
        # A fully saved response can be recovered without paying for OCR again.
        for manifest_path, manifest in records:
            if manifest.get("status") != "request_started":
                continue
            response_path = manifest_path.parent / "response.json"
            if response_path.exists():
                raw = _read_private(response_path)
                try:
                    document = self._decode_document(raw, selection)
                except GoogleBackendError as error:
                    manifest.update(status="invalid_response", failure_code=error.code,
                                    finished_at=_now(), recovered_after_interruption=True)
                else:
                    manifest.update(
                        status="complete", response_sha256=_sha(raw), http_status=200,
                        returned_pages=[p["pageNumber"] for p in document["pages"]],
                        finished_at=_now(), recovered_after_interruption=True,
                    )
            else:
                manifest.update(
                    status="outcome_unknown", retry_kind="transient_unknown",
                    next_attempt_at_unix=self._retry_time(1),
                    finished_at=_now(), recovered_after_interruption=True,
                )
            _private_json(manifest_path, manifest)
        return records

    @staticmethod
    def _active_cycle(records: list[tuple[Path, dict]]) -> list[tuple[Path, dict]]:
        last_success = -1
        for index, (_, manifest) in enumerate(records):
            if manifest.get("status") == "complete":
                last_success = index
        return records[last_success + 1:]

    @staticmethod
    def _post_records(records: list[tuple[Path, dict]]) -> list[tuple[Path, dict]]:
        return [(path, manifest) for path, manifest in records
                if "submitted_at" in manifest or manifest.get("status") in {
                    "request_started", "outcome_unknown", "http_error",
                    "invalid_response", "complete",
                }]

    def _retry_state(
        self, cycle: list[tuple[Path, dict]], post_records: list[tuple[Path, dict]],
    ) -> tuple[bool, float]:
        if cycle:
            latest = cycle[-1][1]
            if latest.get("status") in {"authentication_refresh_failed", "authentication_refresh_unavailable"}:
                raise GoogleBackendError("authentication_refresh_failed_requires_configuration")
        if not post_records:
            return False, self._wall_clock()
        latest = post_records[-1][1]
        status = latest.get("status")
        http_status = latest.get("http_status")
        if status == "invalid_response":
            raise GoogleBackendError(latest.get("failure_code", "response_document_validation_failed"))
        if status == "http_error" and http_status not in RETRYABLE_HTTP and http_status != 401:
            raise GoogleBackendError("post_http_error_fatal", http_status=http_status)
        force_refresh = status == "http_error" and http_status == 401
        if force_refresh and any(manifest.get("forced_auth_refresh") for _, manifest in cycle):
            raise GoogleBackendError("authentication_rejected_after_refresh", http_status=401)
        if status not in {"http_error", "outcome_unknown", "request_started"}:
            raise GoogleBackendError("cached_attempt_state_invalid")
        return force_refresh, float(latest.get("next_attempt_at_unix", self._wall_clock()))

    @staticmethod
    def _decode_document(raw: bytes, selection: tuple[int, ...]) -> dict:
        try:
            return validate_response_pages(json.loads(raw), selection)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, TypeError):
            raise GoogleBackendError("response_json_invalid") from None

    def _process_chunk(
        self, source: SourcePDF, selection: tuple[int, ...],
        durable: Path, deadline: float,
    ) -> OCRChunk:
        options = json.loads(_json_bytes(self._options))
        options["individualPageSelector"] = {"pages": list(selection)}
        identity = {
            "schema_version": 1,
            "operation": "sync_process_original_raw_pdf",
            "source_sha256": source.sha256,
            "source_byte_count": len(source.data),
            "source_page_count": source.page_count,
            "processor_version": self.config.processor_version,
            "process_options": options,
            "requested_pages": list(selection),
            "mime_type": "application/pdf",
        }
        key = _sha(_json_bytes(identity))
        directory = durable / key
        _private_directory(directory)
        with _request_lock(directory):
            records = self._history(directory, identity, selection)
            complete = [(path, manifest) for path, manifest in records
                        if manifest.get("status") == "complete"]
            if self.config.reuse_successful_cache and complete:
                path, manifest = complete[-1]
                return self._load_completed_chunk(path, manifest, selection, cache_hit=True)
            body = _json_bytes({
                "rawDocument": {
                    "mimeType": "application/pdf",
                    "content": base64.b64encode(source.data).decode("ascii"),
                },
                "processOptions": options,
            })
            while True:
                cycle = self._active_cycle(records)
                posts = self._post_records(cycle)
                force_refresh, next_attempt = self._retry_state(cycle, posts)
                if len(posts) >= MAX_POST_ATTEMPTS:
                    raise GoogleBackendError(
                        "post_retry_budget_exhausted",
                        http_status=posts[-1][1].get("http_status") if posts else None,
                    )
                self._wait_until(next_attempt, deadline)
                sequence = max((m.get("sequence_number", 0) for _, m in records), default=0) + 1
                attempt = directory / f"attempt-{sequence:06d}-{uuid4().hex}"
                _private_directory(attempt)
                manifest_path = attempt / "manifest.json"
                manifest = {
                    "identity": identity, "cache_key": key, "created_at": _now(),
                    "sequence_number": sequence, "status": "prepared",
                    "endpoint": self.endpoint, "source_path_at_call": str(source.path),
                    "forced_auth_refresh": force_refresh,
                }
                _private_json(manifest_path, manifest)
                records.append((manifest_path, manifest))
                if force_refresh and not callable(self._token_refresher):
                    manifest.update(status="authentication_refresh_unavailable", finished_at=_now())
                    _private_json(manifest_path, manifest)
                    raise GoogleBackendError("authentication_refresh_unavailable")
                try:
                    provider = self._token_refresher if force_refresh else self._token_provider
                    token = _validate_token(provider())
                except Exception:
                    manifest.update(
                        status="authentication_refresh_failed" if force_refresh else "authentication_failed",
                        finished_at=_now(),
                    )
                    _private_json(manifest_path, manifest)
                    raise GoogleBackendError("authentication_failed_before_post") from None
                remaining = self._remaining(deadline)
                # An attempt is committed before the billable operation, not
                # after receiving its response. Restart never resets this count.
                manifest.update(
                    status="request_started", submitted_at=_now(),
                    post_attempt_number=len(posts) + 1,
                    request_timeout_seconds=min(self.config.timeout_seconds, remaining),
                )
                _private_json(manifest_path, manifest)
                try:
                    response = self._transport.post(
                        self.endpoint, body, token, min(self.config.timeout_seconds, remaining),
                    )
                except Exception:
                    manifest.update(
                        status="outcome_unknown", retry_kind="transient_unknown",
                        next_attempt_at_unix=self._retry_time(len(posts) + 1),
                        finished_at=_now(),
                    )
                    _private_json(manifest_path, manifest)
                    continue
                if response.status != 200:
                    retry_kind = (
                        "refresh_auth_once" if response.status == 401
                        else "transient_http" if response.status in RETRYABLE_HTTP
                        else "fatal_http"
                    )
                    manifest.update(
                        status="http_error", http_status=response.status,
                        retry_kind=retry_kind,
                        next_attempt_at_unix=(
                            self._wall_clock() if response.status == 401 else
                            self._retry_time(len(posts) + 1, response.retry_after_seconds)
                        ),
                        finished_at=_now(),
                    )
                    _private_json(manifest_path, manifest)
                    continue
                # Preserve original response bytes before parsing or validating.
                _private_write(attempt / "response.json", response.body)
                manifest.update(response_sha256=_sha(response.body), http_status=200)
                try:
                    document = self._decode_document(response.body, selection)
                except GoogleBackendError as error:
                    manifest.update(status="invalid_response", failure_code=error.code,
                                    finished_at=_now())
                    _private_json(manifest_path, manifest)
                    raise
                manifest.update(
                    status="complete", returned_pages=[p["pageNumber"] for p in document["pages"]],
                    finished_at=_now(),
                )
                _private_json(manifest_path, manifest)
                return self._chunk(manifest_path, manifest, document, selection, cache_hit=False)

    def _load_completed_chunk(
        self, manifest_path: Path, manifest: dict, selection: tuple[int, ...], *, cache_hit: bool,
    ) -> OCRChunk:
        raw = _read_private(manifest_path.parent / "response.json")
        if _sha(raw) != manifest.get("response_sha256"):
            raise GoogleBackendError("cached_response_sha256_mismatch")
        document = self._decode_document(raw, selection)
        return self._chunk(manifest_path, manifest, document, selection, cache_hit=cache_hit)

    def _chunk(
        self, manifest_path: Path, manifest: dict, document: dict,
        selection: tuple[int, ...], *, cache_hit: bool,
    ) -> OCRChunk:
        # Return the validated response's mapping, never trust an unvalidated
        # manifest copy of it when exposing cache provenance.
        returned_pages = [page["pageNumber"] for page in document["pages"]]
        return OCRChunk(
            source_pages=selection,
            document=document,
            processor_version=self.config.processor_version,
            response_sha256=manifest["response_sha256"],
            provenance={
                "manifest_path": str(manifest_path),
                "response_path": str(manifest_path.parent / "response.json"),
                "cache_key": manifest["cache_key"],
                "cache_hit": cache_hit,
                "input_sha256": manifest["identity"]["source_sha256"],
                "requested_pages": list(selection),
                "returned_pages": returned_pages,
                "process_options": manifest["identity"]["process_options"],
                "post_attempt_number": manifest.get("post_attempt_number", 1),
            },
        )
