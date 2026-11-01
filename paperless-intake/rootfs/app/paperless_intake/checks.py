"""Checks added on top of the October validation and renderer gates.

- missing_pages: printed page numbers that reveal a page the scan doesn't have.
  This one holds the scan.
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

# Paperless 3.3.4 treats normalized text of 50 characters or fewer as "no text"
# (PDF_TEXT_MIN_LENGTH in paperless/parsers/utils.py) and re-OCRs it. We stay
# well clear of that line because Paperless measures its own extraction, not ours.
SHORT_TEXT_LIMIT = 100


def _series(plan) -> list[dict]:
    return [entry for entry in plan.evidence if isinstance(entry, dict) and entry.get("kind") == "series"]


def missing_pages(plan, page_count: int) -> str | None:
    """Return a plain description of the gap, or None when nothing is missing.

    With a printed total ("2 of 3"), every number from 1 to the total must be
    present. Without a total, only gaps between the lowest and highest printed
    numbers count, because an excerpt may legitimately start at page 5.
    """
    gaps = []
    for series in _series(plan):
        pairs = series.get("sourcePageNumberPairs") or []
        numbers = sorted({int(number) for _, number in pairs})
        if not numbers:
            continue
        total = series.get("total")
        if total is not None:
            expected = set(range(1, int(total) + 1))
            missing = sorted(expected - set(numbers))
            if missing:
                gaps.append(f"{'page' if len(missing) == 1 else 'pages'} {_list(missing)} of {total} "
                            f"{'is' if len(missing) == 1 else 'are'} missing "
                            f"(the printed page numbers count to {total}; the scan has {page_count} "
                            f"page{'' if page_count == 1 else 's'})")
            elif int(total) > page_count:
                gaps.append(f"the printed page numbers say {total} pages, but the scan has {page_count}")
        else:
            missing = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
            if missing:
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


def short_text(text: str) -> bool:
    return len(" ".join(normalized_words(text))) <= SHORT_TEXT_LIMIT


def text_matches(google_text: str, paperless_text: str, *, tolerance: float = 0.02) -> dict:
    """Compare as bags of words: order differs because Paperless extracts by layout."""
    expected = Counter(normalized_words(google_text))
    actual = Counter(normalized_words(paperless_text))
    total = sum(expected.values())
    missing = sum((expected - actual).values())
    extra = sum((actual - expected).values())
    ok = total > 0 and missing <= tolerance * total and extra <= tolerance * total
    return {"ok": ok, "google_words": total, "missing_words": missing, "extra_words": extra}
