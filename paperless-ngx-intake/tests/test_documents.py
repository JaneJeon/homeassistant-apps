"""Every document in Paperless gets the same question, however it arrived.

Was it scanned, and does it lack a searchable version? These tests put documents
into a stand-in Paperless by every route (the scanner's folder, a hand upload,
stored long before the app) and end at what Jane would see on the document.
"""

from __future__ import annotations

import shutil
import unittest
import unittest.mock

from paperless_intake import __version__
from paperless_intake.docai import glyphless
from paperless_intake.documents import DONE, GAVE_UP, LATER, NEXT_RELEASE, NOT_NEEDED, WORKING, Record
from paperless_intake.paperless import PaperlessError
from paperless_intake.service import Service
from paperless_intake.store import COMMITTED, COMMITTED_WARNING, HELD, WAITING

from fake_paperless import Crash
from synth import PageSpec, make_scan, numbered_letter
from test_pipeline import Harness

HOUR = 3600


def letter(count: int, tag: str = "") -> list[PageSpec]:
    return [PageSpec([f"Statement {tag} page {n} with enough words on it to judge which way up it is",
                      "A second line of ordinary text for this page of the test letter",
                      "And a third so the page counts as one with plenty of text"], footer=f"Page {n} of {count}")
            for n in range(1, count + 1)]


def alert_kinds(h) -> list[str]:
    return [payload["kind"] for kind, payload in h.notifier.sent if kind == "alert"]


def look(h, after=301):
    """Let time pass until the app asks Paperless again what it holds, then let it work."""
    h.clock.advance(after)
    h.settle()


def a_newer_release_starts(h, doc_id=None):
    """The app was updated. Records say which release last tried, so that is all an update changes."""
    for record in h.documents.all():
        if doc_id in {None, record.doc_id}:
            record.state["by"] = "an-earlier-release"
            record.save()
    h.build()


def as_stored_before_this_release(h, item, release="1.1.2"):
    """What the Green holds from before 1.3.0: a finished scan record, and no record of its document."""
    shutil.rmtree(h.documents.dir / str(item.state["doc_id"]))
    item.state["prepared_by"] = release
    item.save()
    h.build()
    assert h.documents.adopt_scans() == 1


def badly_fitted():
    """Lay the words out as 1.1.2 did, so Paperless reads some of them run together."""
    real_fit = glyphless.fit
    return unittest.mock.patch.object(glyphless, "fit", lambda points, cells: real_fit(points, max(1, cells - 1)))


class Base(unittest.TestCase):
    def record(self, h, doc_id) -> Record:
        return h.documents.load(doc_id)

    def assertNothingToDo(self, h):
        writes = [call for call in h.paperless.calls if call in {"update_version", "add_note", "set_owner",
                                                                 "post_document"}]
        asked = h.google.calls
        look(h)
        self.assertEqual([call for call in h.paperless.calls if call in {"update_version", "add_note", "set_owner",
                                                                         "post_document"}], writes)
        self.assertEqual(h.google.calls, asked)


class ScansHandedOverTests(Base):
    """A scan the app stored itself. Once it has finished going in, its document belongs to the loop."""

    def stored(self, scan=None):
        h = Harness(self)
        h.drop(scan or make_scan(letter(3), turn=180, flipped=True))
        h.settle()
        return h, h.only_item()

    def stored_reading_badly(self):
        h = Harness(self)
        h.drop(make_scan(letter(3), turn=180, flipped=True, touching=True))
        with badly_fitted():
            h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["mode"]), (COMMITTED_WARNING, "with_google"))
        self.assertIn("search text differs", item.state["reason"])
        self.assertEqual(alert_kinds(h), ["warning"])
        return h, item

    def stored_without_a_copy(self, scan=None):
        from paperless_intake.docai.pdf_renderer import ReviewRequired
        h = Harness(self)

        def refuse(*args, **kwargs):
            raise ReviewRequired("Only a direct JPEG image without masks is supported")

        real = h.pipeline.renderer.render
        h.pipeline.renderer.render = refuse
        h.drop(scan or make_scan(letter(3), turn=180, encodings=["jpeg", "ccitt", "jpeg"]))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 1)
        self.assertEqual(alert_kinds(h), ["warning"])
        self.real_render = real
        return h, item

    def test_a_scan_that_went_in_well_has_a_record_saying_so_and_is_left_alone(self):
        h, item = self.stored()
        record = self.record(h, item.state["doc_id"])
        self.assertEqual((record.status, record.state["source"], record.state["kind"], record.state["by"]),
                         (DONE, "scanner", "scanned", __version__))
        self.assertEqual(record.state["added"], [item.state["searchable_sha256"]])
        self.assertNothingToDo(h)
        a_newer_release_starts(h)
        self.assertNothingToDo(h)                                          # a newer release leaves a good one alone too

    def test_a_copy_that_read_back_badly_is_built_again_by_the_next_release_and_only_by_that(self):
        h, item = self.stored_reading_badly()
        doc_id = item.state["doc_id"]
        doc = h.paperless.docs[doc_id]
        self.assertEqual(self.record(h, doc_id).status, NEXT_RELEASE)
        self.assertNothingToDo(h)                                          # the release that built it has had its try
        as_stored_before_this_release(h, item)
        h.settle()
        record = self.record(h, doc_id)
        self.assertEqual((record.status, record.state["by"]), (DONE, __version__))
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (3, 2))
        self.assertIn("built the searchable version again", doc["notes"][1])
        self.assertIsNone(doc["owner"])
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])           # the warning Jane got is closed
        self.assertEqual(h.only_item().status, COMMITTED_WARNING)           # what happened to the scan stays as it was
        self.assertNothingToDo(h)

    def test_a_better_copy_that_cant_be_built_leaves_the_stored_one_alone(self):
        from paperless_intake.docai.pdf_renderer import ReviewRequired
        h, item = self.stored_reading_badly()
        doc = h.paperless.docs[item.state["doc_id"]]
        as_stored_before_this_release(h, item)

        def refuse(*args, **kwargs):
            raise ReviewRequired("a page shape nobody has met yet")

        h.pipeline.renderer.render = refuse
        h.settle()
        record = self.record(h, item.state["doc_id"])
        self.assertEqual((record.status, record.state["by"]), (NEXT_RELEASE, __version__))
        self.assertIn("a page shape nobody has met yet", record.state["reason"])
        self.assertEqual(record.state["added"], [item.state["searchable_sha256"]])
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (2, 1))
        self.assertEqual(alert_kinds(h), ["warning"])                       # nothing new was said
        self.assertNothingToDo(h)                                           # and this release doesn't try twice

    def test_a_rebuilt_copy_that_still_reads_badly_isnt_reported_again(self):
        h, item = self.stored_reading_badly()
        doc = h.paperless.docs[item.state["doc_id"]]
        as_stored_before_this_release(h, item)
        real_fit = glyphless.fit
        with unittest.mock.patch.object(glyphless, "fit", lambda points, cells: real_fit(points, max(1, cells - 1))
                                        if cells % 2 else real_fit(points, cells)):
            h.settle()                                                      # better, and still short of the mark
        record = self.record(h, item.state["doc_id"])
        self.assertEqual(record.status, NEXT_RELEASE)
        self.assertIn("search text differs", record.state["reason"])
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (3, 2))
        self.assertIn("Read-back warnings", doc["notes"][1])
        self.assertEqual(alert_kinds(h), ["warning"])
        self.assertNothingToDo(h)

    def test_a_scan_stored_without_its_copy_gets_it_from_a_later_release(self):
        h, item = self.stored_without_a_copy()
        doc_id = item.state["doc_id"]
        self.assertEqual(self.record(h, doc_id).status, NEXT_RELEASE)
        self.assertNothingToDo(h)                                           # this release couldn't, and doesn't try again
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        asked = h.google.calls
        h.settle()
        doc = h.paperless.docs[doc_id]
        self.assertEqual(self.record(h, doc_id).status, DONE)
        self.assertEqual((len(h.paperless.docs), len(doc["versions"]), len(doc["notes"])), (1, 2, 2))
        self.assertIn("Only the original was stored", doc["notes"][0])
        self.assertIn("added the searchable version", doc["notes"][1])
        self.assertIn("3 of 3 pages were turned upright", doc["notes"][1])
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])
        self.assertEqual(h.google.calls, asked + 1)                         # read once more, from Paperless's copy

    def test_the_scan_kept_on_the_green_isnt_needed_for_it(self):
        # Version 1 comes from Paperless, so it works after the local copy has been tidied away.
        h, item = self.stored_without_a_copy()
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        item.source_path.unlink()
        h.settle()
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)

    def test_a_scan_google_refused_is_never_asked_about_again(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline._ready_without_google(item, "Google couldn't read this scan (PAGE_LIMIT_EXCEEDED, HTTP 400)")
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        record = self.record(h, item.state["doc_id"])
        self.assertEqual(record.status, GAVE_UP)
        a_newer_release_starts(h)
        self.assertNothingToDo(h)

    def test_a_scan_jane_kept_with_a_page_missing_is_never_held_or_asked_about_again(self):
        h = Harness(self)
        h.drop(numbered_letter(3, missing=(2,)))
        with badly_fitted():
            h.settle()
            item = h.only_item()
            self.assertEqual(item.status, HELD)
            h.pipeline.keep_as_it_is(item)
            h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        said = alert_kinds(h)
        as_stored_before_this_release(h, item)
        h.settle()
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual(len(doc["versions"]), 3)
        self.assertEqual(h.only_item().status, COMMITTED_WARNING)
        self.assertNotIn("held", alert_kinds(h)[len(said):])
        self.assertNotIn("may be missing", doc["notes"][-1])                # that was asked and answered at the scan

    # A document already in Paperless is safe. Working on it must never make it look otherwise.

    def assertLeftAsItWas(self, h, item, *, versions=1):
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (versions, 1))
        self.assertEqual(alert_kinds(h), ["warning"])                       # the one from the scan, nothing new
        item = h.only_item()
        self.assertEqual((item.status, item.state["doc_id"]), (COMMITTED_WARNING, item.state["doc_id"]))

    def test_google_being_down_leaves_the_document_alone_and_is_tried_again_in_an_hour(self):
        h, item = self.stored_without_a_copy()
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        h.google.fail = [503] * 200
        h.settle()
        record = self.record(h, item.state["doc_id"])
        self.assertEqual(record.status, LATER)
        self.assertLeftAsItWas(h, item)
        self.assertNothingToDo(h)                                           # not before the hour is up
        h.google.fail = []
        look(h, HOUR)
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])

    def test_paperless_being_down_for_a_day_gives_up_quietly_until_the_next_release(self):
        h, item = self.stored_without_a_copy()
        doc_id = item.state["doc_id"]
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        h.paperless.unreachable = True
        for _ in range(60):                                                 # thirty hours of half-hourly looks
            h.settle()
            h.clock.advance(30 * 60)
        h.paperless.unreachable = False
        record = self.record(h, doc_id)
        self.assertEqual((record.status, record.state["by"]), (NEXT_RELEASE, __version__))
        self.assertLeftAsItWas(h, item)
        self.assertNothingToDo(h)
        a_newer_release_starts(h)
        h.pipeline.renderer.render = self.real_render
        h.settle()
        self.assertEqual(self.record(h, doc_id).status, DONE)

    def test_an_unexpected_error_says_nothing_changes_nothing_and_is_tried_three_times(self):
        h, item = self.stored_without_a_copy()
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render

        def boom(*args, **kwargs):
            raise RuntimeError("something nobody planned for")

        h.pipeline._plan = boom
        asked = h.google.calls
        for _ in range(6):
            h.settle()
            h.clock.advance(HOUR)
        record = self.record(h, item.state["doc_id"])
        self.assertEqual((record.status, record.state["tries"]["odd"]), (NEXT_RELEASE, 3))
        self.assertIn("RuntimeError", record.state["reason"])
        self.assertLeftAsItWas(h, item)
        self.assertEqual(h.google.calls, asked + 1)                         # Google was paid once, not three times

    def test_work_cut_short_after_the_upload_is_finished_without_uploading_twice(self):
        h, item = self.stored_reading_badly()
        doc_id = item.state["doc_id"]
        doc = h.paperless.docs[doc_id]
        as_stored_before_this_release(h, item)
        h.paperless.crash_after.add("update_version")
        record = h.documents.step()
        with self.assertRaises(Crash):                                      # the app dies part way through
            h.documents._work(record)
        self.assertEqual((self.record(h, doc_id).status, len(doc["versions"]), len(doc["notes"])), (WORKING, 3, 1))
        h.build()                                                           # the next start
        h.settle()
        self.assertEqual(self.record(h, doc_id).status, DONE)
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (3, 2))
        self.assertEqual(h.paperless.calls.count("update_version"), 2)      # the scan's, and one for the rebuild

    def test_work_cut_short_after_the_note_doesnt_write_the_note_twice(self):
        h, item = self.stored_reading_badly()
        doc_id = item.state["doc_id"]
        doc = h.paperless.docs[doc_id]
        as_stored_before_this_release(h, item)
        h.paperless.crash_after.add("add_note")
        record = h.documents.step()
        with self.assertRaises(Crash):
            h.documents._work(record)
        self.assertEqual((self.record(h, doc_id).status, len(doc["versions"]), len(doc["notes"])), (WORKING, 3, 2))
        a_newer_release_starts(h)                                           # the app was updated again meanwhile
        h.settle()
        self.assertEqual(self.record(h, doc_id).status, DONE)
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (3, 2))
        self.assertEqual(h.paperless.calls.count("update_version"), 2)

    def test_an_error_after_the_upload_is_picked_up_an_hour_later(self):
        h, item = self.stored_reading_badly()
        doc_id = item.state["doc_id"]
        doc = h.paperless.docs[doc_id]
        as_stored_before_this_release(h, item)
        metadata = h.paperless.metadata

        def boom(*args, **kwargs):
            raise RuntimeError("something nobody planned for")

        h.paperless.metadata = boom
        h.settle()
        self.assertEqual((self.record(h, doc_id).status, len(doc["versions"]), len(doc["notes"])), (LATER, 3, 1))
        self.assertEqual(alert_kinds(h), ["warning"])
        h.paperless.metadata = metadata
        asked = h.google.calls
        look(h, HOUR)
        self.assertEqual(self.record(h, doc_id).status, DONE)
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (3, 2))  # not uploaded twice
        self.assertEqual(h.google.calls, asked)                             # and Google not asked twice
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])

    def test_tags_jane_changed_since_are_left_as_she_set_them(self):
        h, item = self.stored_without_a_copy()
        doc = h.paperless.docs[item.state["doc_id"]]
        doc["tags"] = [9]                                                   # she took the app's tags off and set her own
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        h.settle()
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)
        self.assertEqual((doc["tags"], doc["owner"], len(doc["versions"])), ([9], None, 2))

    def test_a_document_jane_made_her_own_is_let_go_without_a_word(self):
        # Paperless hides a document with an owner from every other user, the app included.
        h, item = self.stored_without_a_copy()
        doc = h.paperless.docs[item.state["doc_id"]]
        doc["owner"] = 3
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        h.settle()
        record = self.record(h, item.state["doc_id"])
        self.assertEqual(record.status, GAVE_UP)
        self.assertEqual((len(doc["versions"]), len(doc["notes"]), doc["owner"]), (1, 1, 3))
        self.assertEqual(alert_kinds(h), ["warning"])
        a_newer_release_starts(h)
        self.assertNothingToDo(h)

    def test_a_document_jane_deleted_is_let_go_at_once_and_without_a_word(self):
        h, item = self.stored_without_a_copy()
        doc_id = item.state["doc_id"]
        as_stored_before_this_release(h, item, "1.1.0")
        del h.paperless.docs[doc_id]
        h.settle()
        self.assertEqual(self.record(h, doc_id).status, GAVE_UP)
        self.assertEqual((alert_kinds(h), h.paperless.docs), (["warning"], {}))
        a_newer_release_starts(h)
        self.assertNothingToDo(h)

    def test_a_file_version_someone_else_added_means_the_app_adds_none(self):
        h, item = self.stored_reading_badly()
        doc_id = item.state["doc_id"]
        doc = h.paperless.docs[doc_id]
        doc["versions"].insert(0, {"id": 99, "checksum": "e" * 64, "label": "Rotated by hand", "data": b"%PDF-1.4",
                                   "archive": False, "content": "her own"})
        as_stored_before_this_release(h, item)
        h.settle()
        record = self.record(h, doc_id)
        self.assertEqual(record.status, NOT_NEEDED)
        self.assertIn("someone else added a file version", record.state["reason"])
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (3, 1))
        a_newer_release_starts(h)
        self.assertNothingToDo(h)

    def test_copies_added_by_earlier_releases_count_as_the_apps_own(self):
        # Document 54 on the Green has three copies from three releases. Only the newest is in its scan record.
        h, item = self.stored_reading_badly()
        doc_id = item.state["doc_id"]
        doc = h.paperless.docs[doc_id]
        doc["versions"].insert(1, {"id": 98, "checksum": "d" * 64, "label": "Google OCR pretrained, intake 1.1.0",
                                   "data": b"%PDF-1.4", "archive": False, "content": ""})
        as_stored_before_this_release(h, item)
        h.settle()
        self.assertEqual(self.record(h, doc_id).status, DONE)
        self.assertEqual(len(doc["versions"]), 4)

    def test_version_1_replaced_in_paperless_is_not_worked_on(self):
        h, item = self.stored_without_a_copy()
        doc_id = item.state["doc_id"]
        other = make_scan(letter(1, "z")).pdf
        import hashlib
        h.paperless.docs[doc_id]["versions"][-1].update(data=other, checksum=hashlib.sha256(other).hexdigest())
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        h.settle()
        record = self.record(h, doc_id)
        self.assertEqual(record.status, GAVE_UP)
        self.assertIn("no longer the file that was stored", record.state["reason"])
        self.assertEqual(alert_kinds(h), ["warning"])

    def test_a_download_that_doesnt_match_paperlesss_own_checksum_is_never_worked_from(self):
        h, item = self.stored_without_a_copy()
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        h.paperless.download_original = lambda doc_id, version_id: b"%PDF-1.4 cut short on the way"
        asked = h.google.calls
        for _ in range(5):
            h.settle()
            h.clock.advance(HOUR)
        record = self.record(h, item.state["doc_id"])
        self.assertEqual(record.status, NEXT_RELEASE)
        self.assertIn("doesn't match Paperless's own checksum", record.state["reason"])
        self.assertLeftAsItWas(h, item)
        self.assertEqual(h.google.calls, asked)

    def test_a_copy_release_110_couldnt_build_is_known_from_the_sentence_it_recorded(self):
        h, item = self.stored_without_a_copy()
        item.state.pop("copy_not_built", None)                              # 1.1.0 never wrote this
        item.save()
        as_stored_before_this_release(h, item, "1.1.0")
        self.assertEqual(self.record(h, item.state["doc_id"]).status, NEXT_RELEASE)
        h.pipeline.renderer.render = self.real_render
        h.settle()
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 2)

    def test_a_copy_release_112_stored_reading_badly_is_known_from_its_warning(self):
        h, item = self.stored_reading_badly()
        item.state.pop("copy_reads_badly", None)                            # 1.1.2 never wrote these
        item.state.pop("copy_in_paperless", None)
        item.save()
        as_stored_before_this_release(h, item, "1.1.2")
        self.assertEqual(self.record(h, item.state["doc_id"]).status, NEXT_RELEASE)
        h.settle()
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 3)

    def test_a_scan_record_put_back_from_a_snapshot_has_the_key_with_nothing_in_it(self):
        # 1.2.0 began to redo a 1.1.2 scan and put its record back: "reads badly" is there, and empty.
        h, item = self.stored_reading_badly()
        item.state["copy_reads_badly"] = None
        item.state.pop("copy_in_paperless", None)
        item.save()
        as_stored_before_this_release(h, item, "1.1.2")
        self.assertEqual(self.record(h, item.state["doc_id"]).status, NEXT_RELEASE)

    def test_what_each_try_measured_is_kept_with_the_record(self):
        h, item = self.stored_without_a_copy()
        first = (item.root / "checks.json").read_bytes()
        self.assertIn(b"render_error", first)
        as_stored_before_this_release(h, item, "1.1.0")
        h.pipeline.renderer.render = self.real_render
        h.settle()
        record = self.record(h, item.state["doc_id"])
        self.assertEqual((item.root / "checks.json").read_bytes(), first)   # the scan's own record is untouched
        self.assertNotIn(b"render_error", (record.root / "checks.json").read_bytes())
        trail = (record.root / "events.jsonl").read_text().splitlines()
        self.assertGreaterEqual(len(trail), 3)                              # redo, working, done


class OnlyWhatCameFromTheScannerTests(Base):
    """The Physical Mail tag is the boundary. A document without it is not this app's, whatever is in the file."""

    def test_a_document_without_the_physical_mail_tag_is_never_looked_at(self):
        h = Harness(self)
        scan = make_scan(letter(2))                                         # a scan in every way but the tag
        doc_id = h.paperless.add_foreign(scan.pdf, name="from my phone.pdf", tags=[9])
        fetched = []
        download = h.paperless.download_original
        h.paperless.download_original = lambda *args, **kwargs: fetched.append(args) or download(*args, **kwargs)
        h.settle()
        for _ in range(3):
            look(h)
        doc = h.paperless.docs[doc_id]
        self.assertFalse(h.documents.exists(doc_id))                        # no record: it was never taken on
        self.assertEqual((fetched, h.google.calls), ([], 0))                # not even fetched
        self.assertEqual((doc["tags"], len(doc["versions"]), doc["notes"]), ([9], 1, []))
        self.assertEqual(h.notifier.sent, [])

    def test_a_scan_that_is_given_the_tag_later_is_taken_on_then(self):
        # A scan from before Jane's tagging rule has no tag. That is a gap on that one document,
        # and it is mended on the document: she adds the tag in Paperless, and nothing in the app changes.
        h = Harness(self)
        doc_id = h.paperless.add_foreign(make_scan(letter(2)).pdf, name="20260101_120000_000001.pdf")
        look(h)
        self.assertFalse(h.documents.exists(doc_id))                        # the scanner's file name decides nothing
        h.paperless.docs[doc_id]["tags"].append(h.paperless.tags["Physical Mail"])
        look(h)
        self.assertEqual(self.record(h, doc_id).status, DONE)
        self.assertEqual(len(h.paperless.docs[doc_id]["versions"]), 2)

    def test_the_tag_itself_missing_from_paperless_is_only_a_wait(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        number = h.paperless.tags.pop("Physical Mail")                      # renamed or deleted in Paperless
        h.settle()
        self.assertEqual((h.documents.all(), h.google.calls, h.notifier.sent), ([], 0, []))
        h.paperless.tags["Physical Mail"] = number
        look(h)
        self.assertEqual(self.record(h, doc_id).status, DONE)


class ScansStoredBeforeTheAppTests(Base):
    """Stored through Paperless's own folder before the app existed, and tagged Physical Mail by Jane's rule there."""

    def test_a_scan_stored_before_the_app_gets_a_searchable_version(self):
        h = Harness(self)
        scan = make_scan(letter(3), turn=180, flipped=True)
        doc_id = h.paperless.add_old_scan(scan.pdf, tags=[9])
        h.settle()
        doc = h.paperless.docs[doc_id]
        record = self.record(h, doc_id)
        self.assertEqual((record.status, record.state["source"], record.state["kind"]), (DONE, "paperless", "scanned"))
        self.assertEqual(len(doc["versions"]), 2)
        self.assertEqual(doc["versions"][-1]["data"], scan.pdf)             # version 1 is still the file as uploaded
        self.assertTrue(doc["versions"][0]["label"].startswith("Google OCR "))
        self.assertEqual((doc["tags"], doc["owner"]), ([1, 9], None))       # hers, untouched
        self.assertEqual(len(doc["notes"]), 1)
        self.assertIn("This scan was already in Paperless, without a searchable version", doc["notes"][0])
        self.assertIn("reordered to follow the printed page numbers", doc["notes"][0])
        self.assertIn("3 of 3 pages were turned upright", doc["notes"][0])
        self.assertEqual(h.notifier.sent, [])                               # no started, no finished, no alert
        self.assertEqual(h.google.calls, 1)
        self.assertFalse(record.source_path.exists())                       # working files are dropped
        self.assertNothingToDo(h)

    def test_a_pdf_that_already_has_text_is_left_exactly_as_it_is(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        h.settle()
        with_text = h.paperless.docs[h.only_item().state["doc_id"]]["versions"][0]["data"]
        asked = h.google.calls
        doc_id = h.paperless.add_old_scan(with_text)                        # the scanner can be set to read pages itself
        look(h)
        record = self.record(h, doc_id)
        self.assertEqual((record.status, record.state["kind"]), (NOT_NEEDED, "has_text"))
        doc = h.paperless.docs[doc_id]
        self.assertEqual((len(doc["versions"]), doc["notes"]), (1, []))
        self.assertEqual(h.google.calls, asked)                             # nothing was sent to Google
        a_newer_release_starts(h)
        self.assertNothingToDo(h)

    def test_trouble_after_the_outcome_is_recorded_doesnt_undo_it(self):
        # The disk fills up as the working files are being removed. The version and the note are in.
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(2)).pdf)
        with unittest.mock.patch.object(Record, "tidy", side_effect=OSError("No space left on device")):
            h.settle()
        doc = h.paperless.docs[doc_id]
        self.assertEqual((self.record(h, doc_id).status, len(doc["versions"]), len(doc["notes"])), (DONE, 2, 1))
        asked = h.google.calls
        look(h, HOUR)
        self.assertEqual((len(doc["versions"]), len(doc["notes"]), h.google.calls), (2, 1, asked))

    def test_paperless_refusing_the_new_file_version_is_tried_three_times_and_said_plainly(self):
        from paperless_intake.paperless import PaperlessTaskFailed
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)

        def refuse(*args, **kwargs):
            raise PaperlessTaskFailed("Paperless task t-1 failure", "the file was rejected")

        h.paperless.update_version = refuse
        for _ in range(5):
            h.settle()
            h.clock.advance(HOUR)
        record = self.record(h, doc_id)
        self.assertEqual((record.status, record.state["tries"]["odd"]), (NEXT_RELEASE, 3))
        self.assertIn("Paperless didn't take the new file version", record.state["reason"])
        self.assertEqual((h.google.calls, h.notifier.sent), (1, []))

    def test_a_tagged_file_that_isnt_a_pdf_is_set_aside_for_good_with_the_reason(self):
        # A photo that was given the Physical Mail tag. The tag makes it the app's to look at. The scanner saves
        # PDFs, so the tag is probably the mistake, and giving a photo the tag must not be a way to get it read.
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(b"\x89PNG\r\n\x1a\n" + b"0" * 64, name="photo.png")
        h.settle()
        record = self.record(h, doc_id)
        self.assertEqual((record.status, record.state["kind"]), (GAVE_UP, "not_pdf"))
        self.assertIn("it isn't a PDF", record.state["reason"])
        self.assertEqual((h.google.calls, h.notifier.sent), (0, []))
        self.assertNothingToDo(h)
        a_newer_release_starts(h)
        self.assertNothingToDo(h)                                           # a later release doesn't look again

    def test_a_scan_with_almost_no_text_is_left_alone(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan([PageSpec(["Hi"])]).pdf)
        h.settle()
        record = self.record(h, doc_id)
        self.assertEqual(record.status, NOT_NEEDED)
        self.assertIn("almost no text", record.state["reason"])
        self.assertEqual((len(h.paperless.docs[doc_id]["versions"]), h.paperless.docs[doc_id]["notes"]), (1, []))

    def test_a_page_that_looks_missing_goes_into_the_note_and_holds_nothing(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(numbered_letter(3, missing=(2,)).pdf)
        h.settle()
        doc = h.paperless.docs[doc_id]
        self.assertEqual(self.record(h, doc_id).status, DONE)
        self.assertEqual(len(doc["versions"]), 2)
        self.assertIn("a page may be missing", doc["notes"][0])
        self.assertEqual(h.notifier.sent, [])

    def test_a_scan_that_has_an_owner_is_never_seen_until_it_is_shared_or_the_owner_is_cleared(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf, owner=2)
        h.settle()
        self.assertFalse(h.documents.exists(doc_id))
        self.assertEqual((h.google.calls, len(h.paperless.docs[doc_id]["versions"])), (0, 1))
        h.paperless.docs[doc_id]["owner"] = None                            # mended on the document, in Paperless
        look(h)
        self.assertEqual(self.record(h, doc_id).status, DONE)

    def test_google_refusing_one_document_doesnt_stop_the_next(self):
        h = Harness(self)
        first = h.paperless.add_old_scan(make_scan(letter(1, "a")).pdf)
        second = h.paperless.add_old_scan(make_scan(letter(1, "b")).pdf)
        h.google.fail = [400]
        h.settle()
        record = self.record(h, first)
        self.assertEqual(record.status, GAVE_UP)
        self.assertIn("Google couldn't read this scan", record.state["reason"])
        self.assertEqual(self.record(h, second).status, DONE)
        self.assertEqual(h.notifier.sent, [])

    def test_a_scan_is_always_served_before_stored_documents(self):
        h = Harness(self)
        stored = h.paperless.add_old_scan(make_scan(letter(1, "old")).pdf)
        h.drop(make_scan(letter(1, "new")))
        h.service.claim_captures()
        h.clock.advance(31)
        self.assertTrue(h.service.step())
        self.assertEqual(h.only_item().status, COMMITTED)                   # the scan went first, all the way in
        self.assertFalse(h.documents.exists(stored))
        self.assertTrue(h.service.step())
        self.assertEqual(self.record(h, stored).status, DONE)

    def test_a_new_document_is_noticed_within_minutes_without_being_announced(self):
        h = Harness(self)
        h.settle()                                                          # nothing there
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        h.settle()
        self.assertFalse(h.documents.exists(doc_id))                        # Paperless isn't asked at every turn
        look(h)
        self.assertEqual(self.record(h, doc_id).status, DONE)

    def test_paperless_being_unreachable_when_asked_is_only_a_wait(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        h.paperless.unreachable = True
        h.settle()
        self.assertEqual((h.documents.all(), h.notifier.sent), ([], []))
        h.paperless.unreachable = False
        look(h)
        self.assertEqual(self.record(h, doc_id).status, DONE)

    def test_a_paperless_version_the_app_wasnt_tested_on_gets_no_writes(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        h.paperless.version = "3.4.0"
        h.settle()
        record = self.record(h, doc_id)
        self.assertEqual(record.status, LATER)
        self.assertIn("tested on 3.3.4", record.state["reason"])
        self.assertEqual((len(h.paperless.docs[doc_id]["versions"]), h.google.calls, h.notifier.sent), (1, 0, []))


class WhoOwnsADocumentTests(Base):
    """One document is never worked on from two sides."""

    def test_a_scan_still_going_in_keeps_its_document_until_it_has_finished(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        add = h.paperless.update_version

        def refuse(*args, **kwargs):
            raise PaperlessError("Paperless POST /api/documents/1/update_version/ returned HTTP 503")

        h.paperless.update_version = refuse
        h.settle()
        item = h.only_item()
        doc_id = item.state["doc_id"]
        self.assertEqual((item.status, item.state["retry_stage"]), (WAITING, "commit"))
        self.assertEqual(len(h.paperless.docs[doc_id]["versions"]), 1)      # the original is in, the copy isn't
        look(h)                                                             # the loop looks, and keeps its hands off
        self.assertEqual(h.documents.all(), [])
        h.paperless.update_version = add
        look(h, 31 * 60)
        self.assertEqual(h.only_item().status, COMMITTED)
        self.assertEqual(self.record(h, doc_id).status, DONE)
        self.assertEqual((len(h.paperless.docs[doc_id]["versions"]), len(h.paperless.docs[doc_id]["notes"])), (2, 1))
        self.assertEqual(h.paperless.calls.count("update_version"), 1)

    def test_a_scan_whose_copy_was_cut_short_is_finished_by_the_scan_not_the_loop(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline.prepare(item)
        h.paperless.crash_after.add("update_version")
        with self.assertRaises(Crash):
            h.pipeline.commit(item)
        self.assertEqual(item.status, "committing")
        h.build()
        self.assertIsNone(h.documents.step())                               # not the loop's yet
        h.settle()
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual((h.only_item().status, len(doc["versions"]), len(doc["notes"])), (COMMITTED, 2, 1))
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)

    def test_the_same_file_stored_twice_is_two_documents_each_with_its_own_record(self):
        h = Harness(self)
        scan = make_scan(letter(2))
        h.drop(scan)
        h.settle()
        first = h.only_item().state["doc_id"]
        written = self.record(h, first).state["recorded_at"]
        second = h.paperless.add_old_scan(scan.pdf)                         # the same file, also in through Paperless's folder
        look(h)
        look(h)
        self.assertEqual(self.record(h, second).status, DONE)
        self.assertEqual(len(h.paperless.docs[second]["versions"]), 2)
        record = self.record(h, first)
        self.assertEqual((record.status, record.state["recorded_at"], record.state["source"]), (DONE, written, "scanner"))
        self.assertEqual(len((record.root / "events.jsonl").read_text().splitlines()), 1)   # written once, not at every look

    def test_a_finished_scan_whose_record_went_missing_gets_it_back(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        h.settle()
        doc_id = h.only_item().state["doc_id"]
        shutil.rmtree(h.documents.dir / str(doc_id))
        look(h)
        record = self.record(h, doc_id)
        self.assertEqual((record.status, record.state["source"]), (DONE, "scanner"))
        self.assertEqual(len(h.paperless.docs[doc_id]["versions"]), 2)

    def test_a_scan_whose_file_is_already_in_paperless_another_way_becomes_a_document_of_its_own(self):
        h = Harness(self)
        scan = make_scan(letter(2))
        theirs = h.paperless.add_foreign(scan.pdf, name="uploaded by hand.pdf", tags=[9])
        h.settle()
        doc = h.paperless.docs[theirs]
        h.drop(scan)                                                        # the same file, through the scanner's folder
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertNotEqual(item.state["doc_id"], theirs)
        self.assertEqual((doc["tags"], len(doc["versions"]), doc["notes"]), ([9], 1, []))        # theirs is untouched
        self.assertFalse(h.documents.exists(theirs))                        # and was never the app's
        self.assertEqual(self.record(h, item.state["doc_id"]).state["source"], "scanner")
        self.assertEqual(len(h.paperless.docs), 2)

    def test_when_paperless_refuses_duplicates_such_a_scan_waits_and_says_why(self):
        h = Harness(self)
        h.paperless.refuse_duplicates = True
        scan = make_scan(letter(2))
        theirs = h.paperless.add_foreign(scan.pdf, name="uploaded by hand.pdf", tags=[9])
        h.settle()
        h.drop(scan)
        h.settle()
        item = h.only_item()
        doc = h.paperless.docs[theirs]
        self.assertEqual(item.status, WAITING)
        self.assertIn("already stored as a document the app didn't upload", item.state["reason"])
        self.assertEqual((len(h.paperless.docs), doc["tags"], len(doc["versions"]), doc["notes"]), (1, [9], 1, []))
        self.assertFalse(h.documents.exists(theirs))                        # the other document was never the app's

    def test_a_scan_cut_short_just_after_its_note_was_written_gets_one_note(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline.prepare(item)
        h.paperless.crash_after.add("add_note")
        with self.assertRaises(Crash):
            h.pipeline.commit(item)
        h.build()
        h.settle()
        doc = h.paperless.docs[h.only_item().state["doc_id"]]
        self.assertEqual((h.only_item().status, len(doc["versions"]), len(doc["notes"])), (COMMITTED, 2, 1))

    def test_a_note_written_just_before_an_update_is_recognised_after_it(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline.prepare(item)
        h.paperless.crash_after.add("add_note")
        with self.assertRaises(Crash):
            h.pipeline.commit(item)
        doc = h.paperless.docs[item.state["doc_id"]]
        doc["notes"] = [note.replace(f"paperless-intake {__version__}", "paperless-intake 1.2.9")
                        for note in doc["notes"]]                           # as the release before wrote it
        h.build()
        h.settle()
        self.assertEqual((h.only_item().status, len(doc["notes"])), (COMMITTED, 1))

    def test_a_document_paperless_names_no_file_for_is_left_alone(self):
        # If Paperless ever stopped saying which file a document holds, the app couldn't tell whose it is.
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        listing = h.paperless.documents
        h.paperless.documents = lambda: [{**row, "versions": [{**v, "checksum": None} for v in row["versions"]]}
                                         for row in listing()]
        h.settle()
        self.assertEqual((h.documents.all(), h.google.calls), ([], 0))
        h.paperless.documents = listing
        look(h)
        self.assertEqual(self.record(h, doc_id).status, DONE)

    def test_if_the_apps_own_records_are_lost_the_documents_still_say_what_they_have(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        h.settle()
        doc_id = h.only_item().state["doc_id"]
        doc = h.paperless.docs[doc_id]
        asked = h.google.calls
        shutil.rmtree(h.settings.intake_dir)                                # everything the app ever wrote down
        h.settings.intake_dir.mkdir()
        h.build()
        h.settle()
        record = self.record(h, doc_id)
        self.assertEqual((record.status, record.state["by"], record.state["source"]), (DONE, __version__, "paperless"))
        self.assertEqual((len(doc["versions"]), len(doc["notes"]), h.google.calls), (2, 1, asked))
        a_newer_release_starts(h)
        self.assertNothingToDo(h)

    def test_a_scan_that_failed_its_read_back_is_not_the_loops_to_mend(self):
        h = Harness(self)
        h.drop(make_scan(letter(2)))
        download = h.paperless.download_original
        h.paperless.download_original = lambda doc_id, version_id: b"not what was sent"
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, "failed")
        h.paperless.download_original = download
        doc = h.paperless.docs[item.state["doc_id"]]
        before = (len(doc["versions"]), len(doc["notes"]))
        look(h)
        self.assertEqual(h.documents.all(), [])
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), before)


class HowMuchAtOnceTests(Base):
    def test_this_release_starts_on_three_scans_stored_before_the_app_and_no_more(self):
        h = Harness(self)
        h.document_limit = 3
        h.build()
        h.drop(make_scan(letter(1, "scan")))
        h.settle()
        with_text = h.paperless.docs[h.only_item().state["doc_id"]]["versions"][0]["data"]
        has_text = h.paperless.add_old_scan(with_text)
        others = [h.paperless.add_old_scan(make_scan(letter(1, f"old {n}")).pdf) for n in range(5)]
        for _ in range(8):
            look(h)
        self.assertEqual(self.record(h, has_text).status, NOT_NEEDED)       # looking costs nothing, so it isn't counted
        self.assertEqual([self.record(h, doc_id).status for doc_id in others[:3]], [DONE] * 3)
        self.assertEqual([h.documents.exists(doc_id) for doc_id in others[3:]], [False, False])
        self.assertEqual(h.google.calls, 4)
        h.document_limit = None                                             # the release after lifts it
        h.build()
        h.settle()
        self.assertEqual([self.record(h, doc_id).status for doc_id in others], [DONE] * 5)

    def test_stored_documents_leave_a_quarter_of_the_days_google_pages_for_scans(self):
        h = Harness(self, daily_page_cap=8)                                 # their share is 6 pages
        docs = [h.paperless.add_old_scan(make_scan(letter(3, f"old {n}")).pdf) for n in range(3)]
        small = h.paperless.add_old_scan(make_scan(letter(1, "c")).pdf)
        for _ in range(4):
            look(h)
        self.assertEqual([self.record(h, doc_id).status for doc_id in docs], [DONE, DONE, LATER])
        self.assertIn("share of today's Google pages", self.record(h, docs[2]).state["reason"])
        self.assertFalse(h.documents.exists(small))                         # nothing new is started today
        self.assertEqual(h.google.calls, 2)
        h.drop(make_scan(letter(2, "new")))                                 # a scan the same day still goes through
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED, h.only_item().state.get("reason"))
        for _ in range(40):                                                 # waiting is not a failed try, however long
            look(h, 1800)
            if h.documents.exists(small) and self.record(h, small).status == DONE:
                break
        self.assertEqual([self.record(h, doc_id).status for doc_id in [*docs, small]], [DONE] * 4)
        self.assertEqual(self.record(h, docs[2]).state["tries"], {})

    def test_once_one_document_waits_for_tomorrow_no_new_one_is_fetched_today(self):
        # Otherwise every remaining file would be fetched onto the Green at once, only to wait there.
        h = Harness(self, daily_page_cap=8)                                 # their share is 6 pages
        docs = [h.paperless.add_old_scan(make_scan(letter(3, tag)).pdf) for tag in ("a", "b", "c", "d", "e")]
        for _ in range(6):
            look(h)
        self.assertEqual([self.record(h, doc_id).status for doc_id in docs[:3]], [DONE, DONE, LATER])
        self.assertEqual([h.documents.exists(doc_id) for doc_id in docs[3:]], [False, False])
        self.assertEqual(h.paperless.calls.count("download_original"), 3 + 2)   # three originals, two read-backs
        for _ in range(3):                                                  # three more days at two a day
            h.clock.advance(24 * HOUR)
            for _ in range(4):
                look(h)
        self.assertEqual([self.record(h, doc_id).status for doc_id in docs], [DONE] * 5)

    def test_a_second_try_also_keeps_to_the_days_share(self):
        h = Harness(self, daily_page_cap=10)                                # their share is 7 pages
        first = h.paperless.add_old_scan(make_scan(letter(3, "a")).pdf)
        h.google.fail = [503] * 200
        h.settle()
        self.assertEqual(self.record(h, first).status, LATER)
        h.google.fail = []
        others = [h.paperless.add_old_scan(make_scan(letter(3, tag)).pdf) for tag in ("b", "c")]
        for _ in range(3):
            look(h)
        self.assertEqual([self.record(h, doc_id).status for doc_id in others], [DONE, DONE])   # 6 of 7 pages used
        asked = h.google.calls
        look(h, HOUR)                                                       # the first one's hour is up
        self.assertEqual(h.google.calls, asked)                             # 6 + 3 is over the share: not today
        self.assertEqual(self.record(h, first).status, LATER)
        h.clock.advance(24 * HOUR)
        look(h)
        self.assertEqual(self.record(h, first).status, DONE)

    def test_a_document_bigger_than_a_days_share_is_set_aside_and_blocks_nothing(self):
        h = Harness(self, daily_page_cap=4)                                 # their share is 3 pages
        big = h.paperless.add_old_scan(make_scan(letter(4, "big")).pdf)
        small = h.paperless.add_old_scan(make_scan(letter(1, "c")).pdf)
        for _ in range(3):
            look(h)
        record = self.record(h, big)
        self.assertEqual(record.status, NEXT_RELEASE)
        self.assertIn("more pages than the app sends to Google in a day", record.state["reason"])
        self.assertEqual(self.record(h, small).status, DONE)
        self.assertEqual(h.google.calls, 1)


class WorkerTests(Base):
    """In the app each document is worked in a child process. One that dies must not spin the loop."""

    class Dies:
        def __init__(self):
            self.runs = 0

        def __call__(self, item):
            raise AssertionError("no scan is waiting in this test")

        def document(self, record):
            self.runs += 1
            return -9

    def test_a_worker_that_dies_counts_as_a_try_and_three_are_enough(self):
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        runner = self.Dies()
        service = Service(h.settings, h.store, h.pipeline, h.notifier, clock=h.clock, runner=runner,
                          documents=h.documents)
        self.assertTrue(service.step())
        record = self.record(h, doc_id)
        self.assertEqual(record.status, LATER)
        self.assertIn("exit code -9", record.state["reason"])
        self.assertFalse(service.step())                                    # not again until the hour is up
        for _ in range(5):
            h.clock.advance(HOUR + 1)
            service.step()
        record = self.record(h, doc_id)
        self.assertEqual((record.status, runner.runs), (NEXT_RELEASE, 3))
        self.assertEqual((h.notifier.sent, len(h.paperless.docs[doc_id]["versions"])), ([], 1))

    def test_work_that_keeps_being_cut_short_together_with_the_whole_app_is_set_aside(self):
        # A power cut is one of these. A document that takes the app down every time must not do it for ever.
        h = Harness(self)
        doc_id = h.paperless.add_old_scan(make_scan(letter(1)).pdf)
        handed_out = 0
        for _ in range(8):                                                  # each time the app dies with it in hand
            handed_out += h.documents.step() is not None
            h.build()
        record = self.record(h, doc_id)
        self.assertEqual((handed_out, record.status), (4, NEXT_RELEASE))    # once, and three more tries
        self.assertIn("the app stopped 3 times while working on it", record.state["reason"])
        self.assertEqual((h.google.calls, h.notifier.sent, len(h.paperless.docs[doc_id]["versions"])), (0, [], 1))
        a_newer_release_starts(h)
        h.settle()
        self.assertEqual(self.record(h, doc_id).status, DONE)               # the next release gets its own tries

    def test_a_document_chosen_by_a_newer_release_is_marked_before_its_worker_starts(self):
        h = Harness(self)
        h.drop(make_scan(letter(2), touching=True))
        with badly_fitted():
            h.settle()
        doc_id = h.only_item().state["doc_id"]
        a_newer_release_starts(h)
        runner = self.Dies()
        service = Service(h.settings, h.store, h.pipeline, h.notifier, clock=h.clock, runner=runner,
                          documents=h.documents)
        for _ in range(3):
            service.step()
        self.assertEqual((runner.runs, self.record(h, doc_id).status), (1, LATER))


class StartingANewReleaseTests(Base):
    """What 1.3.0 finds on the Green when it first starts."""

    def test_scans_stored_by_earlier_releases_get_their_records_at_the_first_start(self):
        h = Harness(self)
        for n in range(3):
            h.drop(make_scan(letter(1, f"scan {n}")), name=f"2026110{n + 1}_093000_00040{n}.pdf")
        h.settle()
        ids = sorted(item.state["doc_id"] for item in h.store.all())
        shutil.rmtree(h.documents.dir)
        for item in h.store.all():
            item.state["prepared_by"] = "1.1.3"
            item.save()
        h.build()
        self.assertEqual(h.documents.adopt_scans(), 3)
        self.assertEqual(h.documents.adopt_scans(), 0)
        self.assertEqual([(record.doc_id, record.status, record.state["by"]) for record in h.documents.all()],
                         [(doc_id, DONE, "1.1.3") for doc_id in ids])
        self.assertNothingToDo(h)

    def test_a_scan_an_earlier_release_left_half_way_through_a_redo_is_put_back_and_handed_over(self):
        h = Harness(self)
        h.drop(make_scan(letter(2), touching=True))
        with badly_fitted():
            h.settle()
        item = h.only_item()
        doc_id = item.state["doc_id"]
        doc = h.paperless.docs[doc_id]
        shutil.rmtree(h.documents.dir)
        # As 1.2.0 left it when the update arrived: moved back to "captured" with a snapshot to restore.
        snapshot = {key: item.state.get(key) for key in ("status", "mode", "reason", "reason_kind",
                                                         "searchable_sha256", "page_order")}
        item.state.update(status="captured", copy_added_later=True, copy_retry_restore=snapshot, prepared_by="1.1.2",
                          copy_retry_by="1.2.0", reason=None)
        item.save()
        h.build()
        self.assertEqual(h.pipeline.settle_old_states(), [item.sha])
        self.assertEqual(h.documents.adopt_scans(), 1)
        self.assertEqual(h.pipeline.reprepare_unfinished(), [])
        item = h.only_item()
        self.assertEqual((item.status, item.state["mode"], item.state.get("copy_added_later")),
                         (COMMITTED_WARNING, "with_google", False))
        self.assertFalse(h.pipeline.due(item))                              # the scan's own route is finished with it
        h.settle()
        self.assertEqual(self.record(h, doc_id).status, DONE)               # and the loop did the redo
        self.assertEqual((len(h.paperless.docs), len(doc["versions"]), len(doc["notes"])), (1, 3, 2))

    def test_a_scan_that_hasnt_reached_paperless_is_prepared_again_by_the_new_release(self):
        # Both of the first real scans were waiting on Paperless when the fix for their page order was ready.
        h = Harness(self)
        h.paperless.unreachable = True
        h.drop(make_scan(letter(3), turn=180, flipped=True))
        h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["retry_stage"], item.state["mode"]), (WAITING, "commit", "with_google"))
        asked = h.google.calls
        self.assertEqual(h.pipeline.reprepare_unfinished(), [])             # the same release leaves it alone
        item.state["prepared_by"] = "1.1.1-earlier"
        item.state["page_order"] = [1, 2, 3]                                # as the earlier release left it
        item.save()
        h.paperless.unreachable = False
        h.build()
        self.assertEqual(h.pipeline.reprepare_unfinished(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["page_order"], [3, 2, 1])
        self.assertEqual(h.google.calls, asked)                             # Google's saved answer was reused
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual((len(h.paperless.docs), len(doc["versions"]), len(doc["notes"])), (1, 2, 1))
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)

    def test_a_scan_ready_to_go_in_without_its_copy_is_prepared_again_first(self):
        from paperless_intake.docai.pdf_renderer import ReviewRequired
        h = Harness(self)

        def refuse(*args, **kwargs):
            raise ReviewRequired("Only a direct JPEG image without masks is supported")

        h.pipeline.renderer.render = refuse
        h.drop(make_scan(letter(2), encodings=["ccitt", "jpeg"]))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline.prepare(item)                                            # stopped here, as the update did
        self.assertEqual((item.status, item.state["mode"]), ("ready", "original_only"))
        item.state["prepared_by"] = "1.1.0"
        item.save()
        asked = h.google.calls
        h.build()
        self.assertEqual(h.pipeline.reprepare_unfinished(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (2, 1))
        self.assertEqual((alert_kinds(h), h.google.calls), ([], asked))     # nothing said; Google's answer reused
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)

    def test_a_scan_already_handed_to_paperless_finishes_as_it_is_and_the_loop_improves_it(self):
        # The app died after writing "only the original was stored" and before it recorded the commit.
        from paperless_intake.docai.pdf_renderer import ReviewRequired
        h = Harness(self)

        def refuse(*args, **kwargs):
            raise ReviewRequired("Only a direct JPEG image without masks is supported")

        h.pipeline.renderer.render = refuse
        write_note = h.pipeline._ensure_note

        def write_note_then_die(*args, **kwargs):
            write_note(*args, **kwargs)
            raise Crash("after the note was written and recorded")

        h.pipeline._ensure_note = write_note_then_die
        h.drop(make_scan(letter(2), encodings=["ccitt", "jpeg"]))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline.prepare(item)
        with self.assertRaises(Crash):
            h.pipeline.commit(item)
        self.assertEqual((item.status, item.state.get("note_added")), ("committing", True))
        item.state["prepared_by"] = "1.1.0"
        item.save()
        h.build()                                                           # the new release, which can build the copy
        self.assertEqual(h.pipeline.reprepare_unfinished(), [])             # Paperless has the original: not prepared again
        h.settle()
        item = h.only_item()
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual(item.status, COMMITTED_WARNING)                    # the scan finished as the old release left it
        self.assertEqual(self.record(h, item.state["doc_id"]).status, DONE)  # and then the loop gave it its copy
        self.assertEqual((len(h.paperless.docs), len(doc["versions"]), len(doc["notes"])), (1, 2, 2))
        self.assertIn("Only the original was stored", doc["notes"][0])
        self.assertIn("added the searchable version", doc["notes"][1])
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])


if __name__ == "__main__":
    unittest.main()
