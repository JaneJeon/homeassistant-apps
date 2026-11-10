from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import unittest

from paperless_intake.docai.page_order import plan_page_order


def line(text, **values):
    return {"text": text, "x": 0.80, "y": 0.90, "w": 0.16, "h": 0.012,
            "confidence": 0.99, "numeric_confidence": 0.98, **values}


def box(x, y, w, h):
    return {"normalizedVertices": [{"x": x, "y": y}, {"x": x+w, "y": y},
                                   {"x": x+w, "y": y+h}, {"x": x, "y": y+h}]}


def document(*page_lines):
    text, pages = "", []
    for page_number, specs in enumerate(page_lines, 1):
        page_start = len(text)
        lines, tokens = [], []
        for spec in specs:
            start = len(text)
            content = spec["text"]
            text += content + "\n"
            layout = {"textAnchor": {"textSegments": [{"startIndex": start, "endIndex": len(text)}]},
                      "boundingPoly": box(spec["x"], spec["y"], spec["w"], spec["h"]),
                      "confidence": spec["confidence"], "orientation": spec.get("orientation", "PAGE_UP")}
            lines.append({"layout": layout})
            for token in re.finditer(r"\S+", content):
                confidence = spec["numeric_confidence"] if token.group().isdecimal() else spec["confidence"]
                token_layout = {"textAnchor": {"textSegments": [{"startIndex": start+token.start(), "endIndex": start+token.end()}]},
                                "boundingPoly": box(spec["x"]+spec["w"]*token.start()/max(1, len(content)), spec["y"],
                                                    spec["w"]*(token.end()-token.start())/max(1, len(content)), spec["h"])}
                if confidence is not None:
                    token_layout["confidence"] = confidence
                tokens.append({"layout": token_layout})
        pages.append({"pageNumber": page_number, "image": {"width": 1000, "height": 1400},
                      "layout": {"orientation": "PAGE_UP", "textAnchor": {"textSegments": [{"startIndex": page_start, "endIndex": len(text)}]}},
                      "lines": lines, "tokens": tokens})
    return {"text": text, "pages": pages}


class PageOrderTests(unittest.TestCase):
    def test_reversed_complete_series(self):
        data = document(*[[line(f"Page {n} of 3")] for n in [3, 2, 1]])
        untouched = copy.deepcopy(data)
        plan = plan_page_order(data)
        self.assertEqual(plan.mapping, (3, 2, 1))
        self.assertEqual(plan.status, "reorder_from_complete_numbering")
        self.assertEqual(data, untouched)
        marker = next(e for e in plan.evidence if e["kind"] == "marker")
        self.assertEqual(marker["text"], "Page 3 of 3")
        self.assertEqual(marker["numberTokenConfidenceMinimum"], 0.98)
        self.assertEqual({token["role"] for token in marker["numericTokens"]}, {"number", "total"})
        self.assertTrue(all(token["normalizedBox"] for token in marker["numericTokens"]))
        self.assertEqual(marker["lineBoundingPoly"], data["pages"][0]["lines"][0]["layout"]["boundingPoly"])
        marker["lineBoundingPoly"]["normalizedVertices"][0]["x"] = -999
        self.assertEqual(data, untouched)  # Evidence has no mutable aliases into raw responses.

    def test_complete_no_total_in_order(self):
        plan = plan_page_order(document([line("Pg. 1")], [line("Page 2")]))
        self.assertEqual((plan.mapping, plan.status), ((1, 2), "complete_numbering_in_order"))

    def test_duplicate_numbers_require_review(self):
        plan = plan_page_order(document([line("Page 1 of 3")], [line("Page 1 of 3")], [line("Page 3 of 3")]))
        self.assertEqual((plan.mapping, plan.status), ((1, 2, 3), "needs_review"))

    def test_different_numbers_on_same_page_are_ambiguous(self):
        plan = plan_page_order(document([line("Page 1 of 2", x=0.1), line("Page 2 of 2", x=0.7)], [line("Page 2 of 2")]))
        self.assertEqual(plan.status, "needs_review")

    def test_identical_repeated_marker_is_one_unambiguous_number(self):
        plan = plan_page_order(document([line("Page 1 of 2", x=0.1), line("Page 1 of 2", x=0.7)], [line("Page 2 of 2")]))
        self.assertEqual(plan.status, "complete_numbering_in_order")

    def test_gap_preserves_unnumbered_page_without_promoting_order(self):
        plan = plan_page_order(document([line("Page 1 of 3")], [line("Unnumbered attachment")], [line("Page 3 of 3")]))
        self.assertEqual((plan.mapping, plan.status), ((1, 2, 3), "partial_in_order"))

    def test_partial_out_of_order_does_not_guess_attachment_placement(self):
        plan = plan_page_order(document([line("Page 3 of 4")], [line("Unnumbered attachment")], [line("Page 1 of 4")]))
        self.assertEqual((plan.mapping, plan.status), ((1, 2, 3), "needs_review"))

    def test_wrong_explicit_denominator_blocks_full_document_reordering(self):
        plan = plan_page_order(document([line("Page 2 of 3")], [line("Page 1 of 3")]))
        self.assertEqual((plan.mapping, plan.status), ((1, 2), "needs_review"))

    def test_low_or_missing_numeric_confidence_requires_review(self):
        for confidence in [0.899, None]:
            with self.subTest(confidence=confidence):
                plan = plan_page_order(document([line("Page 2 of 2", numeric_confidence=confidence)], [line("Page 1 of 2")]))
                self.assertEqual(plan.status, "needs_review")
                self.assertEqual(plan.mapping, (1, 2))
        with self.assertRaises(ValueError):
            plan_page_order(document([line("Page 1")]), min_number_token_confidence=0.89)

    def test_explicit_denominator_token_confidence_is_also_gated(self):
        data = document([line("Page 2 of 2")], [line("Page 1 of 2")])
        data["pages"][0]["tokens"][-1]["layout"]["confidence"] = 0.4
        self.assertEqual(plan_page_order(data).status, "needs_review")

    def test_numeric_token_without_a_box_cannot_support_automatic_order(self):
        data = document([line("Page 2 of 2")], [line("Page 1 of 2")])
        del data["pages"][0]["tokens"][1]["layout"]["boundingPoly"]
        self.assertEqual(plan_page_order(data).status, "needs_review")

    def test_page_frame_must_establish_upright_coordinates(self):
        for orientation in [None, "PAGE_RIGHT"]:
            with self.subTest(orientation=orientation):
                data = document([line("Page 2 of 2")], [line("Page 1 of 2")])
                data["pages"][0]["layout"]["orientation"] = orientation
                self.assertEqual((plan_page_order(data).mapping, plan_page_order(data).status), ((1, 2), "needs_review"))

    def test_bare_numbers_dates_fractions_and_unlabelled_of_are_ignored(self):
        plan = plan_page_order(document([line("2026-10-05"), line("1/2"), line("06/29"), line("2 of 3")], [line("17")]))
        self.assertEqual((plan.mapping, plan.status), ((1, 2), "unknown_no_pagination_evidence"))

    def test_body_markers_and_sideways_markers_are_ignored(self):
        plan = plan_page_order(document([line("Page 2 of 2", y=0.45)], [line("Page 1 of 2", orientation="PAGE_RIGHT")]))
        self.assertEqual(plan.status, "unknown_no_pagination_evidence")

    def test_entire_box_must_be_inside_outer_band(self):
        plan = plan_page_order(document([line("Page 1", y=0.19, h=0.03)]))
        self.assertEqual(plan.status, "unknown_no_pagination_evidence")

    def test_nearby_above_and_left_page_labels(self):
        plan = plan_page_order(document(
            [line("Page", x=0.82, y=0.04, w=0.04), line("2 of 2", x=0.80, y=0.06, w=0.08)],
            [line("PAGE", x=0.60, y=0.83, w=0.04, h=0.01), line("1 OF 2", x=0.67, y=0.83, w=0.07, h=0.01)]))
        # Band grouping is intentional: these two markers do not form one series.
        self.assertEqual(plan.status, "partial_in_order")
        self.assertEqual(plan.mapping, (1, 2))
        self.assertEqual(len([e for e in plan.evidence if e["kind"] == "marker"]), 2)

    def test_standalone_of_needs_a_nearby_label(self):
        plan = plan_page_order(document([line("Page", y=0.02), line("1 of 2", y=0.18)], [line("2 of 2")]))
        self.assertEqual(plan.status, "unknown_no_pagination_evidence")

    def test_slash_after_explicit_marker_is_not_silently_dropped(self):
        plan = plan_page_order(document([line("Page 2/2")], [line("Page 1/2")]))
        self.assertEqual((plan.mapping, plan.status), ((1, 2), "needs_review"))

    def test_two_complete_series_must_agree(self):
        for bottom, expected in [([3, 2, 1], "reorder_from_complete_numbering"), ([1, 2, 3], "needs_review")]:
            with self.subTest(bottom=bottom):
                data = document(*[[line(f"Page {top} of 3", y=0.04), line(f"Page {low}")] for top, low in zip([3, 2, 1], bottom)])
                plan = plan_page_order(data)
                self.assertEqual(plan.status, expected)
                self.assertEqual(plan.mapping, (3, 2, 1) if expected == "reorder_from_complete_numbering" else (1, 2, 3))

    def test_complete_nested_series_checks_proposed_not_original_order(self):
        for inner, expected in [([2, 1], "reorder_from_complete_numbering"), ([1, 2], "needs_review")]:
            with self.subTest(inner=inner):
                data = document([line("Page 4 of 4", y=0.04), line(f"Page {inner[0]} of 2")],
                                [line("Page 3 of 4", y=0.04), line(f"Page {inner[1]} of 2")],
                                [line("Page 2 of 4", y=0.04)], [line("Page 1 of 4", y=0.04)])
                plan = plan_page_order(data)
                self.assertEqual(plan.status, expected)
                self.assertTrue(any(e.get("completeInnerDistinctDenominatorSeries") for e in plan.evidence))

    def test_partial_nested_conflict_requires_review(self):
        data = document([line("Page 1 of 5", y=0.04), line("Page 2 of 2")], [], [line("Page 3 of 5", y=0.04), line("Page 1 of 2")])
        self.assertEqual(plan_page_order(data).status, "needs_review")

    def test_invalid_source_page_numbering_never_produces_a_permutation(self):
        data = document([line("Page 2")], [line("Page 1")])
        data["pages"][1]["pageNumber"] = 1
        self.assertEqual((plan_page_order(data).mapping, plan_page_order(data).status), ((1, 2), "needs_review"))


@unittest.skipUnless(os.environ.get("PAPERLESS_DOCUMENT_AI_PRIVATE_RESULTS"), "Private Google golden fixtures are opt-in and stay outside the package")
class PrivatePageOrderGoldenTests(unittest.TestCase):
    def test_seven_saved_google_documents(self):
        base = Path(os.environ["PAPERLESS_DOCUMENT_AI_PRIVATE_RESULTS"])
        expected = {
            19: ((6, 5, 4, 3, 2, 1), "reorder_from_complete_numbering"),
            37: ((2, 1), "reorder_from_complete_numbering"),
            42: (tuple(range(1, 16)), "complete_numbering_in_order"),
            34: (tuple(range(1, 9)), "partial_in_order"),
            23: ((1, 2), "unknown_no_pagination_evidence"),
            29: ((1, 2), "unknown_no_pagination_evidence"),
            31: ((1, 2), "unknown_no_pagination_evidence"),
        }
        for doc_id, result in expected.items():
            with self.subTest(document=doc_id):
                path = base / f"{doc_id}-plain-v21" / "response.json"
                raw = path.read_bytes()
                data = json.loads(raw)["document"]
                plan = plan_page_order(data)
                self.assertEqual((plan.mapping, plan.status), result)
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(raw).digest())
                if doc_id == 34:
                    outer = next(e for e in plan.evidence if e["kind"] == "series" and e["total"] == 7)
                    self.assertEqual(outer["sourcePagesWithoutThisSeries"], [4])
                if doc_id == 19:
                    inner = next(e for e in plan.evidence if e["kind"] == "series" and e["total"] == 2)
                    self.assertTrue(inner["completeInnerDistinctDenominatorSeries"])
                    self.assertEqual(inner["sourcePageNumberPairs"], [[3, 2], [4, 1]])


if __name__ == "__main__":
    unittest.main()
