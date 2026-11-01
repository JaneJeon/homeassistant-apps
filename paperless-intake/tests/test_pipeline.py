"""Pipeline and service tests with synthetic scans, a fake Google and an in-memory Paperless."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from paperless_intake.config import Settings
from paperless_intake.docai.google_backend import GoogleBackendConfig, GoogleDocumentAIBackend
from paperless_intake.docai.validation import inspect_source
from paperless_intake.pipeline import Pipeline
from paperless_intake.service import Service
from paperless_intake.store import (COMMITTED, DISCARDED, HELD, READY, WAITING, Store)

from fake_paperless import Crash, FakePaperless, RecordingNotifier
from synth import FONT, FakeGoogle, PageSpec, make_scan, numbered_letter, token_provider

VERSION = ("projects/paperlss-document-pipeline/locations/us/processors/unit/"
           "processorVersions/pretrained-ocr-v2.1-2024-08-07")


class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Harness:
    def __init__(self, test: unittest.TestCase, **overrides):
        self.root = Path(tempfile.mkdtemp(prefix="intake-test-"))
        test.addCleanup(shutil.rmtree, self.root, True)
        values = dict(
            paperless_url="http://paperless.invalid", paperless_token="x", google_service_account=None,
            processor_version=VERSION, physical_mail_tag="Physical Mail", intake_tag="Mail intake", ha_url="http://ha.invalid",
            webhook_started="s", webhook_finished="f", webhook_alert="a", daily_page_cap=200, min_free_mib=0,
            keep_local_copies_days=7, watchdog_minutes=45, capture_dir=self.root / "capture",
            intake_dir=self.root / "intake", font_path=FONT, tested_paperless_version="3.3.4",
            stable_seconds=30, poll_seconds=0,
        )
        values.update(overrides)
        self.settings = Settings(**values)
        self.settings.capture_dir.mkdir(parents=True)
        self.clock = Clock()
        self.google = FakeGoogle()
        self.paperless = FakePaperless()
        self.notifier = RecordingNotifier()
        self.build()

    def build(self):
        """(Re)create the app objects, as a restart would."""
        self.store = Store(self.settings.items_dir)

        def backend(item):
            return GoogleDocumentAIBackend(GoogleBackendConfig(VERSION, item.google_dir()),
                                           token_provider=token_provider, transport=self.google,
                                           wall_clock=self.clock, sleep=lambda s: None)

        self.pipeline = Pipeline(self.settings, self.store, self.paperless, self.notifier,
                                 token_provider=token_provider, backend_factory=backend, clock=self.clock)
        self.service = Service(self.settings, self.store, self.pipeline, self.notifier, clock=self.clock)

    def drop(self, scan, name="20261101_093000_000400.pdf") -> Path:
        path = self.settings.capture_dir / name
        path.write_bytes(scan.pdf)
        return path

    def settle(self, steps=10):
        """Let files settle and the worker run until there is nothing due."""
        self.service.claim_captures()
        self.clock.advance(31)
        for _ in range(steps):
            if not self.service.step():
                break

    def only_item(self):
        items = self.store.all()
        assert len(items) == 1, items
        return items[0]


class HappyPathTests(unittest.TestCase):
    def test_numbered_letter_commits_with_two_versions_tag_and_note(self):
        h = Harness(self)
        scan = numbered_letter(3)
        h.drop(scan)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual(len(doc["versions"]), 2)
        self.assertEqual(doc["versions"][-1]["data"], scan.pdf)                 # version 1 is the original
        self.assertEqual(doc["versions"][0]["checksum"], item.state["searchable_sha256"])
        self.assertEqual(doc["tags"], [1, 2])
        self.assertIsNone(doc["owner"])                    # visible to every Paperless user, like consume-folder scans
        self.assertEqual(len(doc["notes"]), 1)
        self.assertIn("confirmed by the printed page numbers", doc["notes"][0])
        self.assertEqual([kind for kind, _ in h.notifier.sent], ["started", "finished"])
        self.assertFalse((item.root / "google").exists())
        self.assertTrue((item.root / "google-summary.json.gz").exists())
        self.assertFalse((h.settings.capture_dir / "20261101_093000_000400.pdf").exists())
        self.assertEqual(h.google.calls, 1)

    def test_pages_out_of_order_are_reordered_by_printed_numbers(self):
        h = Harness(self)
        scan = make_scan([PageSpec(["First page body text for the reorder test"], footer="Page 1 of 3"),
                          PageSpec(["Third page body text for the reorder test"], footer="Page 3 of 3"),
                          PageSpec(["Second page body text for the reorder test"], footer="Page 2 of 3")])
        h.drop(scan)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["page_order"], [1, 3, 2])
        self.assertIn("reordered", h.paperless.docs[item.state["doc_id"]]["notes"][0])

    def test_nearly_blank_scan_commits_original_only(self):
        h = Harness(self)
        scan = make_scan([PageSpec(["Hi"])])
        h.drop(scan)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["mode"], "original_only")
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 1)

    def test_file_is_not_claimed_until_it_stops_changing(self):
        h = Harness(self)
        scan = numbered_letter(1)
        path = h.drop(scan)
        h.service.claim_captures()
        h.clock.advance(20)
        path.write_bytes(scan.pdf + b"\n")       # still being written
        h.service.claim_captures()
        h.clock.advance(20)
        self.assertEqual(h.service.claim_captures(), [])
        h.clock.advance(15)
        self.assertEqual(len(h.service.claim_captures()), 1)

    def test_same_bytes_again_go_to_duplicates(self):
        h = Harness(self)
        scan = numbered_letter(1)
        h.drop(scan)
        h.settle()
        h.drop(scan, "again.pdf")
        h.settle()
        self.assertEqual(len(h.store.all()), 1)
        self.assertEqual(len(list(h.settings.duplicates_dir.iterdir())), 1)
        self.assertEqual(len(h.paperless.docs), 1)


class HoldTests(unittest.TestCase):
    def test_missing_page_is_held_with_a_named_gap(self):
        h = Harness(self)
        h.drop(numbered_letter(3, missing=(2,)))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, HELD)
        self.assertIn("page 2 of 3 is missing", item.state["reason"])
        self.assertEqual(h.paperless.docs, {})
        alerts = [payload for kind, payload in h.notifier.sent if kind == "alert"]
        self.assertEqual(alerts[0]["kind"], "held")
        self.assertNotIn("Synthetic", json.dumps(alerts))                       # no document text in alerts
        self.assertIn(("finished", item.sha), h.notifier.sent)

    def test_keep_original_only_commits_version_one_with_the_gap_in_the_note(self):
        h = Harness(self)
        h.drop(numbered_letter(3, missing=(3,)))
        h.settle()
        item = h.only_item()
        h.pipeline.keep_original_only(item)
        self.assertEqual(item.status, READY)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED)
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual(len(doc["versions"]), 1)
        self.assertIn("Jane's choice", doc["notes"][0])
        self.assertIn("page 3 of 3", doc["notes"][0])
        self.assertEqual([kind for kind, _ in h.notifier.sent].count("finished"), 1)
        alerts = [(p["kind"], p["gap"]) for k, p in h.notifier.sent if k == "alert"]
        self.assertEqual([kind for kind, _ in alerts], ["held", "resolved"])   # Home Assistant can clear the hold
        self.assertEqual(alerts[-1][1], "committed")

    def test_discard_moves_the_record_aside(self):
        h = Harness(self)
        h.drop(numbered_letter(2, missing=(1,)))
        h.settle()
        item = h.only_item()
        target = h.pipeline.discard(item)
        self.assertEqual(h.store.all(), [])
        self.assertEqual(json.loads((target / "state.json").read_text())["status"], DISCARDED)
        alerts = [(p["kind"], p["gap"]) for k, p in h.notifier.sent if k == "alert"]
        self.assertEqual(alerts[-1], ("resolved", "discarded"))

    def test_not_a_pdf_is_held_after_five_minutes(self):
        h = Harness(self)
        (h.settings.capture_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff not really a pdf")
        h.service.claim_captures()
        h.clock.advance(31)
        self.assertEqual(h.service.claim_captures(), [])
        h.clock.advance(300)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, HELD)
        self.assertEqual(item.state["reason_kind"], "unsupported_file")


class ResumeTests(unittest.TestCase):
    def _claimed(self, h):
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        return h.store.all()[0]

    def test_crash_after_upload_resumes_without_a_second_document(self):
        h = Harness(self)
        h.paperless.crash_after.add("post_document")
        h.drop(numbered_letter(2))
        item = self._claimed(h)
        h.pipeline.prepare(item)
        with self.assertRaises(Crash):          # the app dies right after Paperless accepted the upload
            h.pipeline.commit(item)
        h.build()                               # restart
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(len(h.paperless.docs), 1)
        self.assertEqual(h.paperless.calls.count("post_document"), 1)
        self.assertIsNone(h.paperless.docs[item.state["doc_id"]]["owner"])   # cleared on resume too

    def test_crash_after_version_upload_resumes_without_a_second_version(self):
        h = Harness(self)
        h.paperless.crash_after.add("update_version")
        h.drop(numbered_letter(2))
        item = self._claimed(h)
        h.pipeline.prepare(item)
        with self.assertRaises(Crash):
            h.pipeline.commit(item)
        h.build()
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 2)
        self.assertEqual(h.paperless.calls.count("update_version"), 1)
        self.assertEqual(h.google.calls, 1)                                      # no second OCR bill

    def test_restart_after_ocr_reuses_the_saved_google_answer(self):
        h = Harness(self)
        h.drop(numbered_letter(2))
        item = self._claimed(h)
        h.pipeline.backend_factory(item).process(inspect_source(item.source_path), item.new_work_dir())
        h.build()
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)
        self.assertEqual(h.google.calls, 1)


class WaitTests(unittest.TestCase):
    def test_untested_paperless_version_waits_and_alerts_once(self):
        h = Harness(self)
        h.paperless.version = "3.4.0"
        h.drop(numbered_letter(1))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, WAITING)
        self.assertEqual(item.state["reason_kind"], "version_mismatch")
        self.assertNotIn("post_document", h.paperless.calls)
        h.clock.advance(31 * 60)
        h.settle()
        alerts = [p for k, p in h.notifier.sent if k == "alert"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("tested on 3.3.4", alerts[0]["gap"])
        h.paperless.version = "3.3.4"                  # Paperless put back on the tested version
        h.clock.advance(31 * 60)
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)
        self.assertEqual([p["kind"] for k, p in h.notifier.sent if k == "alert"], ["waiting", "resolved"])

    def test_pin_moving_ahead_keeps_the_previously_tested_version_working(self):
        h = Harness(self)
        h.drop(numbered_letter(1))
        h.settle()                                     # committed under 3.3.4 while the pin was 3.3.4
        h.settings = __import__("dataclasses").replace(h.settings, tested_paperless_version="3.3.5")
        h.build()                                      # new app image with the bumped pin; Paperless still 3.3.4
        h.drop(numbered_letter(2), "second.pdf")
        h.settle()
        self.assertEqual([i.status for i in h.store.all()], [COMMITTED, COMMITTED])
        h.paperless.version = "3.3.6"                  # never tested
        h.drop(numbered_letter(3), "third.pdf")
        h.settle()
        self.assertEqual(h.store.all()[-1].status, WAITING)

    def test_paperless_outage_waits_then_commits_once(self):
        h = Harness(self)
        h.paperless.unreachable = True
        h.drop(numbered_letter(1))
        h.settle()
        self.assertEqual(h.only_item().status, WAITING)
        h.paperless.unreachable = False
        h.clock.advance(31 * 60)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED)
        self.assertEqual(len(h.paperless.docs), 1)
        self.assertEqual([k for k, _ in h.notifier.sent if k == "alert"], [])   # a short outage stays quiet

    def test_google_errors_wait_then_retry_with_a_fresh_attempt_budget(self):
        h = Harness(self)
        h.google.fail = [503, 503, 503]
        h.drop(numbered_letter(1))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, WAITING)
        self.assertEqual(item.state["reason_kind"], "google")
        h.clock.advance(31 * 60)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["google_run"], 2)

    def test_gives_up_to_held_after_a_day(self):
        h = Harness(self)
        h.paperless.unreachable = True
        h.drop(numbered_letter(1))
        h.settle()
        for _ in range(50):
            h.clock.advance(31 * 60)
            h.settle()
        item = h.only_item()
        self.assertEqual(item.status, HELD)
        self.assertEqual(item.state["reason_kind"], "gave_up")
        self.assertTrue(h.pipeline.can_retry(item))

    def test_daily_page_cap(self):
        h = Harness(self, daily_page_cap=2)
        h.drop(numbered_letter(3))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, WAITING)
        self.assertEqual(item.state["reason_kind"], "page_cap")
        self.assertEqual(h.google.calls, 0)

    def test_no_finished_webhook_after_the_watchdog_gave_up(self):
        h = Harness(self)
        h.paperless.unreachable = True
        h.drop(numbered_letter(1))
        h.settle()
        h.paperless.unreachable = False
        h.clock.advance(60 * 60)
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)
        self.assertNotIn("finished", [kind for kind, _ in h.notifier.sent])


class TidyTests(unittest.TestCase):
    def test_local_copies_dropped_after_keep_days(self):
        h = Harness(self)
        h.drop(numbered_letter(1))
        h.settle()
        item = h.only_item()
        h.clock.advance(6 * 86400)
        h.service.tidy()
        self.assertTrue(item.source_path.exists())
        h.clock.advance(2 * 86400)
        h.service.tidy()
        item = h.only_item()
        self.assertFalse(item.source_path.exists())
        self.assertTrue((item.root / "state.json").exists())
        self.assertTrue(item.state["local_copies_removed_at"])


if __name__ == "__main__":
    unittest.main()
