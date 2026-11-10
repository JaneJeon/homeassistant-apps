"""Offline backend contract tests; all OCR responses and credentials are synthetic."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from paperless_intake.docai.google_backend import (
    GoogleBackendConfig,
    GoogleBackendError,
    GoogleDocumentAIBackend,
    HTTPResponse,
    MAX_REQUEST_BYTES,
    validate_response_pages,
)
from paperless_intake.docai.types import SourcePDF


VERSION = (
    "projects/paperlss-document-pipeline/locations/us/processors/unit-test/"
    "processorVersions/pretrained-ocr-v2.1-2024-08-07"
)
TOKEN = "synthetic-unit-token-do-not-persist"
PDF_BYTES = b"%PDF-1.7\npublic synthetic backend boundary fixture\n%%EOF\n"


def response_for(numbers: list[int]) -> bytes:
    text = ""
    pages = []
    for number in numbers:
        start = len(text)
        text += f"Public synthetic page {number}.\n"
        pages.append({
            "pageNumber": number,
            "layout": {"textAnchor": {"textSegments": [{
                "startIndex": str(start), "endIndex": str(len(text)),
            }]}},
        })
    return json.dumps({"document": {"text": text, "pages": pages}}).encode()


class FakeTransport:
    def __init__(self, action=None):
        self.calls = []
        self.action = action

    def post(self, endpoint, body, token, timeout_seconds):
        request = json.loads(body)
        self.calls.append((endpoint, request, token, timeout_seconds))
        if self.action is not None:
            return self.action(request)
        return HTTPResponse(
            status=200,
            body=response_for(request["processOptions"]["individualPageSelector"]["pages"]),
        )


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.delays = []

    def monotonic(self):
        return self.elapsed

    def time(self):
        return 1_700_000_000.0 + self.elapsed

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.elapsed += seconds


class GoogleBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        snapshot_dir = self.root / "source-snapshot"
        snapshot_dir.mkdir()
        self.snapshot = snapshot_dir / "snapshot.pdf"
        self.snapshot.write_bytes(PDF_BYTES)
        self.snapshot.chmod(0o400)
        self.transport = FakeTransport()
        self.clock = FakeClock()

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, count=3, *, data=PDF_BYTES, path=None):
        return SourcePDF(
            path=path or self.snapshot, data=data,
            sha256=hashlib.sha256(data).hexdigest(), page_count=count,
        )

    def backend(self, **overrides):
        durable = overrides.pop("provenance_dir", self.root / "durable")
        refresher = overrides.pop("token_refresher", None)
        config = GoogleBackendConfig(
            processor_version=overrides.pop("processor_version", VERSION),
            provenance_dir=durable,
            **overrides,
        )
        return GoogleDocumentAIBackend(
            config, token_provider=lambda: TOKEN, token_refresher=refresher,
            transport=self.transport, sleep=self.clock.sleep,
            monotonic=self.clock.monotonic, wall_clock=self.clock.time,
            random_fraction=lambda: 1.0,
        )

    def test_31_pages_use_three_original_byte_selector_requests(self):
        source = self.source(31)
        result = self.backend().process(source, self.root / "work")
        expected = [list(range(1, 16)), list(range(16, 31)), [31]]
        self.assertEqual([list(c.source_pages) for c in result.chunks], expected)
        self.assertEqual(len(self.transport.calls), 3)
        for call, requested in zip(self.transport.calls, expected):
            endpoint, request, token, _ = call
            self.assertEqual(endpoint, "https://us-documentai.googleapis.com/v1/" + VERSION + ":process")
            self.assertEqual(token, TOKEN)
            self.assertEqual(set(request), {"rawDocument", "processOptions"})
            self.assertEqual(base64.b64decode(request["rawDocument"]["content"]), PDF_BYTES)
            self.assertEqual(request["processOptions"]["individualPageSelector"]["pages"], requested)
        self.assertEqual(self.snapshot.read_bytes(), PDF_BYTES)
        self.assertEqual(stat.S_IMODE(self.snapshot.stat().st_mode), 0o400)
        # Chunk two owns a fresh text string and starts its local anchor at zero.
        second = result.chunks[1].document
        self.assertEqual(second["pages"][0]["pageNumber"], 16)
        self.assertEqual(second["pages"][0]["layout"]["textAnchor"]["textSegments"][0]["startIndex"], "0")
        self.assertTrue(second["text"].startswith("Public synthetic page 16."))

    def test_cache_uses_bytes_version_and_options_not_temporary_path(self):
        first = self.backend().process(self.source(), self.root / "work")
        second = self.backend().process(
            self.source(path=self.root / "another-source" / "snapshot.pdf"), self.root / "work-2",
        )
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(second.chunks[0].provenance["cache_hit"])
        self.assertEqual(first.chunks[0].response_sha256, second.chunks[0].response_sha256)
        self.backend(process_options={"ocrConfig": {"enableImageQualityScores": True}}).process(
            self.source(), self.root / "work-3",
        )
        self.assertEqual(len(self.transport.calls), 2)
        self.backend(processor_version=VERSION.replace(
            "pretrained-ocr-v2.1-2024-08-07", "pretrained-ocr-v2.0-2023-06-02",
        )).process(self.source(), self.root / "work-4")
        self.assertEqual(len(self.transport.calls), 3)

    def test_disabling_success_cache_retains_both_attempts(self):
        backend = self.backend(reuse_successful_cache=False)
        backend.process(self.source(), self.root / "work")
        backend.process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(len(list((self.root / "durable").glob("*/attempt-*/response.json"))), 2)

    def test_missing_response_page_is_rejected_and_not_retried(self):
        self._assert_invalid_pages([1, 3], "response_document_validation_failed")

    def test_duplicate_response_page_is_rejected_and_not_retried(self):
        self._assert_invalid_pages([1, 1, 3], "response_document_validation_failed")

    def test_out_of_order_response_pages_are_rejected_not_silently_sorted(self):
        self._assert_invalid_pages([2, 1, 3], "response_document_validation_failed")

    def _assert_invalid_pages(self, numbers, code):
        self.transport.action = lambda _: HTTPResponse(200, response_for(numbers))
        backend = self.backend()
        with self.assertRaisesRegex(GoogleBackendError, code):
            backend.process(self.source(), self.root / "work")
        with self.assertRaisesRegex(GoogleBackendError, code):
            backend.process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 1)
        raw_path = next((self.root / "durable").glob("*/attempt-*/response.json"))
        self.assertEqual(raw_path.read_bytes(), response_for(numbers))

    def test_ambiguous_outcome_retries_at_most_three_times_across_restart(self):
        def fail(_):
            raise TimeoutError("private-document-or-token-must-never-escape")
        self.transport.action = fail
        backend = self.backend()
        with self.assertRaisesRegex(GoogleBackendError, "post_retry_budget_exhausted"):
            backend.process(self.source(), self.root / "work")
        with self.assertRaisesRegex(GoogleBackendError, "post_retry_budget_exhausted"):
            self.backend().process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 3)
        self.assertEqual(self.clock.delays, [2.0, 4.0])
        saved = b"".join(p.read_bytes() for p in (self.root / "durable").rglob("*.json"))
        self.assertNotIn(b"private-document-or-token", saved)
        self.assertNotIn(TOKEN.encode(), saved)
        self.assertIn(b"outcome_unknown", saved)

    def test_transient_http_error_preserves_status_without_body_and_retries_boundedly(self):
        self.transport.action = lambda _: HTTPResponse(503, b"provider-error-with-private-content")
        backend = self.backend()
        with self.assertRaises(GoogleBackendError) as caught:
            backend.process(self.source(), self.root / "work")
        self.assertEqual(caught.exception.http_status, 503)
        with self.assertRaisesRegex(GoogleBackendError, "post_retry_budget_exhausted"):
            backend.process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 3)
        self.assertFalse(list((self.root / "durable").rglob("response.json")))
        saved = b"".join(p.read_bytes() for p in (self.root / "durable").rglob("*.json"))
        self.assertNotIn(b"provider-error-with-private-content", saved)

    def test_invalid_json_is_preserved_but_cannot_become_a_successful_cache(self):
        self.transport.action = lambda _: HTTPResponse(200, b"not-json")
        with self.assertRaisesRegex(GoogleBackendError, "response_json_invalid"):
            self.backend().process(self.source(), self.root / "work")
        path = next((self.root / "durable").glob("*/attempt-*/response.json"))
        self.assertEqual(path.read_bytes(), b"not-json")

    def test_corrupted_cached_response_fails_without_network(self):
        result = self.backend().process(self.source(), self.root / "work")
        Path(result.chunks[0].provenance["response_path"]).write_bytes(b"corrupted")
        with self.assertRaisesRegex(GoogleBackendError, "cached_response_sha256_mismatch"):
            self.backend().process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 1)

    def test_provenance_is_private_and_contains_identity_without_token_or_input_blob(self):
        self.backend(process_options={"ocrConfig": {"enableImageQualityScores": False}}).process(
            self.source(), self.root / "work",
        )
        for path in (self.root / "durable").rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)
        manifest_path = next((self.root / "durable").glob("*/attempt-*/manifest.json"))
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["identity"]["source_sha256"], self.source().sha256)
        self.assertEqual(manifest["identity"]["requested_pages"], [1, 2, 3])
        self.assertEqual(manifest["identity"]["processor_version"], VERSION)
        saved = b"".join(p.read_bytes() for p in (self.root / "durable").rglob("*.json"))
        self.assertNotIn(TOKEN.encode(), saved)
        self.assertNotIn(base64.b64encode(PDF_BYTES), saved)

    def test_oversized_raw_pdf_fails_before_auth_or_request(self):
        data = b"%PDF-" + b" " * (MAX_REQUEST_BYTES - 4)
        with self.assertRaisesRegex(GoogleBackendError, "source_exceeds_40000000_bytes"):
            self.backend().process(self.source(1, data=data), self.root / "work")
        self.assertEqual(self.transport.calls, [])

    def test_configuration_rejects_wrong_project_region_alias_and_selector_override(self):
        invalid = [
            VERSION.replace("paperlss-document-pipeline", "other-project"),
            VERSION.replace("/locations/us/", "/locations/eu/"),
            VERSION.rsplit("/", 1)[0] + "/stable",
            VERSION.rsplit("/", 1)[0] + "/pretrained",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(GoogleBackendError, "pinned_ocr_version"):
                    self.backend(processor_version=value)
        with self.assertRaisesRegex(GoogleBackendError, "pages_per_request"):
            self.backend(pages_per_request=16)
        with self.assertRaisesRegex(GoogleBackendError, "only_ocr_config"):
            self.backend(process_options={"individualPageSelector": {"pages": [2]}})

    def test_response_local_anchor_bounds_are_checked(self):
        value = json.loads(response_for([1, 2, 3]))
        value["document"]["pages"][0]["layout"]["textAnchor"]["textSegments"][0]["endIndex"] = "9999"
        self.transport.action = lambda _: HTTPResponse(200, json.dumps(value).encode())
        with self.assertRaisesRegex(GoogleBackendError, "response_document_validation_failed"):
            self.backend().process(self.source(), self.root / "work")

    def test_auth_failure_is_not_a_post_and_safe_error_has_no_credential(self):
        def fail_auth():
            raise RuntimeError(TOKEN)
        backend = GoogleDocumentAIBackend(
            GoogleBackendConfig(VERSION, self.root / "durable"),
            token_provider=fail_auth, transport=self.transport,
        )
        with self.assertRaisesRegex(GoogleBackendError, "authentication_failed_before_post"):
            backend.process(self.source(), self.root / "work")
        self.assertEqual(self.transport.calls, [])
        result = self.backend().process(self.source(), self.root / "work")
        self.assertEqual(len(result.chunks), 1)

    def test_transient_statuses_and_unknown_outcomes_can_recover(self):
        for status in [408, 429, 500, 502, 503, 504, None]:
            with self.subTest(status=status):
                transport = FakeTransport()
                def action(request):
                    if len(transport.calls) == 1:
                        if status is None:
                            raise TimeoutError("synthetic unknown outcome")
                        return HTTPResponse(status, b"")
                    return HTTPResponse(200, response_for(
                        request["processOptions"]["individualPageSelector"]["pages"],
                    ))
                transport.action = action
                backend = self.backend(provenance_dir=self.root / f"durable-{status}")
                backend._transport = transport
                result = backend.process(self.source(), self.root / "work")
                self.assertEqual(len(transport.calls), 2)
                self.assertEqual(result.chunks[0].provenance["post_attempt_number"], 2)

    def test_fatal_http_statuses_do_not_spend_a_retry(self):
        for status in [400, 403, 404, 413, 501]:
            with self.subTest(status=status):
                transport = FakeTransport(lambda _: HTTPResponse(status, b"private error"))
                backend = self.backend(provenance_dir=self.root / f"fatal-{status}")
                backend._transport = transport
                for _ in range(2):
                    with self.assertRaisesRegex(GoogleBackendError, "post_http_error_fatal"):
                        backend.process(self.source(), self.root / "work")
                self.assertEqual(len(transport.calls), 1)

    def test_401_forces_one_refresh_then_uses_the_new_token(self):
        refresh_calls = []
        def refresh():
            refresh_calls.append(True)
            return "fresh-synthetic-token"
        def action(request):
            if len(self.transport.calls) == 1:
                return HTTPResponse(401, b"")
            return HTTPResponse(200, response_for(
                request["processOptions"]["individualPageSelector"]["pages"],
            ))
        self.transport.action = action
        result = self.backend(token_refresher=refresh).process(self.source(), self.root / "work")
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual([call[2] for call in self.transport.calls], [TOKEN, "fresh-synthetic-token"])
        self.assertEqual(result.chunks[0].provenance["post_attempt_number"], 2)

    def test_repeated_401_is_fatal_after_one_refresh_including_restart(self):
        refresh_calls = []
        def refresh():
            refresh_calls.append(True)
            return "fresh-synthetic-token"
        self.transport.action = lambda _: HTTPResponse(401, b"")
        with self.assertRaisesRegex(GoogleBackendError, "authentication_rejected_after_refresh"):
            self.backend(token_refresher=refresh).process(self.source(), self.root / "work")
        with self.assertRaisesRegex(GoogleBackendError, "authentication_rejected_after_refresh"):
            self.backend(token_refresher=refresh).process(self.source(), self.root / "work")
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(len(self.transport.calls), 2)

    def test_401_without_a_refresh_capability_does_not_loop(self):
        self.transport.action = lambda _: HTTPResponse(401, b"")
        with self.assertRaisesRegex(GoogleBackendError, "authentication_refresh_unavailable"):
            self.backend().process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 1)

    def test_retry_after_is_honored_without_exceeding_total_deadline(self):
        def action(request):
            if len(self.transport.calls) == 1:
                return HTTPResponse(429, b"", retry_after_seconds=12)
            return HTTPResponse(200, response_for(
                request["processOptions"]["individualPageSelector"]["pages"],
            ))
        self.transport.action = action
        self.backend().process(self.source(), self.root / "work")
        self.assertEqual(self.clock.delays, [12.0])
        self.assertEqual(len(self.transport.calls), 2)

    def test_deadline_stops_before_next_post_and_retry_time_survives_restart(self):
        self.transport.action = lambda _: HTTPResponse(429, b"", retry_after_seconds=120)
        with self.assertRaisesRegex(GoogleBackendError, "total_processing_deadline_exceeded"):
            self.backend(total_deadline_seconds=60).process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.clock.delays, [])
        self.clock.elapsed += 120
        self.transport.action = None
        result = self.backend(total_deadline_seconds=60).process(self.source(), self.root / "work")
        self.assertEqual(result.chunks[0].provenance["post_attempt_number"], 2)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertLessEqual(self.transport.calls[-1][3], 60)

    def test_interrupted_started_attempt_counts_and_can_recover_after_restart(self):
        def terminate(_):
            raise SystemExit("simulate worker death without catching the POST outcome")
        self.transport.action = terminate
        with self.assertRaises(SystemExit):
            self.backend().process(self.source(), self.root / "work")
        self.transport.action = None
        result = self.backend().process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(result.chunks[0].provenance["post_attempt_number"], 2)
        manifests = [json.loads(p.read_text()) for p in (self.root / "durable").glob("*/attempt-*/manifest.json")]
        self.assertTrue(any(m.get("recovered_after_interruption") for m in manifests))

    def test_three_interrupted_processes_do_not_reset_the_post_budget(self):
        def terminate(_):
            raise SystemExit("simulate interrupted transport")
        self.transport.action = terminate
        for _ in range(3):
            with self.assertRaises(SystemExit):
                self.backend().process(self.source(), self.root / "work")
        with self.assertRaisesRegex(GoogleBackendError, "post_retry_budget_exhausted"):
            self.backend().process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 3)

    def test_complete_saved_response_is_recovered_without_a_new_post(self):
        result = self.backend().process(self.source(), self.root / "work")
        path = Path(result.chunks[0].provenance["manifest_path"])
        manifest = json.loads(path.read_text())
        manifest["status"] = "request_started"
        manifest.pop("response_sha256")
        path.write_text(json.dumps(manifest))
        recovered = self.backend().process(self.source(), self.root / "work")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(recovered.chunks[0].document, result.chunks[0].document)
        self.assertTrue(json.loads(path.read_text())["recovered_after_interruption"])

    def test_operating_system_releases_request_lock_when_worker_is_killed(self):
        result = self.backend().process(self.source(), self.root / "work")
        directory = Path(result.chunks[0].provenance["manifest_path"]).parent.parent
        program = (
            "from pathlib import Path; import sys,time; "
            "from paperless_intake.docai.google_backend import _request_lock; "
            "lock=_request_lock(Path(sys.argv[1])); lock.__enter__(); "
            "print('ready',flush=True); time.sleep(60)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program, str(directory)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(GoogleBackendError, "request_in_progress"):
                self.backend().process(self.source(), self.root / "work")
            child.kill()
            child.wait(timeout=5)
            self.backend().process(self.source(), self.root / "work")
            self.assertEqual(len(self.transport.calls), 1)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_durable_root_rejects_equal_nested_and_ancestor_paths_before_post(self):
        work = self.root / "work"
        bad = [work, work / "durable", self.root, self.snapshot.parent,
               self.snapshot.parent / "durable"]
        for durable in bad:
            with self.subTest(durable=durable):
                with self.assertRaisesRegex(GoogleBackendError, "durable_provenance_overlaps"):
                    self.backend(provenance_dir=durable).process(self.source(), work)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(work.exists())

    def test_symlink_and_parent_components_cannot_bypass_durable_root_check(self):
        work = self.root / "work"
        work.mkdir()
        alias = self.root / "outside-looking-link"
        alias.symlink_to(work, target_is_directory=True)
        bad = [alias / "durable", self.root / "durable" / ".." / "work" / "cache"]
        for durable in bad:
            with self.subTest(durable=durable):
                with self.assertRaisesRegex(GoogleBackendError, "durable_provenance_overlaps"):
                    self.backend(provenance_dir=durable).process(self.source(), work)
        source_alias = self.root / "source-alias"
        source_alias.symlink_to(self.snapshot.parent, target_is_directory=True)
        with self.assertRaisesRegex(GoogleBackendError, "durable_provenance_overlaps"):
            self.backend(provenance_dir=self.snapshot.parent / "cache").process(
                self.source(path=source_alias / self.snapshot.name), work,
            )
        self.assertEqual(self.transport.calls, [])

    def test_similar_name_prefix_is_not_treated_as_path_ancestry(self):
        self.backend(provenance_dir=self.root / "work-durable").process(
            self.source(), self.root / "work",
        )
        self.assertEqual(len(self.transport.calls), 1)

    def test_scratch_local_symlink_to_external_cache_is_not_a_durable_config_path(self):
        work = self.root / "work"
        work.mkdir()
        external = self.root / "external-durable"
        external.mkdir()
        scratch_alias = work / "cache-link"
        scratch_alias.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(GoogleBackendError, "durable_provenance_overlaps"):
            self.backend(provenance_dir=scratch_alias).process(self.source(), work)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(external.iterdir()), [])

    def test_shared_validation_rejects_unanchored_nonwhitespace_and_accepts_word_gap(self):
        omitted = {"document": {"text": "alpha MISSING beta", "pages": [{
            "pageNumber": 1, "layout": {"textAnchor": {"textSegments": [
                {"startIndex": "0", "endIndex": "5"},
                {"startIndex": "14", "endIndex": "18"},
            ]}},
        }]}}
        with self.assertRaisesRegex(GoogleBackendError, "response_document_validation_failed"):
            validate_response_pages(omitted, (1,))
        whitespace = {"document": {"text": "alpha beta", "pages": [{
            "pageNumber": 1, "layout": {"textAnchor": {"textSegments": [
                {"startIndex": "0", "endIndex": "5"},
                {"startIndex": "6", "endIndex": "10"},
            ]}},
        }]}}
        self.assertIs(validate_response_pages(whitespace, (1,)), whitespace["document"])


if __name__ == "__main__":
    unittest.main()
