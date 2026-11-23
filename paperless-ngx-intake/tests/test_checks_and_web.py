"""The added checks, and the Mail intake page."""

from __future__ import annotations

import http.client
import unittest

from paperless_intake import checks
from paperless_intake.docai.page_order import plan_page_order
from paperless_intake.store import HELD
from paperless_intake.web import Page, serve

from synth import PageSpec, google_response, make_scan, numbered_letter
from test_pipeline import Harness


def plan_for(scan):
    document = google_response(scan, list(range(1, len(scan.pages) + 1)))["document"]
    return document, plan_page_order(document)


class CheckTests(unittest.TestCase):
    def test_missing_pages_named(self):
        _, plan = plan_for(numbered_letter(4, missing=(2, 3)))
        self.assertEqual(checks.missing_pages(plan, 2),
                         "pages 2 and 3 of 4 are missing (the printed page numbers count to 4; the scan has 2 pages)")

    def test_a_first_page_without_a_number_is_not_a_missing_page(self):
        # Many letters start numbering at "Page 2 of 3", with no number on the first page.
        _, plan = plan_for(numbered_letter(3, unnumbered=(1,)))
        self.assertIsNone(checks.missing_pages(plan, 3))

    def test_any_other_number_missing_holds_even_with_an_unnumbered_page_in_the_scan(self):
        # The page may be there with its footer unread. Only Jane looking can tell, so it holds.
        _, plan = plan_for(numbered_letter(4, unnumbered=(3,)))
        self.assertEqual(checks.missing_pages(plan, 4),
                         "page 3 of 4 is missing (the printed page numbers count to 4; the scan has 4 pages)")

    def test_a_second_numbered_document_cant_stand_in_for_a_missing_page(self):
        # Document A lost its page 2 in a double feed. Document B's pages carry their own numbers.
        scan = make_scan([PageSpec(["Explanation of benefits, first page of three"], footer="Page 1 of 3"),
                          PageSpec(["Explanation of benefits, last page of three"], footer="Page 3 of 3"),
                          PageSpec(["A separate notice, first page of two"], footer="Page 1 of 2"),
                          PageSpec(["A separate notice, last page of two"], footer="Page 2 of 2")])
        _, plan = plan_for(scan)
        self.assertIn("page 2 of 3 is missing", checks.missing_pages(plan, 4))

    def test_a_cover_letter_cant_stand_in_for_a_missing_last_page(self):
        scan = make_scan([PageSpec(["A cover letter with no page number on it at all"]),
                          PageSpec(["The first page of a two page form"], footer="Page 1 of 2")])
        _, plan = plan_for(scan)
        self.assertIn("page 2 of 2 is missing", checks.missing_pages(plan, 2))

    def test_an_unnumbered_insert_doesnt_hide_pages_that_are_really_gone(self):
        scan = make_scan([PageSpec(["Body text of the first page of this letter"], footer="Page 1 of 4"),
                          PageSpec(["An insert with no page number printed on it"]),
                          PageSpec(["Body text of the last page of this letter"], footer="Page 4 of 4")])
        _, plan = plan_for(scan)
        self.assertEqual(checks.missing_pages(plan, 3),
                         "pages 2 and 3 of 4 are missing (the printed page numbers count to 4; the scan has 3 pages)")

    def test_complete_numbering_has_no_gap(self):
        _, plan = plan_for(numbered_letter(3))
        self.assertIsNone(checks.missing_pages(plan, 3))

    def test_no_page_numbers_no_gap(self):
        _, plan = plan_for(make_scan([PageSpec(["Just a letter with no page numbers at all here"])]))
        self.assertIsNone(checks.missing_pages(plan, 1))

    def test_coverage_finds_ink_without_text(self):
        document, _ = plan_for(make_scan([PageSpec(["A line of text above a logo block"], mark=True)]))
        page = checks.coverage(document)[0]
        self.assertGreater(page["uncovered_share"], 0.5)

    def test_short_text(self):
        self.assertTrue(checks.short_text("Hi\n"))
        self.assertFalse(checks.short_text("word " * 30))

    def test_text_matches_ignores_order_and_spacing(self):
        result = checks.text_matches("one two\nthree four", "three   four one two")
        self.assertTrue(result["ok"])
        self.assertFalse(checks.text_matches("one two three four", "TESSERACT TEXT")["ok"])

    def test_text_matches_sees_words_not_punctuation_spacing(self):
        self.assertTrue(checks.text_matches("Total: $1,234.56 (due)", "Total : $ 1,234.56 ( due )")["ok"])
        self.assertFalse(checks.text_matches("statement of account", "statem ent of account")["ok"])
        self.assertTrue(checks.text_matches("주의: 한국어", "주 의 : 한 국 어")["ok"])


class WebTests(unittest.TestCase):
    def start(self, h, allowed=("127.0.0.1",)):
        page = Page(h.service, h.pipeline, h.settings)
        server = serve(page, 0, allowed)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return page, server.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def test_held_scan_listed_viewable_and_discardable(self):
        h = Harness(self)
        h.drop(numbered_letter(3, missing=(2,)))
        h.settle()
        item = h.only_item()
        self.assertEqual(item.status, HELD)
        page, port = self.start(h)
        status, _, body = self.request(port, "GET", "/")
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn("page 2 of 3 is missing", html)
        self.assertIn("Keep as it is", html)
        status, headers, pdf = self.request(port, "GET", f"/scan/{item.sha}")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(pdf.startswith(b"%PDF"))
        status, _, _ = self.request(port, "POST", f"/action/{item.sha}/discard", body="token=wrong",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 400)
        status, headers, _ = self.request(port, "POST", f"/action/{item.sha}/discard", body=f"token={page.token}",
                                          headers={"Content-Type": "application/x-www-form-urlencoded",
                                                   "X-Ingress-Path": "/api/hassio_ingress/abc"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/api/hassio_ingress/abc/")
        self.assertEqual(h.store.all(), [])

    def test_the_page_says_what_was_done_about_documents_that_came_another_way(self):
        from synth import PageSpec, make_scan
        h = Harness(self)
        page = Page(h.service, h.pipeline, h.settings)
        self.assertIn("None looked at yet", page.render())
        body = ["A letter uploaded on the web page, with enough words to read",
                "and a second line so that the page counts as one with plenty of text"]
        h.paperless.add_foreign(make_scan([PageSpec(body)]).pdf)
        h.paperless.add_foreign(b"\x89PNG\r\n\x1a\n" + b"0" * 64, name="<b>photo</b>.png")
        h.settle()
        html = page.render()
        self.assertIn("Other documents in Paperless", html)
        self.assertIn("2 looked at. 1 given a searchable version.", html)
        self.assertIn("1 of a kind this release can't read.", html)
        self.assertIn("Document 2: it isn&#x27;t a PDF, and this release reads only PDFs", html)
        self.assertNotIn("<b>photo", html)

    def test_a_scan_whose_copy_a_later_release_mended_says_so(self):
        from paperless_intake.docai import glyphless
        from synth import make_scan
        from test_stack import letter
        import unittest.mock
        h = Harness(self)
        real_fit = glyphless.fit
        h.drop(make_scan(letter(3), turn=180, flipped=True, touching=True))
        with unittest.mock.patch.object(glyphless, "fit", lambda points, cells: real_fit(points, max(1, cells - 1))):
            h.settle()
        page = Page(h.service, h.pipeline, h.settings)
        self.assertIn("committed with a warning", page.render())
        self.assertNotIn("has since given it", page.render())
        record = h.documents.load(h.only_item().state["doc_id"])
        record.state["by"] = "an-earlier-release"
        record.save()
        item = h.only_item()
        item.state["prepared_by"] = "an-earlier-release"
        item.save()
        h.settle()
        self.assertIn("has since given it a searchable version that reads back right", page.render())

    def test_only_the_ingress_proxy_may_connect(self):
        h = Harness(self)
        _, port = self.start(h, allowed=("172.30.32.2",))
        status, _, _ = self.request(port, "GET", "/")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
