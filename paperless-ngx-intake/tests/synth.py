"""Synthetic scans and a fake Google Document AI that answers like the real one.

A synthetic scan is a PDF with one full-page grayscale JPEG per page, the shape
the Brother scanner produces. The fake Google returns, for the requested pages,
the page image rasterised the way Document AI does (about 4 megapixels), plus
lines and tokens with text anchors and normalized boxes. That is enough for
the real backend, page planner and renderer gates to run unchanged.

Everything here is synthetic; no real mail is used.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from paperless_intake.docai.google_backend import HTTPResponse

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
PAGE_POINTS = (612.0, 792.0)      # US letter
PIXELS = (1275, 1650)             # 150 dpi

_REGISTRY: dict[str, "Scan"] = {}


@dataclass
class PageSpec:
    lines: list[str]
    footer: str | None = None      # e.g. "Page 2 of 3", drawn in the bottom band
    blank: bool = False
    mark: bool = False             # a dark block (like a logo or signature) that has no text


@dataclass
class Scan:
    pdf: bytes
    pages: list[PageSpec]
    images: list[bytes] = field(default_factory=list)       # source JPEGs
    boxes: list[list[tuple[str, tuple[int, int, int, int], list]]] = field(default_factory=list)
    size: tuple[int, int] = PIXELS

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pdf).hexdigest()


def _font(size: int):
    return ImageFont.truetype(str(FONT), size)


def _draw_page(spec: PageSpec, scale: float = 1.0):
    size = (round(PIXELS[0] * scale), round(PIXELS[1] * scale))
    s = lambda value: round(value * scale)
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    font = _font(s(30))
    placed = []
    y = 150
    texts = [] if spec.blank else list(spec.lines)
    for text in texts:
        placed.append(_draw_line(draw, font, text, s(120), s(y)))
        y += 60
    if spec.footer and not spec.blank:
        placed.append(_draw_line(draw, font, spec.footer, s(520), s(1500)))
    if spec.mark:
        draw.rectangle((s(800), s(900), s(1100), s(1100)), fill=20)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue(), placed


def _draw_line(draw, font, text, x, y):
    draw.text((x, y), text, fill=0, font=font)
    box = draw.textbbox((x, y), text, font=font)
    words = []
    cursor = x
    for word in text.split(" "):
        start = cursor
        bbox = draw.textbbox((start, y), word, font=font)
        words.append((word, bbox))
        cursor = start + draw.textlength(word + " ", font=font)
    return text, box, words


def _pdf(images: list[bytes]) -> bytes:
    """Hand-written PDF: each page is exactly `q W 0 0 H 0 0 cm /Im0 Do Q`."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholder 1
    pages_obj = add(b"")  # placeholder 2
    kids = []
    for data in images:
        width, height = Image.open(io.BytesIO(data)).size
        image_obj = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                        b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (width, height, len(data))
                        + data + b"\nendstream")
        content = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
        content_obj = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        page_obj = add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im0 %d 0 R >> >> "
                       b"/Contents %d 0 R >>" % (image_obj, content_obj))
        kids.append(page_obj)
    objects[catalog - 1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[pages_obj - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                              + b"] /Count %d >>" % len(kids))
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def make_scan(pages: list[PageSpec], *, scale: float = 1.0) -> Scan:
    images, boxes = [], []
    for spec in pages:
        data, placed = _draw_page(spec, scale)
        images.append(data)
        boxes.append(placed)
    size = (round(PIXELS[0] * scale), round(PIXELS[1] * scale))
    scan = Scan(_pdf(images), pages, images, boxes, size)
    _REGISTRY[scan.sha256] = scan
    return scan


def numbered_letter(count: int, *, missing: tuple[int, ...] = (), scale: float = 1.0, tag: str = "") -> Scan:
    """A letter with "Page n of count" footers; pages listed in `missing` are left out."""
    specs = []
    for number in range(1, count + 1):
        if number in missing:
            continue
        body = [f"Synthetic account statement line {number}.{i} for testing purposes only" for i in range(1, 6)]
        if tag:
            body.append(f"Reference {tag}")
        specs.append(PageSpec(body, footer=f"Page {number} of {count}"))
    return make_scan(specs, scale=scale)


def _norm_box(box, size=PIXELS):
    x0, y0, x1, y1 = box
    w, h = size
    return {"normalizedVertices": [{"x": x0 / w, "y": y0 / h}, {"x": x1 / w, "y": y0 / h},
                                   {"x": x1 / w, "y": y1 / h}, {"x": x0 / w, "y": y1 / h}]}


def _anchor(start, end):
    return {"textSegments": [{"startIndex": str(start), "endIndex": str(end)}]}


def google_response(scan: Scan, page_numbers: list[int]) -> dict:
    text = ""
    pages = []
    scale = math.sqrt(4_000_000 / (PAGE_POINTS[0] * PAGE_POINTS[1]))
    raster = (math.floor(PAGE_POINTS[0] * scale), math.floor(PAGE_POINTS[1] * scale))
    for number in page_numbers:
        source = Image.open(io.BytesIO(scan.images[number - 1]))
        returned = source.resize(raster, Image.Resampling.BICUBIC)
        buffer = io.BytesIO()
        returned.save(buffer, "PNG")
        page_start = len(text)
        lines, tokens = [], []
        for line_text, box, words in scan.boxes[number - 1]:
            line_start = len(text)
            cursor = len(text)
            for word, word_box in words:
                tokens.append({"layout": {"textAnchor": _anchor(cursor, cursor + len(word)),
                                          "boundingPoly": _norm_box(word_box, scan.size), "confidence": 0.99,
                                          "orientation": "PAGE_UP"}})
                cursor += len(word) + 1
            text += line_text + "\n"
            lines.append({"layout": {"textAnchor": _anchor(line_start, len(text)), "boundingPoly": _norm_box(box, scan.size),
                                     "confidence": 0.99, "orientation": "PAGE_UP"}})
        pages.append({
            "pageNumber": number,
            "dimension": {"width": raster[0], "height": raster[1], "unit": "pixels"},
            "layout": {"textAnchor": _anchor(page_start, len(text)) if len(text) > page_start else {},
                       "boundingPoly": _norm_box((0, 0, *scan.size), scan.size), "orientation": "PAGE_UP",
                       "confidence": 0.99},
            "lines": lines,
            "tokens": tokens,
            "image": {"content": base64.b64encode(buffer.getvalue()).decode(), "mimeType": "image/png",
                      "width": raster[0], "height": raster[1]},
        })
    return {"document": {"text": text, "pages": pages, "mimeType": "application/pdf"}}


class FakeGoogle:
    """A transport for GoogleDocumentAIBackend. `fail` makes the next N calls return that status."""

    def __init__(self):
        self.calls = 0
        self.fail: list[int] = []

    def post(self, endpoint, body, token, timeout_seconds):
        self.calls += 1
        if self.fail:
            return HTTPResponse(status=self.fail.pop(0), body=b"")
        request = json.loads(body)
        data = base64.b64decode(request["rawDocument"]["content"])
        scan = _REGISTRY[hashlib.sha256(data).hexdigest()]
        pages = request["processOptions"]["individualPageSelector"]["pages"]
        return HTTPResponse(status=200, body=json.dumps(google_response(scan, pages)).encode())


def token_provider():
    return "synthetic-test-token"
