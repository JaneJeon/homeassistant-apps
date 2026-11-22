"""The stack is one rigid thing: how it went into the feeder decides turn and order for every page.

Unit tests for the two stack decisions, then whole-pipeline tests with scans made
the way a stack really goes in: turned head to foot, flipped over, skewed.
Each pipeline test ends at what Jane would see in Paperless.
"""

from __future__ import annotations

import io
import unittest
import unittest.mock

from pypdf import PdfReader

from paperless_intake import checks
from paperless_intake.docai import glyphless, stack
from paperless_intake.docai.google_backend import GoogleBackendError
from paperless_intake.docai.page_order import plan_page_order
from paperless_intake.docai.validation import inspect_source
from paperless_intake.store import COMMITTED, COMMITTED_WARNING, HELD, WAITING

from synth import PageSpec, make_scan, numbered_letter
from test_page_order import document, line
from test_pipeline import Harness
from test_checks_and_web import plan_for


def reading(cardinal, words):
    return {"cardinal": cardinal, "words": words, "source": "transform"}


class RotationTests(unittest.TestCase):
    def test_a_page_with_little_text_follows_a_clear_stack(self):
        turns, decisions = stack.decide_rotations([reading(180, 200), reading(0, 3), reading(180, 150)])
        self.assertEqual(turns, [180, 180, 180])
        self.assertTrue(decisions[1]["followedStack"])
        self.assertFalse(decisions[0]["followedStack"])

    def test_a_page_with_plenty_of_text_keeps_its_own_reading(self):
        # A sheet printed with its back upside down: the stack is upright, this page isn't.
        turns, _ = stack.decide_rotations([reading(0, 200), reading(180, 120), reading(0, 180), reading(0, 150)])
        self.assertEqual(turns, [0, 180, 0, 0])

    def test_one_dense_upside_down_page_doesnt_speak_for_the_stack(self):
        # A bill with terms printed upside down on the back, and a short slip: the slip stays upright.
        turns, _ = stack.decide_rotations([reading(0, 100), reading(180, 500), reading(0, 8)])
        self.assertEqual(turns, [0, 180, 0])
        turns, _ = stack.decide_rotations([reading(0, 120), reading(180, 600), reading(0, 90), reading(180, 600),
                                           reading(0, 6)])
        self.assertEqual(turns[-1], 0)

    def test_no_clear_stack_means_every_page_keeps_its_own(self):
        turns, decisions = stack.decide_rotations([reading(0, 100), reading(180, 100), reading(0, 4)])
        self.assertEqual(turns, [0, 180, 0])
        self.assertIsNone(decisions[2]["stackHeadToFoot"])

    def test_a_sideways_page_with_plenty_of_text_keeps_its_own_turn(self):
        turns, _ = stack.decide_rotations([reading(180, 200), reading(90, 150), reading(180, 100)])
        self.assertEqual(turns, [180, 90, 180])

    def test_a_nearly_empty_page_follows_the_stack_even_when_google_reads_it_as_sideways(self):
        # The second real scan: three upright pages and a back page with one word on it,
        # which Google read as turned 270. One word is not enough to turn a page on.
        turns, decisions = stack.decide_rotations([reading(0, 337), reading(0, 79), reading(0, 357), reading(270, 1)])
        self.assertEqual(turns, [0, 0, 0, 0])
        self.assertTrue(decisions[3]["followedStack"])
        turns, _ = stack.decide_rotations([reading(180, 200), reading(90, 5), reading(180, 100)])
        self.assertEqual(turns, [180, 180, 180])

    def test_only_weak_pages_keep_what_google_said(self):
        turns, _ = stack.decide_rotations([reading(180, 5), reading(0, 2)])
        self.assertEqual(turns, [180, 0])


def order_of(*pages):
    data = document(*pages)
    plan = plan_page_order(data)
    return plan, stack.decide_order(plan, len(pages))


class OrderTests(unittest.TestCase):
    def test_complete_reversed_series_like_document_19(self):
        # Six pages scanned 6..1, with a two-page form inside that has its own numbering.
        pages = [[line(f"Page {n} of 6")] for n in (6, 5, 4, 3, 2, 1)]
        pages[2].append(line("PAGE 2 OF 2", y=0.05))
        pages[3].append(line("PAGE 1 OF 2", y=0.05))
        _, order = order_of(*pages)
        self.assertEqual(order["mapping"], [6, 5, 4, 3, 2, 1])
        self.assertTrue(order["verified"])

    def test_two_pages_of_a_longer_letter_scanned_flipped_like_document_35(self):
        plan, order = order_of([line("Page 4 of 4")], [line("Page 3 of 4")])
        self.assertEqual((order["mapping"], order["status"]), ([2, 1], "reversed_stack_from_printed_numbers"))
        # The order is a matter for the copy. The two pages that aren't there are a matter for Jane.
        self.assertIn("pages 1 and 2 of 4 are missing", checks.missing_pages(plan, 2))

    def test_one_numbered_page_votes_by_its_position_like_document_41(self):
        _, as_scanned = order_of([line("An unnumbered first page", y=0.5)], [line("Page 2 of 2")])
        self.assertEqual((as_scanned["mapping"], as_scanned["status"]), ([1, 2], "partial_in_order"))
        _, flipped = order_of([line("Page 2 of 2")], [line("An unnumbered first page", y=0.5)])
        self.assertEqual((flipped["mapping"], flipped["status"]), ([2, 1], "reversed_stack_from_printed_numbers"))

    def test_a_middle_page_says_nothing_by_position(self):
        _, order = order_of([line("text", y=0.5)], [line("Page 2 of 3")], [line("text", y=0.5)])
        self.assertEqual((order["mapping"], order["status"]), ([1, 2, 3], "unverified_numbers_inconclusive"))

    def test_partly_numbered_stack_running_backwards_is_reversed_whole(self):
        _, order = order_of([line("Page 4 of 4")], [line("An insert", y=0.5)], [line("Page 2 of 4")],
                            [line("Page 1 of 4")])
        self.assertEqual((order["mapping"], order["status"]), ([4, 3, 2, 1], "reversed_stack_from_printed_numbers"))
        self.assertFalse(order["verified"])

    def test_numbers_that_point_both_ways_move_nothing(self):
        _, order = order_of([line("Page 2 of 5")], [line("Page 1 of 5")], [line("Page 4 of 5")])
        self.assertEqual((order["mapping"], order["status"]), ([1, 2, 3], "unverified_mixed_evidence"))

    def test_repeated_numbers_dont_vote(self):
        # The same number on two pages means two documents share the series. Without this guard
        # the one step down (2 to 1) would reverse the stack.
        _, order = order_of([line("Page 2 of 2")], [line("Page 2 of 2")], [line("Page 1 of 2")])
        self.assertEqual((order["mapping"], order["reverseVotes"]), ([1, 2, 3], 0))

    def test_numbering_that_starts_again_is_not_a_flipped_stack(self):
        # A statement whose first page has no number, then an enclosure starting at "Page 1". Fed correctly.
        _, order = order_of([line("Statement", y=0.5)], [line("Page 2")], [line("Page 1")])
        self.assertEqual((order["mapping"], order["status"]), ([1, 2, 3], "unverified_numbers_inconclusive"))

    def test_a_step_down_onto_one_does_vote_when_a_total_is_printed(self):
        _, order = order_of([line("An insert", y=0.5)], [line("Page 2 of 5")], [line("Page 1 of 5")])
        self.assertEqual((order["mapping"], order["status"]), ([3, 2, 1], "reversed_stack_from_printed_numbers"))

    def test_a_page_number_that_is_referred_to_is_not_that_pages_number(self):
        plan, order = order_of([line("See page 4 for terms")], [line("Page 2")])
        self.assertEqual(order["mapping"], [1, 2])
        self.assertIsNone(checks.missing_pages(plan, 2))
        _, order = order_of([line("Continued on page 2")], [line("Page 2")])
        self.assertEqual(order["mapping"], [1, 2])

    def test_doubtful_numbers_dont_vote(self):
        _, order = order_of([line("Page 2 of 2", numeric_confidence=0.4)], [line("Page 1 of 2", numeric_confidence=0.4)])
        self.assertEqual(order["mapping"], [1, 2])

    def test_no_numbers_no_change(self):
        _, order = order_of([line("A letter", y=0.5)], [line("Its second page", y=0.5)])
        self.assertEqual((order["mapping"], order["status"]), ([1, 2], "unknown_no_pagination_evidence"))


def runs_on_page(pdf_bytes: bytes, page_index: int) -> list[tuple[str, list[float]]]:
    """Every invisible run on a page, with the text matrix it was stored with."""
    runs, matrix = [], None
    for operands, operator in PdfReader(io.BytesIO(pdf_bytes)).pages[page_index].get_contents().operations:
        if operator == b"Tm":
            matrix = [float(value) for value in operands]
        elif operator == b"Tj":
            raw = operands[0]
            data = raw.original_bytes if hasattr(raw, "original_bytes") else bytes(raw)
            runs.append((data.decode("utf-16-be").strip(), matrix))
    return runs


def expected_box(scan, source_page: int, word_index: int):
    """Where a word is printed on the upright page, in PDF points: left, right, bottom, top."""
    words = [(word, box) for _, _, line_words in scan.boxes[source_page - 1] for word, box in line_words]
    word, (x0, y0, x1, y1) = words[word_index]
    width, height = scan.size
    return word, x0 / width * 612, x1 / width * 612, 792 - y1 / height * 792, 792 - y0 / height * 792


class StackThroughThePipelineTests(unittest.TestCase):
    def commit(self, scan, **kwargs):
        h = Harness(self, **kwargs)
        h.drop(scan)
        h.settle()
        return h, h.only_item()

    def assertTextSitsOnTheWords(self, scan, item, *, output_page: int, source_page: int):
        runs = runs_on_page(item.searchable_path.read_bytes(), output_page - 1)
        for index in (0, len(runs) // 2, len(runs) - 1):
            word, left, right, bottom, top = expected_box(scan, source_page, index)
            text, (a, b, c, d, e, f) = runs[index]
            self.assertEqual(text, word)
            width = glyphless.ADVANCE * len(word)
            self.assertAlmostEqual(e, left, delta=0.6)
            self.assertAlmostEqual(e + a * width, right, delta=0.6)
            self.assertAlmostEqual(f, bottom, delta=0.6)
            self.assertAlmostEqual(f + d, top, delta=0.6)
            self.assertAlmostEqual(b, 0, delta=1e-6)       # laid square, whatever the scan's skew

    def test_stack_turned_and_flipped_comes_out_upright_and_in_order(self):
        scan = numbered_letter(4, flipped=True, turn=180)
        h, item = self.commit(scan)
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["page_order"], [4, 3, 2, 1])
        self.assertEqual(item.state["rotations"], [180, 180, 180, 180])
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual(doc["versions"][-1]["data"], scan.pdf)          # the original is stored as it was scanned
        self.assertIn("reordered to follow the printed page numbers", doc["notes"][0])
        self.assertIn("4 of 4 pages were turned upright", doc["notes"][0])
        first = PdfReader(io.BytesIO(item.searchable_path.read_bytes())).pages[0]
        self.assertEqual((float(first.mediabox.width), float(first.mediabox.height)), (612.0, 792.0))
        self.assertTextSitsOnTheWords(scan, item, output_page=1, source_page=4)
        self.assertEqual([p["kind"] for k, p in h.notifier.sent if k == "alert"], [])

    def test_sideways_stack_comes_out_upright(self):
        for turn in (90, 270):
            with self.subTest(turn=turn):
                scan = numbered_letter(2, turn=turn)
                _, item = self.commit(scan)
                self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
                self.assertEqual(item.state["rotations"], [turn, turn])
                self.assertTextSitsOnTheWords(scan, item, output_page=2, source_page=2)

    def test_partly_numbered_flipped_stack_is_reversed_and_says_why(self):
        h, item = self.commit(numbered_letter(4, flipped=True, unnumbered=(1,)))      # numbered from "Page 2 of 4"
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["page_order"], [4, 3, 2, 1])
        self.assertIn("ran backwards", h.paperless.docs[item.state["doc_id"]]["notes"][0])

    def test_a_skewed_scan_goes_through_and_its_words_read_back_whole(self):
        for skew in (1.3, -2.0):
            with self.subTest(skew=skew):
                h, item = self.commit(numbered_letter(2, skew=skew))
                self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
                self.assertEqual(item.state["rotations"], [0, 0])
                self.assertEqual([p["kind"] for k, p in h.notifier.sent if k == "alert"], [])

    def test_a_page_google_cant_judge_alone_is_turned_with_the_stack(self):
        specs = [PageSpec([f"Synthetic account statement line {n}.{i} for testing purposes only" for i in range(1, 6)],
                          footer=f"Page {n} of 2") for n in (1, 2)]
        specs.append(PageSpec(["Thank you"], unturned=True))          # a slip with two words and no page number
        scan = make_scan(specs, turn=180)
        h, item = self.commit(scan)
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["rotations"], [180, 180, 180])
        self.assertEqual(item.state["rotations_from_stack"], 1)
        self.assertIn("turned like the rest of the stack", h.paperless.docs[item.state["doc_id"]]["notes"][0])
        self.assertTextSitsOnTheWords(scan, item, output_page=3, source_page=3)

    def test_a_turned_page_reported_by_label_is_turned_too(self):
        scan = numbered_letter(3, turn=180, report="label")
        _, item = self.commit(scan)
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["rotations"], [180, 180, 180])
        self.assertTextSitsOnTheWords(scan, item, output_page=1, source_page=1)

    def test_text_in_scripts_no_font_covers_goes_into_the_layer(self):
        body = ["Important notice about your coverage and language assistance services",
                "주의: 한국어를 사용하시는 경우 언어 지원 서비스를 무료로 이용하실 수 있습니다",
                "注意：如果您使用繁體中文，您可以免費獲得語言援助服務",
                "โปรดทราบ ถ้าคุณพูดภาษาไทย คุณสามารถใช้บริการช่วยเหลือทางภาษาได้ฟรี"]
        h, item = self.commit(make_scan([PageSpec(body)]))
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))       # 1.0 held this scan
        text = " ".join(run for run, _ in runs_on_page(item.searchable_path.read_bytes(), 0))
        for word in ("한국어를", "注意：如果您使用繁體中文，您可以免費獲得語言援助服務", "ภาษาไทย"):
            self.assertIn(word, text)
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 2)

    def test_text_that_cant_be_checked_against_googles_image_still_commits_with_a_remark(self):
        h, item = self.commit(numbered_letter(2, google_image="inverted"))
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))       # 1.0 held this scan
        note = h.paperless.docs[item.state["doc_id"]]["notes"][0]
        self.assertIn("doesn't match Google's page image", note)
        self.assertIn("The page images aren't affected", note)
        self.assertEqual([p["kind"] for k, p in h.notifier.sent if k == "alert"], [])     # a remark, not an alert

    def test_a_scan_google_cant_read_goes_in_as_the_original_and_jane_is_told(self):
        h = Harness(self)
        h.google.fail = [400]
        scan = numbered_letter(2)
        h.drop(scan)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING, item.state.get("reason"))
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual([v["data"] for v in doc["versions"]], [scan.pdf])
        self.assertIn("Only the original was stored", doc["notes"][0])
        alerts = [p for k, p in h.notifier.sent if k == "alert"]
        self.assertEqual([p["kind"] for p in alerts], ["warning"])
        self.assertIn("no Google text or upright copy", alerts[0]["gap"])

    def test_arabic_and_hebrew_read_back_the_right_way_round(self):
        body = ["Important notice about your coverage and language assistance services",
                "ملحوظة: إذا كنت تتحدث اللغة العربية فإن خدمات المساعدة اللغوية متاحة",
                "שים לב: אם אתה מדבר עברית שירותי סיוע בשפה זמינים"]
        h, item = self.commit(make_scan([PageSpec(body)]))
        # Committed without a warning means Paperless's text (pulled out with pdftotext) has Google's words.
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        read_back = [e for e in item.events() if e["event"] == "read_back"][0]
        self.assertEqual(read_back["text_match"]["missing_words"], 0, read_back["text_match"])

    def test_a_missing_page_still_holds_when_the_copy_cant_be_built(self):
        h = Harness(self)

        def broken(*args, **kwargs):
            raise KeyError("a shape of Google answer the renderer never met")

        h.pipeline.renderer.render = broken
        h.drop(numbered_letter(3, missing=(2,)))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, HELD)
        self.assertIn("page 2 of 3 is missing", item.state["reason"])
        self.assertEqual(h.paperless.docs, {})

    def test_a_bad_google_setting_waits_and_says_so_at_once(self):
        h = Harness(self)

        def refused(item):
            raise GoogleBackendError("pinned_ocr_version_in_allowed_project_us_required")

        h.pipeline.backend_factory = refused
        h.drop(numbered_letter(2))
        h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"]), (WAITING, "google"))
        alerts = [p for k, p in h.notifier.sent if k == "alert"]
        self.assertEqual([p["kind"] for p in alerts], ["waiting"])
        self.assertIn("pinned_ocr_version", alerts[0]["gap"])

    def test_an_unexpected_error_says_so_at_once_and_only_once(self):
        h = Harness(self)

        def boom(*args, **kwargs):
            raise RuntimeError("something nobody planned for")

        h.pipeline._plan = boom          # fails on every try, cached Google answer or not
        h.drop(numbered_letter(2))
        h.settle()
        h.clock.advance(31 * 60)
        h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["reason_kind"]), (WAITING, "unexpected"))
        self.assertEqual([p["kind"] for k, p in h.notifier.sent if k == "alert"], ["waiting"])

    def test_a_damaged_local_copy_warns_instead_of_holding(self):
        h = Harness(self)
        scan = numbered_letter(2)
        h.drop(scan)
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline.prepare(item)
        item.searchable_path.write_bytes(b"%PDF-1.7 not the copy that was checked")
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING, item.state.get("reason"))
        self.assertEqual([v["data"] for v in h.paperless.docs[item.state["doc_id"]]["versions"]], [scan.pdf])
        self.assertIn("changed on disk", item.state["reason"])

    def test_a_defect_in_building_the_copy_cant_strand_a_scan(self):
        h = Harness(self)

        def broken(*args, **kwargs):
            raise KeyError("a shape of Google answer the renderer never met")

        h.pipeline.renderer.render = broken
        scan = numbered_letter(2)
        h.drop(scan)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING, item.state.get("reason"))
        self.assertEqual([v["data"] for v in h.paperless.docs[item.state["doc_id"]]["versions"]], [scan.pdf])
        self.assertIn("an unexpected KeyError in the app", item.state["reason"])

    def test_google_refusing_the_setup_waits_and_alerts_instead_of_skipping_google(self):
        h = Harness(self)
        h.google.fail = [403]
        h.drop(numbered_letter(2))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, WAITING)
        self.assertEqual(h.paperless.docs, {})
        self.assertEqual([p["kind"] for k, p in h.notifier.sent if k == "alert"], ["waiting"])

    def test_two_pages_of_a_longer_letter_are_held_and_can_be_kept_whole(self):
        specs = [PageSpec([f"Synthetic account statement line {n}.{i} for testing purposes only" for i in range(1, 6)],
                          footer=f"Page {n} of 4") for n in (3, 4)]
        h, item = self.commit(make_scan(specs, flipped=True, turn=180))
        self.assertEqual(item.status, HELD)
        self.assertIn("pages 1 and 2 of 4 are missing", item.state["reason"])
        h.pipeline.keep_as_it_is(item)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["page_order"], [2, 1])
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 2)


class RequeueTests(unittest.TestCase):
    def test_scans_the_old_checks_held_are_put_through_again_once(self):
        h = Harness(self)
        scan = numbered_letter(2, skew=1.3)
        h.drop(scan)
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        # What version 1.0 left behind: Google's answer saved, the scan held over the searchable
        # copy, the alert sent and the watchdog told.
        h.pipeline.backend_factory(item).process(inspect_source(item.source_path), item.new_work_dir())
        self.assertEqual(h.google.calls, 1)
        item.state.update(finished_sent=True, alert_open=True)
        item.set(HELD, reason="Google's text couldn't be lined up with the scanned page exactly",
                 reason_kind="render_check", held_reason="Google's text couldn't be lined up with the scanned page exactly")
        h.notifier.sent.clear()

        h.build()                                                   # the new version starts
        self.assertEqual(h.pipeline.requeue_obsolete_holds(), [item.sha])
        self.assertEqual(h.pipeline.requeue_obsolete_holds(), [])   # once
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 2)
        self.assertEqual(h.google.calls, 1)                         # Google's saved answer was reused, not paid for again
        sent = [(kind, payload if kind != "alert" else (payload["kind"], payload["gap"])) for kind, payload in h.notifier.sent]
        self.assertEqual(sent, [("alert", ("resolved", "committed"))])      # the old alert is cleared; no second "finished"

    def test_a_scan_held_for_a_missing_page_stays_held(self):
        h = Harness(self)
        h.drop(numbered_letter(3, missing=(2,)))
        h.settle()
        h.build()
        self.assertEqual(h.pipeline.requeue_obsolete_holds(), [])
        self.assertEqual(h.only_item().status, HELD)


if __name__ == "__main__":
    unittest.main()


def alert_kinds(h) -> list[str]:
    return [payload["kind"] for kind, payload in h.notifier.sent if kind == "alert"]


def stored_images(pdf: bytes) -> list[list[tuple[str, int, str]]]:
    """Per page, every image as stored: format, byte count and hash."""
    from paperless_intake.docai import pdf_renderer
    return [pdf_renderer._fingerprint(pdf_renderer.page_images(page)) for page in PdfReader(io.BytesIO(pdf)).pages]


def letter(count: int) -> list[PageSpec]:
    return [PageSpec([f"Statement page {n} with enough words on it to judge which way up it is",
                      "A second line of ordinary text for this page of the test letter",
                      "And a third so the page counts as one with plenty of text"], footer=f"Page {n} of {count}")
            for n in range(1, count + 1)]


class WhatTheFirstRealScansShowedTests(unittest.TestCase):
    """Two real scans went through on 2026-11-22. Each showed something no made-up scan had."""

    def commit(self, scan):
        h = Harness(self)
        h.drop(scan)
        h.settle()
        return h, h.only_item()

    def test_page_numbers_are_read_on_a_turned_stack_however_google_labels_it(self):
        # The first real scan went in turned and flipped. Its pages came out upright, and in
        # reverse: "Page 6 of 6" first. Every page number had been thrown away, because Google
        # labelled the lines of a turned page one way and the page another.
        for labels in ("as_page", "as_scanned", "page_as_scanned"):
            with self.subTest(labels=labels):
                scan = make_scan(letter(6), turn=180, flipped=True, line_labels=labels,
                                 encodings=["jpeg", "jpeg", "ccitt", "ccitt", "ccitt", "jpeg"])
                h, item = self.commit(scan)
                self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
                self.assertEqual(item.state["page_order"], [6, 5, 4, 3, 2, 1])
                self.assertEqual(item.state["rotations"], [180] * 6)
                self.assertIn(item.state["order_status"],
                              {"reorder_from_complete_numbering", "reversed_stack_from_printed_numbers"})

    def test_page_numbers_at_the_top_of_some_pages_and_the_bottom_of_others_are_one_series(self):
        # A statement with inserts prints them in different places. Kept apart, each place looks gappy.
        pages = [PageSpec(["Statement page with enough words on it to count as text"] * 3,
                          footer=None if n in (3, 4, 5) else f"Page {n} of 6") for n in range(1, 7)]
        for n in (3, 4, 5):
            pages[n - 1].lines.insert(0, f"Page {n} of 6")
        scan = make_scan(pages, turn=180, flipped=True, line_labels="as_scanned")
        _, plan = plan_for(scan)
        self.assertGreater(len([e for e in plan.evidence if e.get("kind") == "series"]), 1)   # two bands, as on paper
        self.assertIsNone(checks.missing_pages(plan, 6))
        h, item = self.commit(scan)
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["page_order"], [6, 5, 4, 3, 2, 1])

    def test_a_page_really_missing_from_such_a_document_still_holds(self):
        pages = [PageSpec(["Statement page with enough words on it to count as text"] * 3,
                          footer=None if n in (3, 4, 5) else f"Page {n} of 6") for n in (1, 2, 3, 5, 6)]
        for page, n in zip(pages, (1, 2, 3, 5, 6)):
            if n in (3, 5):
                page.lines.insert(0, f"Page {n} of 6")
        _, plan = plan_for(make_scan(pages))
        self.assertIn("page 4 of 6 is missing", checks.missing_pages(plan, 5))

    def test_two_documents_that_count_to_the_same_total_are_each_checked_by_themselves(self):
        # Document A numbers its three pages at the top. Document B numbers at the bottom and lost its page 2.
        pages = [PageSpec([f"Page {n} of 3"] + ["First document, with enough words on the page to count"] * 3) for n in (1, 2, 3)]
        pages += [PageSpec(["Second document, with enough words on the page to count"] * 3, footer=f"Page {n} of 3") for n in (1, 3)]
        _, plan = plan_for(make_scan(pages))
        self.assertIn("page 2 of 3 is missing", checks.missing_pages(plan, 5))

    def test_words_whose_boxes_touch_dont_run_together_in_paperless(self):
        # Both real scans lost about one word in twenty this way: "En Español" read back as "EnEspañol".
        for turn in (0, 180):
            with self.subTest(turn=turn):
                h, item = self.commit(make_scan(letter(3), turn=turn, flipped=bool(turn), touching=True))
                self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
                read_back = [e for e in item.events() if e["event"] == "read_back"][0]
                self.assertEqual((read_back["text_match"]["missing_words"], read_back["text_match"]["extra_words"]), (0, 0))

    def test_a_stored_copy_that_read_back_badly_is_built_again_by_the_next_version(self):
        from paperless_intake.docai import glyphless
        h = Harness(self)
        real_fit = glyphless.fit
        scan = make_scan(letter(3), turn=180, flipped=True, touching=True)
        h.drop(scan)
        with unittest.mock.patch.object(glyphless, "fit", lambda points, cells: real_fit(points, max(1, cells - 1))):
            h.settle()                                                      # as 1.1.2 laid the words out
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        self.assertIn("search text differs", item.state["reason"])
        doc = h.paperless.docs[item.state["doc_id"]]
        doc["owner"] = 7                                                    # Jane's since
        self.assertEqual(alert_kinds(h), ["warning"])
        self.assertEqual(h.pipeline.retry_copies(), [])                     # the version that built it leaves it
        item.state["prepared_by"] = "1.1.2"
        item.state.pop("copy_reads_badly", None)                            # 1.1.2 kept only the sentence
        item.save()
        h.build()
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual((len(doc["versions"]), len(doc["notes"]), doc["owner"]), (3, 2, 7))
        self.assertIn("built again by a newer version", doc["notes"][1])
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])
        self.assertEqual(h.pipeline.retry_copies(), [])

    def test_a_failed_try_at_a_better_copy_puts_back_what_was_recorded_about_the_stored_one(self):
        from paperless_intake.docai import glyphless
        h = Harness(self)
        real_fit = glyphless.fit
        h.drop(make_scan(letter(3), turn=180, flipped=True, touching=True))
        with unittest.mock.patch.object(glyphless, "fit", lambda points, cells: real_fit(points, max(1, cells - 1))):
            h.settle()
        item = h.only_item()
        before = {key: item.state.get(key) for key in ("status", "mode", "reason", "searchable_sha256", "page_order")}
        item.state["prepared_by"] = "1.1.2"
        item.save()
        h.build()
        h.google.fail = [503] * 200
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual({key: item.state.get(key) for key in before}, before)
        self.assertEqual(before["mode"], "with_google")
        self.assertEqual(alert_kinds(h), ["warning"])

    def test_a_scan_that_hasnt_finished_going_in_is_prepared_again_by_a_new_version(self):
        # Both real scans were waiting on Paperless when the fix for their page order was ready.
        h = Harness(self)
        scan = make_scan(letter(3), turn=180, flipped=True)
        h.paperless.unreachable = True
        h.drop(scan)
        h.settle()
        item = h.only_item()
        self.assertEqual((item.status, item.state["retry_stage"], item.state["mode"]), (WAITING, "commit", "with_google"))
        asked = h.google.calls
        self.assertEqual(h.pipeline.retry_copies(), [])                     # the same version leaves it alone
        item.state["prepared_by"] = "1.1.1-earlier"
        item.state["page_order"] = [1, 2, 3]                                # as the earlier version left it
        item.save()
        h.paperless.unreachable = False
        h.build()
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["page_order"], [3, 2, 1])
        self.assertEqual(h.google.calls, asked)                             # Google's saved answer was reused
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual((len(h.paperless.docs), len(doc["notes"])), (1, 1))
        self.assertNotIn("added after", doc["notes"][0])


class PageImageFormatTests(unittest.TestCase):
    """A scanner that detects colour saves colour pages as JPEG and black and white pages as
    one-bit images, in the same file. 1.1.0 built a copy only from JPEG pages, and the first
    real scan it met had another kind. The copy must not depend on what a page image is."""

    def commit(self, scan):
        h = Harness(self)
        h.drop(scan)
        h.settle()
        return h, h.only_item()

    def test_colour_and_black_and_white_pages_in_one_scan(self):
        scan = make_scan(letter(4), turn=180, flipped=True, encodings=["jpeg", "ccitt", "flate", "jpeg"])
        h, item = self.commit(scan)
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual((item.state["page_order"], item.state["rotations"]), ([4, 3, 2, 1], [180] * 4))
        self.assertEqual(alert_kinds(h), [])
        versions = h.paperless.docs[item.state["doc_id"]]["versions"]
        self.assertEqual(len(versions), 2)
        # Every page image in the copy is the scanner's own, byte for byte, in its own format.
        original, copy = stored_images(scan.pdf), stored_images(versions[0]["data"])
        self.assertEqual(copy, original[::-1])
        self.assertEqual([page[0][0] for page in original], ["DCTDecode", "FlateDecode", "CCITTFaxDecode", "DCTDecode"])

    def test_a_format_the_app_cant_decode_and_a_page_in_layers(self):
        # Nothing that builds the copy may need to decode a page image.
        scan = make_scan(letter(3), turn=180, encodings=["jbig2", "layered", "jpeg"])
        h, item = self.commit(scan)
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["rotations"], [180] * 3)
        self.assertEqual(item.state["quality_notes"], [])
        versions = h.paperless.docs[item.state["doc_id"]]["versions"]
        self.assertEqual(stored_images(versions[0]["data"]), stored_images(scan.pdf))
        self.assertEqual(len(stored_images(scan.pdf)[1]), 2)          # the layered page has two images

    def test_the_log_line_names_each_pages_format_and_says_what_wasnt_measured(self):
        scan = make_scan(letter(3), encodings=["jpeg", "ccitt", "jbig2"])
        with self.assertLogs("paperless_intake.pipeline", "INFO") as logs:
            self.commit(scan)
        line = next(entry for entry in logs.output if "pages as scanned" in entry)
        self.assertIn("DCTDecode 8-bit, image match 1.000", line)
        self.assertIn("Google labels page PAGE_UP lines 4 PAGE_UP", line)
        self.assertIn("CCITTFaxDecode 1-bit, image match 0.9", line)
        self.assertIn("JBIG2Decode 1-bit, image match not measured", line)
        self.assertNotIn("Statement", line)                           # numbers and format names, never text

    def test_when_a_copy_cant_be_built_the_log_says_what_the_scanner_wrote(self):
        from paperless_intake.docai.pdf_renderer import ReviewRequired
        h = Harness(self)

        def refuse(*args, **kwargs):
            raise ReviewRequired("a page shape nobody has met yet")

        h.pipeline.renderer.render = refuse
        h.drop(make_scan(letter(2), encodings=["ccitt", "layered"]))
        with self.assertLogs("paperless_intake.pipeline", "WARNING") as logs:
            h.settle()
        line = next(entry for entry in logs.output if "as the scanner wrote them" in entry)
        self.assertIn("p1 box [0.0, 0.0, 612.0, 792.0] rotate 0; CCITTFaxDecode DeviceGray 1-bit 1275x1650; draws q cm Do Q", line)
        self.assertIn("p2 box [0.0, 0.0, 612.0, 792.0] rotate 0; DCTDecode DeviceGray 8-bit 1275x1650, "
                      "FlateDecode mask 1-bit 1275x1650; draws q cm Do Q q g cm Do Q", line)
        self.assertNotIn("Statement", line)

    def test_a_page_is_remarked_on_only_when_its_image_match_is_plainly_wrong(self):
        # Real pages whose text sat exactly on the printed words scored 0.83 to 0.999.
        from paperless_intake.docai import pdf_renderer
        provenance = {"pages": [
            {"outputPage": 1, "registration": {"measured": True, "correlation": 0.957, "withinYardstick": False}},
            {"outputPage": 2, "registration": {"measured": True, "correlation": 0.830, "withinYardstick": False}},
            {"outputPage": 3, "registration": {"measured": True, "correlation": 0.12, "withinYardstick": False}},
            {"outputPage": 4, "registration": {"measured": False, "reason": "the page isn't a single image"}}]}
        notes = pdf_renderer.quality_notes(provenance)
        self.assertEqual(len(notes), 1)
        self.assertIn("on page 3 doesn't match Google's page image", notes[0])


class RebuildTheCopyTests(unittest.TestCase):
    """The copy is made from the original, so a copy an earlier version couldn't build can be
    built by a later one: before the scan reaches Paperless, or after."""

    def broken(self, h):
        from paperless_intake.docai.pdf_renderer import ReviewRequired

        def refuse(*args, **kwargs):
            raise ReviewRequired("Only a direct JPEG image without masks is supported")

        real = h.pipeline.renderer.render
        h.pipeline.renderer.render = refuse
        return real

    @staticmethod
    def as_an_earlier_version_left_it(item):
        """1.1.0 recorded the reason as a sentence and nothing else."""
        for key in ("prepared_by", "copy_not_built"):
            item.state.pop(key, None)
        item.save()

    def committed_without_a_copy(self, scan=None):
        h = Harness(self)
        real = self.broken(h)
        h.drop(scan or make_scan(letter(3), turn=180, encodings=["jpeg", "ccitt", "jpeg"]))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        self.assertEqual(len(h.paperless.docs[item.state["doc_id"]]["versions"]), 1)
        self.assertEqual(alert_kinds(h), ["warning"])
        self.as_an_earlier_version_left_it(item)
        h.pipeline.renderer.render = real
        return h, item

    def test_a_scan_already_in_paperless_gets_its_copy_as_a_new_version(self):
        h, item = self.committed_without_a_copy()
        doc_id = item.state["doc_id"]
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["doc_id"], doc_id)                      # the same document
        doc = h.paperless.docs[doc_id]
        self.assertEqual(len(doc["versions"]), 2)
        self.assertEqual(item.state["rotations"], [180] * 3)
        self.assertEqual(len(doc["notes"]), 2)
        self.assertIn("Only the original was stored", doc["notes"][0])
        self.assertIn("The searchable copy was added after the document was first stored", doc["notes"][1])
        # The warning Jane got about this scan is closed.
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])

    def test_a_scan_still_on_its_way_in_goes_back_one_step(self):
        h = Harness(self)
        real = self.broken(h)
        h.drop(make_scan(letter(2), encodings=["ccitt", "jpeg"]))
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline.prepare(item)                                            # stopped here, as the update did
        self.assertEqual((item.status, item.state["mode"]), ("ready", "original_only"))
        self.as_an_earlier_version_left_it(item)
        h.pipeline.renderer.render = real
        asked = h.google.calls
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (2, 1))
        self.assertNotIn("added after", doc["notes"][0])
        self.assertEqual(alert_kinds(h), [])
        self.assertEqual(h.google.calls, asked)                             # Google's saved answer was reused

    def test_each_version_tries_once_and_doesnt_repeat_itself(self):
        h, item = self.committed_without_a_copy()
        self.broken(h)                                                      # this version can't build it either
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (1, 1))
        self.assertEqual(alert_kinds(h), ["warning"])      # said once, not again
        self.assertEqual(h.pipeline.retry_copies(), [])
        h.build()                                                           # a restart of the same version
        self.assertEqual(h.pipeline.retry_copies(), [])

    def test_a_scan_google_refused_and_already_in_paperless_is_not_asked_again(self):
        h = Harness(self)
        scan = make_scan(letter(2))
        h.drop(scan)
        h.service.claim_captures()
        h.clock.advance(31)
        h.service.claim_captures()
        item = h.only_item()
        h.pipeline._ready_without_google(item, "Google couldn't read this scan (PAGE_LIMIT_EXCEEDED, HTTP 400)")
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        item.state.pop("prepared_by", None)
        item.save()
        self.assertEqual(h.pipeline.retry_copies(), [])

    def test_nothing_is_tried_once_the_local_scan_is_gone(self):
        h, item = self.committed_without_a_copy()
        item.source_path.unlink()
        self.assertEqual(h.pipeline.retry_copies(), [])
        self.assertEqual(h.only_item().status, COMMITTED_WARNING)

    def test_a_scan_jane_kept_with_a_page_missing_isnt_held_again(self):
        h = Harness(self)
        real = self.broken(h)
        h.drop(numbered_letter(3, missing=(2,)))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, HELD)
        h.pipeline.keep_as_it_is(item)
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        self.as_an_earlier_version_left_it(item)
        h.pipeline.renderer.render = real
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertIn(item.status, {COMMITTED, COMMITTED_WARNING})
        self.assertNotEqual(item.status, HELD)
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual(len(doc["versions"]), 2)
        self.assertIn("Kept as it is, by Jane's choice", doc["notes"][-1])

    # A document already in Paperless is safe. Trying to add its copy must never make it look otherwise.

    def assertLeftAsItWas(self, h, item, doc_id):
        item = h.only_item()
        self.assertEqual((item.status, item.state["doc_id"], item.state["mode"]), (COMMITTED_WARNING, doc_id, "original_only"))
        self.assertIn("no Google text or upright copy", item.state["reason"])
        self.assertEqual(alert_kinds(h), ["warning"])                       # the one from the first time, nothing new
        doc = h.paperless.docs[doc_id]
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (1, 1))
        return item

    def test_google_being_down_leaves_the_document_alone_and_tries_at_the_next_start(self):
        h, item = self.committed_without_a_copy()
        doc_id = item.state["doc_id"]
        h.google.fail = [503] * 200
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = self.assertLeftAsItWas(h, item, doc_id)
        h.clock.advance(26 * 3600)                                          # past the give-up time: still not held
        h.settle()
        self.assertLeftAsItWas(h, item, doc_id)
        h.google.fail = []
        h.build()                                                           # the next start of the same version
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(len(h.paperless.docs[doc_id]["versions"]), 2)
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])

    def test_a_damaged_local_scan_leaves_the_document_alone(self):
        h, item = self.committed_without_a_copy()
        doc_id = item.state["doc_id"]
        item.source_path.write_bytes(b"not a PDF any more")
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        self.assertLeftAsItWas(h, item, doc_id)
        self.assertEqual(h.pipeline.retry_copies(), [])                     # and this version doesn't try again

    def test_paperless_being_down_for_a_day_never_holds_a_document_it_already_has(self):
        h, item = self.committed_without_a_copy()
        doc_id = item.state["doc_id"]
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.paperless.unreachable = True
        for _ in range(60):                                                 # thirty hours of half-hourly tries
            h.settle()
            h.clock.advance(30 * 60)
        h.paperless.unreachable = False
        item = h.only_item()
        self.assertNotIn(item.status, {HELD, WAITING})
        self.assertEqual((item.status, item.state["doc_id"]), (COMMITTED_WARNING, doc_id))
        self.assertNotIn("held", alert_kinds(h))

    def test_an_unexpected_error_while_adding_the_copy_says_nothing_and_changes_nothing(self):
        h, item = self.committed_without_a_copy()
        doc_id = item.state["doc_id"]

        def boom(*args, **kwargs):
            raise RuntimeError("something nobody planned for")

        h.pipeline._plan = boom
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        self.assertLeftAsItWas(h, item, doc_id)

    def test_a_scan_whose_first_note_was_already_written_gets_a_second_note(self):
        # The app died after writing "only the original was stored" and before it recorded the commit.
        from fake_paperless import Crash
        h = Harness(self)
        real = self.broken(h)
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
        self.as_an_earlier_version_left_it(item)
        h.pipeline.renderer.render = real
        h.build()
        h.pipeline.renderer.render = real
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        doc = h.paperless.docs[item.state["doc_id"]]
        self.assertEqual((len(h.paperless.docs), len(doc["versions"]), len(doc["notes"])), (1, 2, 2))
        self.assertIn("Only the original was stored", doc["notes"][0])
        self.assertIn("The searchable copy was added after the document was first stored", doc["notes"][1])

    def test_what_jane_changed_on_the_document_since_is_left_alone(self):
        h, item = self.committed_without_a_copy()
        doc = h.paperless.docs[item.state["doc_id"]]
        doc["owner"] = 3                                                    # she made it her own
        doc["tags"] = [tag for tag in doc["tags"] if tag != h.paperless.tags["Mail intake"]]
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(doc["owner"], 3)
        self.assertEqual(len(doc["versions"]), 2)
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])

    def test_a_stored_original_that_no_longer_matches_is_not_reported_as_a_failed_scan(self):
        # Whatever happened to the document since it went in, it didn't happen in this try.
        h, item = self.committed_without_a_copy()
        h.paperless.docs[item.state["doc_id"]]["versions"][-1]["checksum"] = "f" * 64
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED_WARNING)
        self.assertEqual(alert_kinds(h), ["warning"])

    def test_an_unfinished_try_on_a_stored_document_is_still_that_after_another_update(self):
        # The app was updated again while it was part way through adding a copy to a document
        # that has been in Paperless for a while. Jane's owner and tags on it are still hers.
        from fake_paperless import Crash
        h, item = self.committed_without_a_copy()
        doc = h.paperless.docs[item.state["doc_id"]]
        h.paperless.crash_after.add("update_version")
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        item = h.only_item()
        h.pipeline.prepare(item)
        with self.assertRaises(Crash):                                      # the app dies part way through
            h.pipeline.commit(item)
        self.assertEqual(item.status, "committing")
        doc["owner"] = 42
        doc["tags"] = [tag for tag in doc["tags"] if tag != h.paperless.tags["Mail intake"]]
        item.state["prepared_by"] = item.state["copy_retry_by"] = "an-earlier-version"
        item.save()
        h.build()
        self.assertEqual(h.pipeline.retry_copies(), [item.sha])
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(doc["owner"], 42)
        self.assertEqual(len(doc["notes"]), 2)
        self.assertIn("The searchable copy was added after the document was first stored", doc["notes"][1])
        self.assertEqual(alert_kinds(h), ["warning", "resolved"])

    def test_a_document_jane_deleted_is_tried_three_times_at_most_and_never_said_anything_about(self):
        h, item = self.committed_without_a_copy()
        del h.paperless.docs[item.state["doc_id"]]
        for start in range(5):
            h.build()
            h.pipeline.retry_copies()
            h.settle()
            item = h.only_item()
            self.assertEqual(item.status, COMMITTED_WARNING)
        self.assertEqual(alert_kinds(h), ["warning"])
        self.assertEqual(sum(1 for event in item.events() if event["event"] == "copy_retry"), 3)
        self.assertEqual(h.paperless.docs, {})

    def test_what_the_first_try_recorded_is_kept(self):
        h, item = self.committed_without_a_copy()
        first = (item.root / "checks.json").read_bytes()
        self.assertIn(b"render_error", first)
        h.pipeline.retry_copies()
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, COMMITTED)
        self.assertEqual((item.root / "checks.by-earlier-version.json").read_bytes(), first)
        self.assertNotIn(b"render_error", (item.root / "checks.json").read_bytes())


class ImageCheckTests(unittest.TestCase):
    """The hard check: a copied page carries exactly the images the scanner wrote."""

    def images(self, encodings, **stack):
        from paperless_intake.docai import pdf_renderer
        scan = make_scan(letter(len(encodings)), encodings=encodings, **stack)
        return [pdf_renderer.page_images(page) for page in PdfReader(io.BytesIO(scan.pdf)).pages]

    def test_the_same_bytes_described_differently_are_not_the_same_image(self):
        from paperless_intake.docai import pdf_renderer
        (page,) = self.images(["ccitt"])
        self.assertTrue(pdf_renderer._same_images(page, [dict(image) for image in page]))
        flipped = [dict(image, described=image["described"].replace("('/BlackIs1', 'True')", "('/BlackIs1', 'False')"))
                   for image in page]
        self.assertNotEqual(flipped[0]["described"], page[0]["described"])
        self.assertFalse(pdf_renderer._same_images(page, flipped))
        self.assertFalse(pdf_renderer._same_images(page, [dict(page[0], sha256="0" * 64)]))
        self.assertFalse(pdf_renderer._same_images(page, []))

    def test_an_image_written_inline_is_counted(self):
        from paperless_intake.docai import pdf_renderer
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(612, 792)
        stream = DecodedStreamObject()
        stream.set_data(b"q 612 0 0 792 0 0 cm BI /W 2 /H 2 /BPC 8 /CS /G ID \x00\xff\xff\x00 EI Q")
        page[NameObject("/Contents")] = writer._add_object(stream)
        buffer = io.BytesIO()
        writer.write(buffer)
        (found,) = pdf_renderer.page_images(PdfReader(io.BytesIO(buffer.getvalue())).pages[0])
        self.assertEqual((found["format"], found["width"], found["height"], found["byteCount"]), ("inline", 2, 2, 4))

    def test_a_copy_that_lost_a_drawing_instruction_is_refused(self):
        from paperless_intake.docai import pdf_renderer
        self.assertEqual(pdf_renderer._draws(PdfReader(io.BytesIO(
            make_scan(letter(1), encodings=["layered"]).pdf)).pages[0]), 2)

    def test_a_jpeg_whose_format_is_written_as_a_list_is_still_a_jpeg(self):
        from paperless_intake.docai import pdf_renderer
        scan = make_scan(letter(1))
        pdf = scan.pdf.replace(b"/Filter /DCTDecode", b"/Filter [/DCTDecode]")
        page = PdfReader(io.BytesIO(pdf)).pages[0]
        image, kind = pdf_renderer.comparison_image(page)
        self.assertEqual((kind, image.size), ("DCTDecode", (1275, 1650)))
