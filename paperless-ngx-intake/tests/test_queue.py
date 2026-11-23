"""The job queue: what may start now, leases, and what is owed after a kill. Synthetic scans and a stand-in Paperless."""

from __future__ import annotations

import json
import os
import time
import unittest
import unittest.mock
from pathlib import Path

import requests

from paperless_intake.config import load
from paperless_intake.documents import DONE, GAVE_UP, LATER, WORKING
from paperless_intake.gate import describe_memory, pressure
from paperless_intake.jobs import RECHECK_SECONDS, RESTART_BOUND
from paperless_intake.paperless import (BUSY, LOGIN, NO_TAG, NOT_FOUND, REFUSED, TIMEOUT, UNREACHABLE, Paperless,
                                       PaperlessError)
from paperless_intake.store import COMMITTED, HELD, WAITING
from paperless_intake.usage import GoogleUsage, day_of, month_of, next_midnight

from synth import make_scan
from test_documents import alert_kinds, lease, letter, look
from test_pipeline import Harness

HOUR = 3600


def webhooks(h, kind):
    return [entry for entry in h.notifier.sent if entry[0] == kind]


class TypedPaperlessErrorTests(unittest.TestCase):
    """A failure that is about Paperless itself is told apart from one about a document."""

    class Session:
        def __init__(self, outcome):
            self.outcome = outcome
            self.headers = {}

        def request(self, *args, **kwargs):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            response = requests.Response()
            response.status_code = self.outcome
            return response

    def kind(self, outcome):
        with self.assertRaises(PaperlessError) as caught:
            Paperless("http://paperless.invalid", "x", session=self.Session(outcome)).document(1)
        return caught.exception.kind, caught.exception.outside

    def test_each_kind_of_failure(self):
        self.assertEqual(self.kind(requests.ConnectionError()), (UNREACHABLE, True))
        self.assertEqual(self.kind(requests.ReadTimeout()), (TIMEOUT, True))
        self.assertEqual(self.kind(requests.ConnectTimeout()), (TIMEOUT, True))
        for status in (502, 503, 504):
            self.assertEqual(self.kind(status), (BUSY, True))
        self.assertEqual(self.kind(401), (LOGIN, True))                    # every request would fail the same way
        self.assertEqual(self.kind(404), (NOT_FOUND, False))
        for status in (400, 403, 500):
            self.assertEqual(self.kind(status), (REFUSED, False))
        self.assertTrue(PaperlessError("no tag", NO_TAG).outside)

    def test_whether_paperless_can_have_taken_anything_from_a_failed_request_is_known(self):
        from urllib3.exceptions import MaxRetryError, NewConnectionError
        refused = requests.ConnectionError(MaxRetryError(None, "/", reason=NewConnectionError(None, "refused")))

        def answered(outcome):
            with self.assertRaises(PaperlessError) as caught:
                Paperless("http://paperless.invalid", "x", session=self.Session(outcome)).document(1)
            return caught.exception.answered
        self.assertIs(answered(refused), False)                             # never reached Paperless
        self.assertIs(answered(requests.ConnectTimeout()), False)
        self.assertIsNone(answered(requests.ReadTimeout()))                 # sent; no answer came
        self.assertIsNone(answered(requests.ConnectionError("reset")))
        self.assertIsNone(answered(502))                                    # a proxy may have passed it on
        self.assertIs(answered(400), True)
        self.assertIs(answered(500), True)

    def test_an_upload_waits_minutes_for_paperless_to_answer_and_other_calls_a_minute(self):
        seen = []

        class Session(self.Session):
            def request(self, method, url, **kwargs):
                seen.append(kwargs["timeout"])
                response = requests.Response()
                response.status_code = 200
                response._content = b'"task-1"'
                return response

        paperless = Paperless("http://paperless.invalid", "x", session=Session(200))
        paperless.update_version(1, "a.pdf", b"%PDF-", label="x")
        paperless.post_document("a.pdf", b"%PDF-", tag_ids=[1])
        paperless._request("GET", "/api/tags/")
        self.assertEqual(seen, [(10.0, 600.0), (10.0, 600.0), 60.0])


class MemoryDiskAndPressureTests(unittest.TestCase):
    """Nothing starts while the Green is short. A scan held back waits as a scan waits; a stored document isn't touched."""

    def test_a_scan_held_back_for_memory_waits_without_a_try_and_goes_in_when_memory_is_back(self):
        h = Harness(self)
        h.green(available_mib=300)
        h.drop(make_scan(letter(2)))
        h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"], item.state["waits_on"]), (WAITING, "memory_low", "memory"))
        self.assertIn("only 300 MiB of memory available", item.state["reason"])
        self.assertEqual(item.state["next_attempt_at_unix"], h.clock() + RECHECK_SECONDS)
        self.assertEqual((h.google.calls, alert_kinds(h)), (0, []))         # not started, and nothing to tell Jane yet
        self.assertEqual(h.service.queue.held["scan"].kind, "memory_low")
        h.green(available_mib=2048)
        h.clock.advance(RECHECK_SECONDS)
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)
        self.assertEqual([kind for kind, _ in h.notifier.sent], ["started", "finished"])

    def test_a_scan_the_gate_holds_for_a_day_is_held_for_jane_with_an_alert(self):
        h = Harness(self)
        h.green(available_mib=300)
        h.drop(make_scan(letter(1)))
        h.settle()
        for _ in range(int(25 * HOUR / RECHECK_SECONDS)):
            h.clock.advance(RECHECK_SECONDS)
            h.service.step()
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"]), (HELD, "gave_up"))
        self.assertIn("kept failing for 24 hours: only 300 MiB", item.state["reason"])
        self.assertEqual(alert_kinds(h), ["held"])

    def test_a_stored_document_is_not_leased_marked_or_counted_while_memory_is_short(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        h.green(available_mib=300)
        for _ in range(5):
            look(h)
        self.assertFalse(h.documents.exists(doc_id))                       # never marked: nothing to count
        self.assertEqual((h.google.calls, h.paperless.calls.count("download_original")), (0, 0))
        h.green(available_mib=2048)
        look(h)
        record = h.documents.load(doc_id)
        self.assertEqual((record.status, record.state["tries"]), (DONE, {}))

    def test_disk_low_holds_everything_before_a_worker_starts(self):
        h = Harness(self, min_free_mib=10 ** 9)
        h.drop(make_scan(letter(1)))
        h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"], item.state["waits_on"]), (WAITING, "disk_low", "disk"))
        self.assertEqual(h.google.calls, 0)

    def test_a_saturated_disk_or_memory_stalls_hold_scans_too(self):
        for io, memory, kind in ((60, 0, "io_pressure"), (0, 40, "memory_pressure")):
            h = Harness(self)
            h.green(io=io, memory=memory)
            h.drop(make_scan(letter(1)))
            h.settle()
            item = h.only_item()
            self.assertEqual((item.status, item.state["reason_kind"]), (WAITING, kind))
            self.assertIn("of the last minute", item.state["reason"])
            h.green(io=io - 1 if io else 0, memory=memory - 1 if memory else 0)   # just under the bar
            h.clock.advance(RECHECK_SECONDS)
            h.settle()
            self.assertEqual(h.only_item().status, COMMITTED)

    def test_without_psi_the_gate_goes_by_memory_alone_and_says_so_at_start(self):
        h = Harness(self)
        h.green(psi=False)
        self.assertIsNone(pressure("io", h.settings.pressure_dir))
        line, warnings = describe_memory(h.settings.meminfo_path, limits=(h.root / "nope",),
                                         pressure_root=h.settings.pressure_dir)
        self.assertIn("pressure files: none", line)
        self.assertTrue(any("goes by MemAvailable alone" in warning for warning in warnings))
        h.drop(make_scan(letter(1)))
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)

    def test_the_start_up_check_warns_when_the_container_cant_be_seeing_the_green(self):
        h = Harness(self)
        limit = h.root / "memory.max"
        limit.write_text("max\n")
        line, warnings = describe_memory(h.settings.meminfo_path, limits=(limit,), pressure_root=h.settings.pressure_dir)
        self.assertIn("MemTotal 3919 MiB", line)
        self.assertEqual(warnings, [])
        limit.write_text(str(512 * 1024 * 1024))
        h.settings.meminfo_path.write_text("MemTotal: 1048576 kB\nMemAvailable: 524288 kB\n")
        _, warnings = describe_memory(h.settings.meminfo_path, limits=(limit,), pressure_root=h.settings.pressure_dir)
        self.assertEqual(len(warnings), 2)                                  # too little memory, and a limit

    def test_every_kind_of_hold_the_gate_records_is_one_it_knows_as_its_own(self):
        from paperless_intake.pipeline import GATE_KINDS
        h = Harness(self, min_free_mib=0)
        kinds = set()
        for green in (dict(available_mib=300), dict(io=70), dict(memory=50)):
            h.green(available_mib=2048)
            h.green(**green)
            kinds.add(h.service.queue._resources().kind)
        h.pipeline.intent.open(what="original", sha256="0" * 64, name="x.pdf")
        kinds.add(h.service.queue._uploads().kind)
        self.assertLessEqual(kinds | {"disk_low"}, GATE_KINDS)

    def test_what_the_gate_reads_is_logged_once_an_hour(self):
        h = Harness(self)
        h.green(available_mib=900, io=12.5, memory=3)
        with self.assertLogs("paperless_intake.jobs", "INFO") as logged:
            for _ in range(5):
                h.service.step()
                h.clock.advance(60)
            h.clock.advance(3600)
            h.service.step()
        readings = [line for line in logged.output if "the Green: I/O pressure" in line]
        self.assertEqual(len(readings), 2)
        self.assertIn("I/O pressure 12.5%, memory pressure 3.0% (some, avg60), MemAvailable 900 MiB", readings[0])

    def test_a_scan_held_by_the_gate_goes_before_any_stored_document(self):
        for green in (dict(available_mib=300), dict(io=70)):
            with self.subTest(**green):
                self.scan_first(green)

    def scan_first(self, green):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1, "old")).pdf)
        h.green(**green)
        h.drop(make_scan(letter(1, "new")))
        h.settle()
        h.green(available_mib=2048, io=0)
        h.clock.advance(RECHECK_SECONDS - 1)                                # the Green is fine before the scan's next look
        h.service.step()
        self.assertFalse(h.documents.exists(doc_id))                       # the stored one doesn't jump the queue
        self.assertEqual(h.service.queue.held["document"].kind, "scan_first")
        h.clock.advance(RECHECK_SECONDS)
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)
        look(h)
        self.assertEqual(h.documents.load(doc_id).status, DONE)


class OutsideWaitTests(unittest.TestCase):
    """While a stored document waits on Paperless or Google, no other stored document starts."""

    def test_paperless_down_pauses_stored_documents_and_the_waiting_one_is_the_check(self):
        h = Harness(self)
        first = h.paperless.add_old_scan(make_scan(letter(1, "a")).pdf)
        second = h.paperless.add_old_scan(make_scan(letter(1, "b")).pdf)
        h.paperless.fail_once["download_original"] = PaperlessError("Paperless unreachable: ConnectionError",
                                                                    UNREACHABLE)
        h.settle()
        record = h.documents.load(first)
        self.assertEqual((record.status, record.state["waits_on"], record.state["tries"]["count"]),
                         (LATER, "paperless", 1))
        for _ in range(5):
            look(h)                                                         # 25 minutes: still within its hour
        self.assertFalse(h.documents.exists(second))
        self.assertNotIn("documents", h.paperless.calls[h.paperless.calls.index("download_original") + 1:])
        look(h, HOUR)
        self.assertEqual(h.documents.load(first).status, DONE)
        look(h)
        self.assertEqual(h.documents.load(second).status, DONE)

    def test_google_down_pauses_stored_documents_too(self):
        h = Harness(self)
        first = h.paperless.add_old_scan(make_scan(letter(1, "a")).pdf)
        second = h.paperless.add_old_scan(make_scan(letter(1, "b")).pdf)
        h.google.fail = [503] * 50
        h.settle()
        self.assertEqual(h.documents.load(first).state["waits_on"], "google")
        h.google.fail = []
        look(h)
        self.assertFalse(h.documents.exists(second))

    def test_a_fault_about_one_document_pauses_nothing(self):
        h = Harness(self)
        first = h.paperless.add_old_scan(make_scan(letter(1, "a")).pdf)
        second = h.paperless.add_old_scan(make_scan(letter(1, "b")).pdf)
        h.paperless.fail_once["download_original"] = PaperlessError("HTTP 500", REFUSED)
        h.settle()
        record = h.documents.load(first)
        self.assertEqual((record.status, record.state.get("waits_on"), record.state["tries"]["odd"]), (LATER, None, 1))
        look(h)
        self.assertEqual(h.documents.load(second).status, DONE)

    def test_a_document_paperless_says_is_gone_is_given_up_at_once(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        h.paperless.fail_once["download_original"] = PaperlessError("HTTP 404", NOT_FOUND)
        h.settle()
        self.assertEqual(h.documents.load(doc_id).status, GAVE_UP)


class UploadInDoubtTests(unittest.TestCase):
    """An upload that timed out may still land. Nothing else is sent until Paperless shows how it ended."""

    def test_a_version_upload_that_timed_out_lands_later_and_is_not_sent_twice(self):
        h = Harness(self)
        first = h.paperless.add_old_scan(make_scan(letter(1, "a")).pdf)
        second = h.paperless.add_old_scan(make_scan(letter(1, "b")).pdf)
        h.paperless.slow_uploads = True
        h.paperless.upload_times_out = True
        h.settle()
        record = h.documents.load(first)
        self.assertEqual((record.status, record.state["waits_on"]), (LATER, "paperless"))
        self.assertIsNotNone(h.pipeline.intent.read())
        h.drop(make_scan(letter(1, "new")))
        for _ in range(3):
            look(h, RECHECK_SECONDS)                                        # Paperless is still taking it in
        self.assertEqual(h.only_item().status, WAITING)                     # a new scan waits too: it would upload
        self.assertEqual(h.only_item().state["reason_kind"], "upload_in_doubt")
        self.assertFalse(h.documents.exists(second))
        self.assertEqual(h.paperless.calls.count("update_version"), 1)
        h.paperless.slow_uploads = False
        h.paperless.land()                                                  # it lands, minutes late
        look(h, RECHECK_SECONDS)
        self.assertIsNone(h.pipeline.intent.read())
        self.assertEqual(h.only_item().status, COMMITTED)
        look(h, HOUR)
        look(h)
        self.assertEqual([h.documents.load(doc_id).status for doc_id in (first, second)], [DONE, DONE])
        self.assertEqual(len(h.paperless.docs[first]["versions"]), 2)      # one version, not two
        self.assertEqual(h.paperless.calls.count("update_version"), 3)     # the first, the new scan's, the second's

    def test_a_scan_whose_upload_timed_out_becomes_one_document(self):
        h = Harness(self)
        h.paperless.slow_uploads = True
        h.paperless.upload_times_out = True
        h.drop(make_scan(letter(1)))
        h.settle()
        self.assertEqual(h.only_item().status, WAITING)
        h.clock.advance(31 * 60)
        h.settle()                                                          # its retry time comes; Paperless hasn't filed it
        self.assertEqual(h.paperless.calls.count("post_document"), 1)
        h.paperless.slow_uploads = False
        h.paperless.land()
        h.clock.advance(31 * 60)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED)
        self.assertEqual(len(h.paperless.docs), 1)
        self.assertEqual(h.paperless.calls.count("post_document"), 1)

    def test_an_upload_paperless_never_got_is_sent_again_once_paperless_has_nothing_left(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        h.pipeline.intent.open(what="version", sha256="0" * 64, name="lost.pdf", doc_id=doc_id)
        h.settle()
        self.assertFalse(h.documents.exists(doc_id))                       # not yet: it might still land
        h.clock.advance(5 * 60)
        look(h)
        self.assertIsNone(h.pipeline.intent.read())
        self.assertEqual(h.documents.load(doc_id).status, DONE)

    def test_a_failed_task_settles_the_mark_and_the_job_follows_its_own_rules(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        h.paperless.tasks["t-failed"] = {"status": "failure", "result": "it broke"}
        h.pipeline.intent.open(what="version", sha256="0" * 64, name="broken.pdf", doc_id=doc_id)
        h.pipeline.intent.task("t-failed")
        h.settle()
        self.assertIsNone(h.pipeline.intent.read())
        self.assertEqual(h.documents.load(doc_id).status, DONE)

    def test_paperless_not_answering_about_an_upload_is_asked_less_and_less_often(self):
        h = Harness(self)
        h.pipeline.intent.open(what="original", sha256="0" * 64, name="scan.pdf")
        h.paperless.unreachable = True
        for _ in range(60):
            h.clock.advance(60)
            h.service.step()
        asked = h.paperless.calls.count("server_version")
        self.assertLessEqual(asked, 6)                                      # 2, 4, 8, 16, 30, 30 minutes: never a spin
        self.assertTrue(h.service.queue.held["scan"] is None and h.pipeline.intent.read() is not None)


class UploadAnsweredTests(unittest.TestCase):
    def test_an_upload_paperless_answered_with_an_error_leaves_no_mark(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)

        def refuse(*args, **kwargs):
            raise PaperlessError("Paperless POST returned HTTP 400", REFUSED)
        h.paperless.update_version = refuse
        h.settle()
        self.assertIsNone(h.pipeline.intent.read())
        self.assertEqual(h.documents.load(doc_id).state["tries"]["odd"], 1)


class StuckTaskTests(unittest.TestCase):
    """Paperless's worker can die while it takes a file in (a power cut), and leaves the task unfinished for good."""

    LIMIT = 1800 + 15 * 60                                                 # Paperless's time limit, and the margin

    def test_a_task_left_started_is_taken_as_lost_after_paperlesss_time_limit(self):
        h = Harness(self)
        h.paperless.slow_uploads = True                                     # Paperless answers with a task, then
        h.drop(make_scan(letter(1)))                                        # doesn't finish it in time
        h.settle()
        self.assertTrue(h.pipeline.intent.read()["task"])
        [task] = [name for name, task in h.paperless.tasks.items() if task["status"] == "pending"]
        h.paperless.start(task)                                             # its worker picks it up, then dies
        h.paperless.pending = []                                            # and the file is gone
        h.paperless.slow_uploads = False
        h.clock.advance(self.LIMIT - 120)
        h.settle()
        self.assertIsNotNone(h.pipeline.intent.read())                      # not yet: it could still finish
        self.assertEqual(h.paperless.calls.count("post_document"), 1)
        h.clock.advance(240)
        h.settle()
        self.assertIsNone(h.pipeline.intent.read())
        h.clock.advance(31 * 60)
        h.settle()
        self.assertEqual((h.only_item().status, len(h.paperless.docs)), (COMMITTED, 1))
        self.assertEqual(h.paperless.calls.count("post_document"), 2)       # sent again once, not waited on for ever
        self.assertIn("past Paperless's own time limit",
                      [e for e in h.only_item().events() if e["event"] == "upload_settled"][0]["how"])

    def test_an_upload_queued_40_minutes_then_running_10_is_sent_once(self):
        h = Harness(self)
        h.paperless.slow_uploads = True
        h.drop(make_scan(letter(1)))
        h.settle()
        [task] = [name for name, task in h.paperless.tasks.items() if task["status"] == "pending"]
        h.clock.advance(40 * 60)
        h.paperless.start(task)                                             # a long queue, then its turn
        h.settle()
        h.clock.advance(6 * 60)                                             # 46 minutes after it was queued
        h.settle()
        self.assertIsNotNone(h.pipeline.intent.read())                      # still running: not lost
        h.clock.advance(4 * 60)
        h.paperless.slow_uploads = False
        h.paperless.land()                                                  # it finishes after 10 minutes' work
        h.settle()
        h.clock.advance(31 * 60)
        h.settle()
        self.assertEqual((h.only_item().status, len(h.paperless.docs)), (COMMITTED, 1))
        self.assertEqual(h.paperless.calls.count("post_document"), 1)

    def test_a_started_task_that_doesnt_say_when_it_started_gets_only_the_long_limit(self):
        from paperless_intake.gate import _lost
        task = {"status": "started", "created": 0.0, "started": None}
        self.assertFalse(_lost(task, self.LIMIT + 60, self.LIMIT, 0.0)[0])
        self.assertTrue(_lost(task, 6 * HOUR, self.LIMIT, 0.0)[0])
        self.assertTrue(_lost(dict(task, started=10.0), self.LIMIT + 10, self.LIMIT, 0.0)[0])
        self.assertFalse(_lost(dict(task, status="pending"), self.LIMIT + 60, self.LIMIT, 0.0)[0])

    def test_a_task_still_pending_waits_six_hours_because_a_long_queue_is_not_a_dead_worker(self):
        h = Harness(self)
        h.paperless.slow_uploads = True
        h.drop(make_scan(letter(1)))
        h.settle()
        [task] = [name for name, task in h.paperless.tasks.items() if task["status"] == "pending"]
        h.clock.advance(self.LIMIT + 60)                                    # a started task would be lost by now
        h.settle()
        self.assertIsNotNone(h.pipeline.intent.read())
        self.assertEqual(h.paperless.calls.count("post_document"), 1)
        h.paperless.pending = []                                            # its queue message was lost
        h.paperless.slow_uploads = False
        h.clock.advance(6 * HOUR)
        h.settle()
        self.assertIsNone(h.pipeline.intent.read())
        h.clock.advance(31 * 60)
        h.settle()
        self.assertEqual((h.only_item().status, len(h.paperless.docs)), (COMMITTED, 1))
        self.assertIn("its queue message must have been lost",
                      [e for e in h.only_item().events() if e["event"] == "upload_settled"][0]["how"])

    def test_without_a_task_id_only_tasks_made_after_the_mark_count_and_none_for_ever(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        h.paperless.tasks["stuck-before"] = {"status": "started", "created": h.clock() - 10 * HOUR,
                                             "type": "consume_file"}       # stuck since long before
        h.pipeline.intent.open(what="version", sha256="0" * 64, name="lost.pdf", doc_id=doc_id)
        h.clock.advance(60)
        h.paperless.tasks["stuck-after"] = {"status": "started", "created": h.clock(), "started": h.clock(),
                                            "type": "consume_file"}
        look(h)
        self.assertIsNotNone(h.pipeline.intent.read())                      # one made after the mark might be ours
        h.clock.advance(self.LIMIT)
        look(h)
        self.assertIsNone(h.pipeline.intent.read())                         # but not once it is past the limit
        self.assertEqual(h.documents.load(doc_id).status, DONE)

    def test_a_file_paperless_never_takes_in_three_times_holds_the_scan_with_an_alert(self):
        h = Harness(self)
        h.paperless.slow_uploads = True
        h.drop(make_scan(letter(1)))
        for _ in range(3):
            h.paperless.upload_times_out = True
            h.settle()
            h.paperless.pending = []                                        # Paperless never files it
            for task in h.paperless.tasks.values():
                if task["status"] == "pending":
                    task.update(status="started", started=h.clock())
            h.clock.advance(self.LIMIT + 60)
            h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"]), (HELD, "gave_up"))
        self.assertIn("Paperless never finished taking this file in (3 times)", item.state["reason"])
        self.assertEqual(alert_kinds(h), ["held"])
        self.assertEqual(h.paperless.calls.count("post_document"), 3)
        self.assertEqual(json.loads((h.settings.intake_dir / "upload-strikes.json").read_text()), {})

    def test_a_stored_scan_whose_file_paperless_never_takes_in_is_set_aside_without_an_alert(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        for _ in range(3):
            lease(h)
            record = h.documents.load(doc_id)
            h.pipeline.intent.open(what="version", sha256="0" * 64, name="v.pdf", doc_id=doc_id)
            h.service.queue.forget_upload()
            if h.documents.load(doc_id).status == WORKING:
                h.documents.run(h.documents.load(doc_id))
                record = h.documents.load(doc_id)
            h.clock.advance(HOUR)
        record = h.documents.load(doc_id)
        self.assertEqual(record.status, GAVE_UP)
        self.assertIn("Paperless never finished taking this file in", record.state["reason"])
        self.assertEqual(h.notifier.sent, [])

    def test_forgetting_an_upload_on_the_scans_page_settles_it_at_once_and_is_logged(self):
        from paperless_intake.web import Page
        h = Harness(self)
        h.pipeline.intent.open(what="original", sha256="0" * 64, name="scan.pdf")
        page = Page(h.service, h.pipeline, h.settings)
        self.assertIn("Forget this upload", page.render())
        with self.assertLogs("paperless_intake.jobs", "WARNING") as logged:
            page.forget_upload()
        self.assertIn("Jane chose to forget the upload of scan.pdf", logged.output[0])
        self.assertIsNone(h.pipeline.intent.read())
        self.assertNotIn("Forget this upload", page.render())
        self.assertEqual(json.loads((h.settings.intake_dir / "upload-strikes.json").read_text()), {"0" * 64: 1})
        with self.assertRaises(ValueError):
            page.forget_upload()


class PaceTests(unittest.TestCase):
    def test_two_minutes_pass_between_stored_documents_but_not_between_scans(self):
        h = Harness(self, pace_seconds=120)
        first = h.paperless.add_old_scan(make_scan(letter(1, "a")).pdf)
        second = h.paperless.add_old_scan(make_scan(letter(1, "b")).pdf)
        look(h)
        self.assertEqual((h.documents.load(first).status, h.documents.exists(second)), (DONE, False))
        self.assertEqual(h.service.queue.held["document"].kind, "pace")
        h.drop(make_scan(letter(1, "new")))
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)                   # a scan isn't paced
        h.clock.advance(120)
        h.settle()
        self.assertEqual(h.documents.load(second).status, DONE)


class LeaseTests(unittest.TestCase):
    def test_a_job_in_hand_when_the_app_was_killed_is_resumed_and_not_counted(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        record = lease(h)
        self.assertEqual((record.status, record.state["lease"]["run"]), (WORKING, h.service.queue.run_id))
        h.restart()                                                         # killed with it in hand
        h.settle()
        record = h.documents.load(doc_id)
        self.assertEqual((record.status, record.state["tries"], "lease" in record.state), (DONE, {}, False))

    def test_restarts_that_keep_getting_further_never_set_a_job_aside(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        for step in range(RESTART_BOUND + 3):
            record = lease(h)
            self.assertIsNotNone(record)
            record.state["kind"] = f"step {step}"                          # a step forward each time before the kill
            record.save()
            h.build()
        self.assertEqual(h.documents.load(doc_id).status, WORKING)

    def test_a_scan_that_keeps_taking_the_app_down_is_held_for_jane(self):
        h = Harness(self)
        h.drop(make_scan(letter(1)))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        for _ in range(RESTART_BOUND + 1):
            h.service.queue.next()
            h.build()
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"]), (HELD, "gave_up"))
        self.assertIn(f"the app stopped {RESTART_BOUND} times", item.state["reason"])
        self.assertEqual(alert_kinds(h), ["held"])


class Killed(BaseException):
    """The whole app dies here."""


class RestartBoundTests(unittest.TestCase):
    """A job that keeps taking the app down is set aside, however the restarts are spaced."""

    def test_a_wait_doesnt_read_as_a_step_forward(self):
        from paperless_intake.jobs import Job, Queue
        h = Harness(self)
        h.drop(make_scan(letter(1)))
        h.settle()
        item = h.only_item()
        state = dict(item.state, searchable_sha256="s", doc_id=None, upload_task=None, version_task=None)
        marks = {Queue.progress(Job("scan", type(item)(item.root, dict(state, status=status, retry_stage=stage))))
                 for status, stage in (("ready", None), ("committing", None), ("waiting", "commit"))}
        self.assertEqual(len(marks), 1)
        marks = {Queue.progress(Job("scan", type(item)(item.root, dict(state, status=status, retry_stage=stage))))
                 for status, stage in (("captured", None), ("waiting", "ocr"))}
        self.assertEqual(len(marks), 1)

    def test_restarts_with_a_gate_hold_between_some_of_them_still_reach_the_bound(self):
        h = Harness(self)

        def dies(*args, **kwargs):
            raise Killed("the app goes down as the scan goes into Paperless")
        h.paperless.server_version = dies
        h.drop(make_scan(letter(1)))
        with self.assertRaises(Killed):
            h.settle()
        for n in range(3 * RESTART_BOUND):
            if h.only_item().status == HELD:
                break
            h.restart()
            if n % 2:                                                       # every other time the gate holds it first
                h.green(available_mib=300)
                h.service.step()
                h.green(available_mib=2048)
                h.clock.advance(RECHECK_SECONDS)
            try:
                h.service.step()                                            # dies, or the bound sets it aside
            except Killed:
                pass
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"]), (HELD, "gave_up"))
        self.assertIn(f"the app stopped {RESTART_BOUND} times", item.state["reason"])
        self.assertEqual(alert_kinds(h), ["held"])

    def test_a_scan_whose_upload_takes_the_app_down_is_set_aside_through_the_intent_hold(self):
        h = Harness(self)
        restarts = []

        def dies(*args, **kwargs):
            restarts.append(h.only_item().state["lease"]["restarts"])
            raise Killed("the app goes down as the file goes out")
        h.paperless.post_document = dies
        h.drop(make_scan(letter(1)))
        with self.assertRaises(Killed):
            h.settle()
        for _ in range(3 * RESTART_BOUND):
            if h.only_item().status == HELD:
                break
            h.restart()
            try:
                for _ in range(10):                                         # the intent hold, then the scan again
                    h.clock.advance(6 * 60)
                    h.service.step()
            except Killed:
                pass
        item = h.only_item()
        self.assertEqual(item.status, HELD)
        self.assertIn("Paperless never finished taking this file in (3 times)", item.state["reason"])
        self.assertEqual(restarts, [0, 0, 1])                               # counted across the holds, not reset
        self.assertEqual(alert_kinds(h), ["held"])


class WebhookTests(unittest.TestCase):
    """started and finished stay paired across kills; alerts are owed until Home Assistant takes them."""

    def claimed(self, h):
        h.drop(make_scan(letter(1)))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        return h.only_item()

    def test_started_is_never_sent_twice_even_when_the_app_died_while_sending_it(self):
        h = Harness(self)
        item = self.claimed(h)
        item.state.pop("started_sent")                                      # as if killed between sending and saving
        item.save()
        h.restart()
        h.settle()
        self.assertEqual(([kind for kind, _ in h.notifier.sent]), ["started", "finished"])

    def test_finished_is_sent_after_a_restart_when_the_app_died_before_sending_it(self):
        h = Harness(self)
        h.drop(make_scan(letter(1)))
        real = h.pipeline.settle
        h.pipeline.settle = lambda item: None                               # the scan ends; the app dies before telling
        h.settle()
        h.pipeline.settle = real
        self.assertEqual([kind for kind, _ in h.notifier.sent], ["started"])
        h.restart()
        h.restart()
        self.assertEqual([kind for kind, _ in h.notifier.sent], ["started", "finished"])

    def test_a_scan_claimed_just_before_a_kill_still_gets_its_started_and_finished(self):
        h = Harness(self)
        item = self.claimed(h)
        h.notifier.sent.clear()
        for key in ("started_sent", "started_sending", "started_at_unix"):
            item.state.pop(key, None)
        item.save()
        h.restart()
        h.settle()
        self.assertEqual([kind for kind, _ in h.notifier.sent], ["started", "finished"])

    def test_an_alert_home_assistant_didnt_take_is_sent_once_it_does(self):
        h = Harness(self)
        h.notifier.down = True
        h.drop(numbered_letter_missing_a_page())
        h.settle()
        item = h.only_item()
        self.assertEqual((item.status, [entry["kind"] for entry in item.state["outbox"]]), (HELD, ["held"]))
        h.notifier.down = False
        h.service.tidy()
        self.assertEqual((alert_kinds(h), h.only_item().state["outbox"]), (["held"], []))

    def test_an_empty_webhook_id_sends_nothing_and_owes_nothing(self):
        from paperless_intake.notify import Notifier
        notifier = Notifier("http://ha.invalid", started="", finished="", alert="")
        h = Harness(self)
        h.notifier = notifier
        h.build()
        h.drop(numbered_letter_missing_a_page())
        h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["started_sent"], item.state.get("outbox")), (HELD, False, []))


def numbered_letter_missing_a_page():
    from synth import PageSpec
    return make_scan([PageSpec(["First page body text for the hold test"], footer="Page 1 of 3"),
                      PageSpec(["Third page body text for the hold test"], footer="Page 3 of 3")])


class GooglePagesTests(unittest.TestCase):
    def setUp(self):
        self.tz = os.environ.get("TZ")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        time.tzset()

    def test_today_and_this_month_are_local_and_each_answer_counts_once(self):
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        at = [time.mktime((2026, 11, 30, 15, 0, 0, 0, 0, -1))]             # 3 PM local on the 30th: 11 PM UTC
        h = Harness(self)
        usage = GoogleUsage(h.root / "usage.json", lambda: at[0])
        self.assertEqual(usage.count([("answer-a", 3), ("answer-b", 2)]), 5)
        self.assertEqual(usage.count([("answer-a", 3)]), 0)                 # a resumed job reads the same answer
        at[0] += 2 * HOUR                                                   # 5 PM local: a new UTC day, the same local one
        self.assertEqual((usage.today(), usage.this_month(), day_of(at[0])), (5, 5, "2026-11-30"))
        at[0] = next_midnight(at[0]) + 1                                    # December 1st, local
        self.assertEqual((usage.today(), usage.this_month(), month_of(at[0])), (0, 0, "2026-12"))
        usage.count([("answer-c", 4)])
        self.assertEqual((usage.today(), usage.this_month()), (4, 4))

    def test_a_count_written_by_an_earlier_release_is_read(self):
        h = Harness(self)
        path = h.root / "usage.json"
        today = time.strftime("%Y-%m-%d", time.localtime(h.clock()))
        path.write_text(json.dumps({today: 12}))
        usage = GoogleUsage(path, h.clock)
        self.assertEqual((usage.today(), usage.this_month()), (12, 12))
        usage.count([("answer", 3)])
        self.assertEqual((usage.today(), usage.this_month()), (15, 15))

    def test_waiting_for_tomorrows_pages_ends_just_after_local_midnight(self):
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        h = Harness(self, daily_page_cap=4)                                 # three pages a day for stored documents
        h.clock.t = time.mktime((2026, 11, 30, 15, 0, 0, 0, 0, -1))
        first = h.paperless.add_old_scan(make_scan(letter(3, "a")).pdf)
        second = h.paperless.add_old_scan(make_scan(letter(1, "b")).pdf)
        look(h)
        look(h)
        record = h.documents.load(second)
        self.assertEqual((h.documents.load(first).status, record.status), (DONE, LATER))
        self.assertEqual(record.state["not_before_unix"], time.mktime((2026, 12, 1, 0, 1, 0, 0, 0, -1)))


class TagAndOptionTests(unittest.TestCase):
    def test_a_missing_scanner_tag_at_start_is_a_wait_never_a_crash(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1, "old")).pdf)
        number = h.paperless.tags.pop("Scanner")
        h.restart()
        h.drop(make_scan(letter(1, "new")))
        for _ in range(3):
            look(h)
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"], item.state["waits_on"]), (WAITING, "paperless", "paperless"))
        self.assertIn("'Scanner'", item.state["reason"])
        self.assertEqual(alert_kinds(h), ["waiting"])                       # Jane is told once
        self.assertFalse(h.documents.exists(doc_id))
        h.paperless.tags["Scanner"] = number                                # the shipping step renames the tag
        h.clock.advance(31 * 60)
        look(h)
        look(h)
        self.assertEqual((h.only_item().status, h.documents.load(doc_id).status), (COMMITTED, DONE))
        self.assertEqual(alert_kinds(h), ["waiting", "resolved"])

    def test_the_tag_is_found_whatever_its_case(self):
        h = Harness(self, scanner_tag="scanner", intake_tag="mail intake")
        h.drop(make_scan(letter(1)))
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)

    def test_options_from_before_140_still_load(self):
        h = Harness(self)
        options = h.root / "options.json"
        options.write_text(json.dumps({"paperless_token": "x", "physical_mail_tag": "Physical Mail"}))
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.update(INTAKE_OPTIONS=str(options), INTAKE_PIN_FILE=str(h.root / "pin"))
        (h.root / "pin").write_text("3.3.4\n")
        settings = load()
        self.assertEqual((settings.scanner_tag, settings.intake_tag, settings.webhook_alert), ("Scanner", "Mail Intake", ""))
        self.assertEqual((settings.min_available_mib, settings.max_io_pressure, settings.max_memory_pressure),
                         (500, 60.0, 40.0))


class StartingAgainTests(unittest.TestCase):
    def test_a_claim_cut_short_is_finished_or_undone(self):
        h = Harness(self)
        scan = make_scan(letter(1))
        path = h.drop(scan)
        sha = __import__("hashlib").sha256(scan.pdf).hexdigest()
        root = h.settings.items_dir / sha
        root.mkdir(parents=True)
        (root / "claim.json").write_text(json.dumps({"sha256": sha, "original_name": path.name, "page_count": 1,
                                                     "byte_count": len(scan.pdf), "captured_at_unix": h.clock()}))
        (root / ".state.json.abc.tmp").write_text("half")                   # a temporary file a kill left
        h.restart()                                                         # killed before the file was moved
        self.assertFalse(root.exists())
        self.assertTrue(path.exists())
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)

        other = make_scan(letter(2, "b"))
        sha = __import__("hashlib").sha256(other.pdf).hexdigest()
        root = h.settings.items_dir / sha
        (root / "source").mkdir(parents=True)
        (root / "source" / "original.pdf").write_bytes(other.pdf)
        (root / "claim.json").write_text(json.dumps({"sha256": sha, "original_name": "20261101_100000_000500.pdf",
                                                     "page_count": 2, "byte_count": len(other.pdf),
                                                     "captured_at_unix": h.clock()}))
        h.restart()                                                         # killed after the move, before the record
        item = h.store.load(sha)
        self.assertEqual((item.status, item.state["original_name"]), ("captured", "20261101_100000_000500.pdf"))
        h.settle()
        self.assertEqual(h.store.load(sha).status, COMMITTED)

    def test_google_answers_left_after_a_scan_went_in_are_summarised_by_a_worker_not_the_service(self):
        h = Harness(self)
        h.drop(make_scan(letter(1)))
        h.settle()
        item = h.only_item()
        (item.root / "google-summary.json.gz").unlink()                     # killed after "committed", before tidying
        answer = item.root / "google" / "run-1" / "chunk" / "attempt-1"
        answer.mkdir(parents=True)
        (answer / "response.json").write_text(json.dumps({"document": {"pages": [{"image": {"content": "eA=="}}]}}))
        (answer / "manifest.json").write_text(json.dumps({"status": "complete"}))

        class Runner:
            stopping = False
            tidied = []

            def tidy(self, sha):
                self.tidied.append(sha)
                return 0
        h.build()
        runner = Runner()
        h.service.runner = runner
        opened = []
        real_open = Path.open

        def watch(path, *args, **kwargs):
            if path.name == "response.json":
                opened.append(path)
            return real_open(path, *args, **kwargs)
        with unittest.mock.patch.object(Path, "open", watch), \
                unittest.mock.patch.object(Path, "read_bytes", lambda path: opened.append(path) or real_open(path, "rb").read()):
            h.service.start()
        self.assertEqual(runner.tidied, [item.sha])
        self.assertEqual([path for path in opened if path.name == "response.json"], [])   # the service didn't load it
        h.restart()                                                         # in the tests' own process: done there
        self.assertTrue((item.root / "google-summary.json.gz").exists())
        self.assertFalse((item.root / "google").exists())

    def test_google_answers_are_just_removed_when_their_summary_was_written(self):
        h = Harness(self)
        h.drop(make_scan(letter(1)))
        h.settle()
        item = h.only_item()
        (item.root / "google" / "run-1").mkdir(parents=True)
        h.restart()
        self.assertFalse((item.root / "google").exists())
        self.assertEqual(h.pipeline.left_to_tidy, [])

    def test_a_claim_that_fails_part_way_blocks_nothing_and_is_tried_again(self):
        import errno
        import paperless_intake.store as store
        h = Harness(self)
        first = make_scan(letter(1, "a"))
        h.drop(first, name="20261101_093000_000400.pdf")
        h.drop(make_scan(letter(1, "b")), name="20261101_093100_000401.pdf")
        failing = __import__("hashlib").sha256(first.pdf).hexdigest()
        real = store.write_atomic
        full = [True]

        def disk_full_for_the_first(path, data, *args, **kwargs):
            if path.name == "claim.json" and full[0] and path.parent.name == failing:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, data, *args, **kwargs)
        h.service.claim_captures()
        h.clock.advance(31)
        with unittest.mock.patch.object(store, "write_atomic", disk_full_for_the_first), \
                self.assertLogs("paperless_intake.service", "ERROR") as logged:
            for _ in range(3):                                              # poll after poll: logged once
                h.service.step()
        self.assertEqual(len(logged.output), 1)
        self.assertIn("No space left on device", logged.output[0])
        self.assertEqual(len(h.store.all()), 1)                             # the other file went on regardless
        self.assertTrue((h.settings.items_dir / failing).exists())          # the half-made folder blocks nothing
        full[0] = False
        h.settle()
        self.assertEqual([item.status for item in h.store.all()], [COMMITTED, COMMITTED])
        self.assertEqual(sorted(kind for kind, _ in h.notifier.sent), ["finished"] * 2 + ["started"] * 2)

    def test_records_from_132_are_resumed_as_they_are(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        lease(h)
        record = h.documents.load(doc_id)
        del record.state["lease"]                                           # 1.3.2 wrote no lease
        record.state["cut_short"] = 2
        record.save()
        waiting = h.paperless.add_old_scan(make_scan(letter(1, "b")).pdf)
        from paperless_intake.documents import Record
        Record(h.documents.dir / str(waiting), {"doc_id": waiting, "source": "paperless", "added": [], "tries": {},
                                                "google_run": 1}).set(LATER, reason="Paperless unreachable",
                                                                      not_before_unix=0)
        h.restart()
        for _ in range(3):
            look(h)
        self.assertEqual([h.documents.load(n).status for n in (doc_id, waiting)], [DONE, DONE])


if __name__ == "__main__":
    unittest.main()
