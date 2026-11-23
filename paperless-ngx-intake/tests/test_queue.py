"""The job queue: what may start now, leases, and what is owed after a kill. Synthetic scans and a stand-in Paperless."""

from __future__ import annotations

import json
import os
import time
import unittest

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
        for io, memory, kind in ((30, 0, "io_pressure"), (0, 20, "memory_pressure")):
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
        for green in (dict(available_mib=300), dict(io=50), dict(memory=50)):
            h.green(available_mib=2048)
            h.green(**green)
            kinds.add(h.service.queue._resources().kind)
        h.pipeline.intent.open(what="original", sha256="0" * 64, name="x.pdf")
        kinds.add(h.service.queue._uploads().kind)
        self.assertLessEqual(kinds | {"disk_low"}, GATE_KINDS)

    def test_a_scan_held_by_the_gate_goes_before_any_stored_document(self):
        for green in (dict(available_mib=300), dict(io=50)):
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
                         (500, 30.0, 20.0))


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
