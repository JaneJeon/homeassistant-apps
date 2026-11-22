"""Checks added on top of the October validation and renderer gates.

- missing_pages: printed page numbers that reveal a page the scan doesn't have.
  This is the one check that holds a scan: a lost page can only be recovered
  from the paper.
- coverage: visible ink on a page that no OCR line accounts for. Measured and
  recorded only; it is not a gate yet (see the design doc for why).
- short_text: whether Paperless would throw away Google's text and re-OCR.
- text_matches: the read-back comparison of Paperless's search text with Google's.
"""

from __future__ import annotations

import base64
import io
import unicodedata
from collections import Counter

from .docai import glyphless

# Paperless 3.3.4 treats normalized text of 50 characters or fewer as "no text"
# (PDF_TEXT_MIN_LENGTH in paperless/parsers/utils.py) and re-OCRs it. We stay
# well clear of that line because Paperless measures its own extraction, not ours.
SHORT_TEXT_LIMIT = 100


def _series(plan) -> list[dict]:
    return [entry for entry in plan.evidence if isinstance(entry, dict) and entry.get("kind") == "series"]


def missing_pages(plan, page_count: int) -> str | None:
    """Return a plain description of pages the printed numbers say are missing, or None.

    With a printed total ("2 of 3"), every number from 1 to the total should be
    present. Without a total, only gaps between the lowest and highest printed
    numbers count, because an excerpt may legitimately start at page 5.

    One allowance, and only one. Many letters print no number on their first
    page and start at "Page 2 of 3". So when the only number missing is 1 and
    the scan has a page with no page number of any kind, that page is taken to
    be the first page. (A real two-page letter already in Paperless reads
    that way: the only page number on it is "Page 2 of 2".)

    Any other missing number holds the scan, even if an unnumbered page could
    in principle be carrying it. A lost page can only be recovered from the
    paper, so a doubt here is settled by Jane looking, not by the app assuming.
    """
    numbered = {entry.get("sourcePage") for entry in plan.evidence
                if isinstance(entry, dict) and entry.get("kind") == "marker"}
    unnumbered_pages = page_count - len(numbered)
    # One document can print its page numbers at the top of some pages and the bottom of
    # others (a statement with inserts does). The planner keeps those apart, by band. For
    # this check they are one series when they count to the same total and no number is
    # printed in both places. A number printed in both places means two documents, each
    # of which has to be complete by itself.
    series_found: list[tuple[int | None, set[int]]] = []
    for series in _series(plan):
        numbers = {int(number) for _, number in series.get("sourcePageNumberPairs") or []}
        if not numbers:
            continue
        total = int(series["total"]) if series.get("total") is not None else None
        same = next((found for other, found in series_found
                     if total is not None and other == total and not (found & numbers)), None)
        if same is not None:
            same.update(numbers)
        else:
            series_found.append((total, numbers))
    gaps = []
    for total, found in series_found:
        numbers = sorted(found)
        if total is not None:
            missing = sorted(set(range(1, total + 1)) - found)
        else:
            missing = sorted(set(range(numbers[0], numbers[-1] + 1)) - found)
        if not missing or (missing == [1] and unnumbered_pages > 0):
            continue
        if total is not None:
            gaps.append(f"{'page' if len(missing) == 1 else 'pages'} {_list(missing)} of {total} "
                        f"{'is' if len(missing) == 1 else 'are'} missing "
                        f"(the printed page numbers count to {total}; the scan has {page_count} "
                        f"page{'' if page_count == 1 else 's'})")
        else:
            gaps.append(f"the printed page numbers skip {'page' if len(missing) == 1 else 'pages'} {_list(missing)}")
    if not gaps:
        return None
    return "; ".join(dict.fromkeys(gaps))


def _list(numbers: list[int]) -> str:
    if len(numbers) == 1:
        return str(numbers[0])
    return ", ".join(str(n) for n in numbers[:-1]) + " and " + str(numbers[-1])


def coverage(document: dict) -> list[dict]:
    """Per page: share of dark pixels that sit outside every OCR line box.

    Uses the page image Google returned and Google's own line boxes, so both
    are in the same frame. Never raises: a failure is recorded on the page.
    """
    from PIL import Image
    import numpy as np

    results = []
    for page in document.get("pages", []):
        number = page.get("pageNumber")
        try:
            image_info = page.get("image") or {}
            raw = base64.b64decode(image_info.get("content", ""))
            image = Image.open(io.BytesIO(raw)).convert("L")
            width = 300
            height = max(1, round(image.height * width / image.width))
            pixels = np.asarray(image.resize((width, height)))
            ink = pixels < 110
            margin_x, margin_y = int(width * 0.02), int(height * 0.02)
            ink[:margin_y, :] = False
            ink[-margin_y:, :] = False
            ink[:, :margin_x] = False
            ink[:, -margin_x:] = False
            covered = np.zeros_like(ink)
            pad_x, pad_y = int(width * 0.015) + 1, int(height * 0.015) + 1
            for line in page.get("lines", []):
                vertices = (line.get("layout", {}).get("boundingPoly", {}) or {}).get("normalizedVertices") or []
                if len(vertices) < 3:
                    continue
                xs = [float(v.get("x", 0)) for v in vertices]
                ys = [float(v.get("y", 0)) for v in vertices]
                x0 = max(0, int(min(xs) * width) - pad_x)
                x1 = min(width, int(max(xs) * width) + pad_x + 1)
                y0 = max(0, int(min(ys) * height) - pad_y)
                y1 = min(height, int(max(ys) * height) + pad_y + 1)
                covered[y0:y1, x0:x1] = True
            total = int(ink.sum())
            uncovered = int((ink & ~covered).sum())
            results.append({
                "page": number,
                "ink_pixels": total,
                "uncovered_ink_pixels": uncovered,
                "uncovered_share": round(uncovered / total, 4) if total else 0.0,
            })
        except Exception as error:  # a measurement, never a reason to stop
            results.append({"page": number, "error": type(error).__name__})
    return results


def normalized_words(text: str) -> list[str]:
    return unicodedata.normalize("NFKC", text or "").split()


def search_words(text: str) -> list[str]:
    return glyphless.search_words(text)


def short_text(text: str) -> bool:
    return len(" ".join(normalized_words(text))) <= SHORT_TEXT_LIMIT


def text_matches(google_text: str, paperless_text: str, *, tolerance: float = 0.02) -> dict:
    """Compare as bags of searchable words: order differs because Paperless extracts by layout,
    and punctuation may attach differently, but a search sees neither."""
    expected = Counter(search_words(google_text))
    actual = Counter(search_words(paperless_text))
    total = sum(expected.values())
    missing = sum((expected - actual).values())
    extra = sum((actual - expected).values())
    ok = total > 0 and missing <= tolerance * total and extra <= tolerance * total
    return {"ok": ok, "google_words": total, "missing_words": missing, "extra_words": extra}
