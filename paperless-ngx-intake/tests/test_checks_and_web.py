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
        self.assertIn("Keep original only", html)
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

    def test_only_the_ingress_proxy_may_connect(self):
        h = Harness(self)
        _, port = self.start(h, allowed=("172.30.32.2",))
        status, _, _ = self.request(port, "GET", "/")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
