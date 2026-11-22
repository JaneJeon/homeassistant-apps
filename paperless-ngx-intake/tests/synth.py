"""Synthetic scans and a fake Google Document AI that answers like the real one.

A synthetic scan is a PDF with one full-page grayscale JPEG per page, the shape
the Brother scanner produces. The fake Google returns, for the requested pages,
the page image rasterised the way Document AI does (about 4 megapixels), plus
lines and tokens with text anchors and normalized boxes. That is enough for
the real backend, page planner and renderer to run unchanged.

A scan can be made the way a stack really goes into the feeder: turned head to
foot, flipped over, or skewed. For a turned or skewed page the fake Google does
what the real one did on Jane's pilot scans: it returns the page image already
upright and deskewed, with text boxes in that image's frame, and reports the
rotation it applied in `transforms`. `report="label"` models the other way the
API can describe a turned page: no transform, boxes in the page as scanned,
and an orientation label such as PAGE_DOWN.

Everything here is synthetic; no real mail is used.
"""

from __future__ import annotations

import base64
import hashlib
import io
import zlib
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
    unturned: bool = False         # Google doesn't notice this page is turned (too little text to tell)


@dataclass
class Scan:
    pdf: bytes
    pages: list[PageSpec]
    images: list[bytes] = field(default_factory=list)       # source JPEGs, as the scanner saved them
    boxes: list[list[tuple[str, tuple[int, int, int, int], list]]] = field(default_factory=list)
    size: tuple[int, int] = PIXELS                          # the upright page, in pixels
    turns: list[int] = field(default_factory=list)          # clockwise quarter turn each page needs
    skew: float = 0.0                                       # feed skew in degrees, which Google removes
    report: str = "transform"                               # how the fake Google reports a turned page
    google_image: str = "faithful"                          # "inverted": Google's page image doesn't match

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


def _bits(data: bytes):
    """The page as a one-bit image, the way a scanner stores a black and white page."""
    return Image.open(io.BytesIO(data)).convert("L").point(lambda value: 255 if value > 160 else 0).convert("1")


def _image_object(data: bytes, encoding: str) -> bytes:
    """One page image as a PDF object body, in the format a scanner would have chosen for it."""
    width, height = Image.open(io.BytesIO(data)).size
    head = b"<< /Type /XObject /Subtype /Image /Width %d /Height %d " % (width, height)
    if encoding == "jpeg":
        body, rest = data, b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /DCTDecode"
    elif encoding == "flate":
        body = zlib.compress(_bits(data).tobytes())
        rest = b"/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /FlateDecode"
    elif encoding == "ccitt":
        buffer = io.BytesIO()
        _bits(data).save(buffer, "TIFF", compression="group4", tiffinfo={278: height})   # all rows in one strip
        tiff = Image.open(io.BytesIO(buffer.getvalue()))
        offsets, counts = tiff.tag_v2[273], tiff.tag_v2[279]
        assert len(offsets) == 1, "the fax data must be one strip to be one PDF stream"
        body = buffer.getvalue()[offsets[0]:offsets[0] + counts[0]]
        rest = (b"/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /CCITTFaxDecode "
                b"/DecodeParms << /K -1 /Columns %d /Rows %d /BlackIs1 true >>" % (width, height))
    elif encoding == "jbig2":
        # Not real JBIG2 data. Nothing that builds the copy may need to decode a page image,
        # and this is the stand-in for every format the app has no decoder for.
        body = b"not decodable here " + hashlib.sha256(data).digest() * 8
        rest = b"/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /JBIG2Decode"
    else:
        raise ValueError(encoding)
    return head + rest + b" /Length %d >>\nstream\n" % len(body) + body + b"\nendstream"


def _pdf(images: list[bytes], encodings: list[str] | None = None) -> bytes:
    """Hand-written PDF: each page is exactly `q W 0 0 H 0 0 cm /Im0 Do Q`. Wide images get a landscape page.

    encodings gives each page's image format. "layered" is the exception to the one-image
    shape: a JPEG background with a one-bit stencil drawn over it, as high-compression
    scanner modes write.
    """
    objects: list[bytes] = []
    encodings = encodings or ["jpeg"] * len(images)

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholder 1
    pages_obj = add(b"")  # placeholder 2
    kids = []
    for data, encoding in zip(images, encodings):
        width, height = Image.open(io.BytesIO(data)).size
        page_w, page_h = (792, 612) if width > height else (612, 792)
        content = b"q %d 0 0 %d 0 0 cm /Im0 Do Q" % (page_w, page_h)
        if encoding == "layered":
            image_obj = add(_image_object(data, "jpeg"))
            stencil = zlib.compress(bytes(255 - b for b in _bits(data).tobytes()))        # 1 paints, 0 leaves alone
            mask_obj = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ImageMask true "
                           b"/BitsPerComponent 1 /Filter /FlateDecode /Length %d >>\nstream\n"
                           % (width, height, len(stencil)) + stencil + b"\nendstream")
            content += b" q 0 g %d 0 0 %d 0 0 cm /Im1 Do Q" % (page_w, page_h)
            xobjects = b"/Im0 %d 0 R /Im1 %d 0 R" % (image_obj, mask_obj)
        else:
            xobjects = b"/Im0 %d 0 R" % add(_image_object(data, encoding))
        content_obj = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        page_obj = add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /XObject << " % (page_w, page_h)
                       + xobjects + b" >> >> /Contents %d 0 R >>" % content_obj)
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


def _as_scanned(upright_jpeg: bytes, turn: int, skew: float) -> bytes:
    """The page image as the scanner saved it: turned with the stack, and slightly skewed in the feed."""
    if not turn and not skew:
        return upright_jpeg
    image = Image.open(io.BytesIO(upright_jpeg))
    if turn:
        image = image.rotate(turn, expand=True)                    # PIL turns counter-clockwise
    if skew:
        image = image.rotate(skew, resample=Image.Resampling.BICUBIC, fillcolor=255)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def make_scan(pages: list[PageSpec], *, scale: float = 1.0, turn: int = 0, flipped: bool = False,
              skew: float = 0.0, turns: list[int] | None = None, report: str = "transform",
              google_image: str = "faithful", encodings: list[str] | None = None) -> Scan:
    """A scan of `pages`, given in reading order.

    turn: the whole stack went in turned, so every page needs this clockwise quarter turn (180 = head to foot).
    flipped: the stack went in flipped over, so the pages come out in reverse.
    turns: a turn per page (in reading order) instead of one for the stack.
    skew: every page is this many degrees askew, and Google's transform removes it.
    encodings: the image format of each page (in reading order): "jpeg", "flate" or "ccitt" (one-bit
        black and white), "jbig2" (a format the app can't decode) or "layered" (two images on the page).
    """
    specs = list(pages)
    page_turns = list(turns) if turns is not None else [turn] * len(specs)
    page_encodings = list(encodings) if encodings is not None else ["jpeg"] * len(specs)
    if flipped:
        specs, page_turns, page_encodings = specs[::-1], page_turns[::-1], page_encodings[::-1]
    images, boxes = [], []
    for spec, page_turn in zip(specs, page_turns):
        data, placed = _draw_page(spec, scale)
        images.append(_as_scanned(data, page_turn, skew))
        boxes.append(placed)
    size = (round(PIXELS[0] * scale), round(PIXELS[1] * scale))
    scan = Scan(_pdf(images, page_encodings), specs, images, boxes, size, page_turns, skew, report, google_image)
    _REGISTRY[scan.sha256] = scan
    return scan


def numbered_letter(count: int, *, missing: tuple[int, ...] = (), scale: float = 1.0, tag: str = "",
                    unnumbered: tuple[int, ...] = (), **stack) -> Scan:
    """A letter with "Page n of count" footers; pages in `missing` are left out, pages in `unnumbered` have no footer."""
    specs = []
    for number in range(1, count + 1):
        if number in missing:
            continue
        body = [f"Synthetic account statement line {number}.{i} for testing purposes only" for i in range(1, 6)]
        if tag:
            body.append(f"Reference {tag}")
        specs.append(PageSpec(body, footer=None if number in unnumbered else f"Page {number} of {count}"))
    return make_scan(specs, scale=scale, **stack)


def _anchor(start, end):
    return {"textSegments": [{"startIndex": str(start), "endIndex": str(end)}]}


def _turned(point, turn: int, size):
    """Where an upright-page pixel sits in the page as scanned (turned counter-clockwise by `turn`)."""
    x, y = point
    w, h = size
    return {0: (x, y), 90: (y, w - x), 180: (w - x, h - y), 270: (h - y, x)}[turn]


def _skewed(point, skew: float, size):
    """The same pixel after the feed skew: a counter-clockwise turn about the image centre."""
    if not skew:
        return point
    cx, cy = size[0] / 2, size[1] / 2
    dx, dy = point[0] - cx, point[1] - cy
    cos, sin = math.cos(math.radians(skew)), math.sin(math.radians(skew))
    return cx + dx * cos + dy * sin, cy - dx * sin + dy * cos


def _google_frame(scan: Scan, number: int):
    """How Google sees one page: the raster it works on, the transform it applies, and the image it returns."""
    source = Image.open(io.BytesIO(scan.images[number - 1]))
    turn = scan.turns[number - 1] if scan.turns else 0
    points = (792.0, 612.0) if source.width > source.height else PAGE_POINTS
    scale = math.sqrt(4_000_000 / (points[0] * points[1]))
    raster = (math.floor(points[0] * scale), math.floor(points[1] * scale))
    rastered = source.resize(raster, Image.Resampling.BICUBIC)
    seen = scan.report == "transform" and not scan.pages[number - 1].unturned
    angle = (turn + scan.skew) if seen else 0.0
    if not angle:
        return source, turn, raster, None, rastered
    cos, sin = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    if not scan.skew:                              # exact quarter turns, no trigonometric dust
        cos, sin = float(round(cos)), float(round(sin))
    corners = [(0, 0), (raster[0], 0), (raster[0], raster[1]), (0, raster[1])]
    xs = [cos * x - sin * y for x, y in corners]
    ys = [sin * x + cos * y for x, y in corners]
    T = [[cos, -sin, -min(xs)], [sin, cos, -min(ys)]]          # clockwise turn, then shift to start at 0,0
    canvas = (math.ceil(max(xs) - min(xs) - 1e-6), math.ceil(max(ys) - min(ys) - 1e-6))
    full = [T[0], T[1], [0, 0, 1]]
    det = cos * cos + sin * sin
    inverse = (cos / det, sin / det, -(cos * T[0][2] + sin * T[1][2]) / det,
               -sin / det, cos / det, (sin * T[0][2] - cos * T[1][2]) / det)
    returned = rastered.transform(canvas, Image.Transform.AFFINE, inverse, Image.Resampling.BICUBIC, fillcolor=255)
    return source, turn, raster, full, returned


def _box(scan: Scan, source_size, turn: int, raster, T, canvas, box):
    """A word's box in the frame of the image Google returns, as normalized vertices in reading order."""
    x0, y0, x1, y1 = box
    vertices = []
    for point in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        if T is not None:
            x, y = _skewed(_turned(point, turn, scan.size), scan.skew, source_size)
        else:
            x, y = _turned(point, turn, scan.size)          # boxes stay in the frame of the page as scanned
        x, y = x * raster[0] / source_size[0], y * raster[1] / source_size[1]
        if T is not None:
            x, y = T[0][0] * x + T[0][1] * y + T[0][2], T[1][0] * x + T[1][1] * y + T[1][2]
        vertices.append({"x": x / canvas[0], "y": y / canvas[1]})
    return {"normalizedVertices": vertices}


LABELS = {0: "PAGE_UP", 90: "PAGE_LEFT", 180: "PAGE_DOWN", 270: "PAGE_RIGHT"}


def google_response(scan: Scan, page_numbers: list[int]) -> dict:
    text = ""
    pages = []
    for number in page_numbers:
        source, turn, raster, T, returned = _google_frame(scan, number)
        if scan.google_image == "inverted":
            returned = Image.eval(returned.convert("L"), lambda value: 255 - value)
        canvas = returned.size
        orientation = "PAGE_UP" if scan.report == "transform" else LABELS[turn]
        spec = scan.pages[number - 1]
        buffer = io.BytesIO()
        returned.save(buffer, "PNG")
        page_start = len(text)
        lines, tokens = [], []
        for line_text, box, words in scan.boxes[number - 1]:
            line_start = len(text)
            cursor = len(text)
            for word, word_box in words:
                tokens.append({"layout": {"textAnchor": _anchor(cursor, cursor + len(word)),
                                          "boundingPoly": _box(scan, source.size, turn, raster, T, canvas, word_box),
                                          "confidence": 0.99, "orientation": orientation}})
                cursor += len(word) + 1
            text += line_text + "\n"
            lines.append({"layout": {"textAnchor": _anchor(line_start, len(text)),
                                     "boundingPoly": _box(scan, source.size, turn, raster, T, canvas, box),
                                     "confidence": 0.99, "orientation": orientation}})
        page = {
            "pageNumber": number,
            "dimension": {"width": canvas[0], "height": canvas[1], "unit": "pixels"},
            "layout": {"textAnchor": _anchor(page_start, len(text)) if len(text) > page_start else {},
                       "boundingPoly": {"normalizedVertices": [{"x": 0, "y": 0}, {"x": 1, "y": 0},
                                                               {"x": 1, "y": 1}, {"x": 0, "y": 1}]},
                       "orientation": orientation, "confidence": 0.99},
            "lines": lines,
            "tokens": tokens,
            "image": {"content": base64.b64encode(buffer.getvalue()).decode(), "mimeType": "image/png",
                      "width": canvas[0], "height": canvas[1]},
        }
        if T is not None:
            import struct
            page["transforms"] = [{"rows": 2, "cols": 3, "type": 6,
                                   "data": base64.b64encode(struct.pack("<6d", *T[0], *T[1])).decode()}]
        pages.append(page)
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
