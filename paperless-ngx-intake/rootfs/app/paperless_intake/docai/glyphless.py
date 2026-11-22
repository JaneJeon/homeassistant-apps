"""An invisible text layer that can carry any character.

The text Google read is written into the PDF in render mode 3, which draws
nothing. So the font needs no letter shapes. One empty glyph stands for every
character, and the PDF's ToUnicode map says which character each code is.
Tesseract and OCRmyPDF write their text layers the same way.

Why it matters here: a font with letter shapes only covers some scripts. US
mail routinely carries a block of other languages (Korean, Chinese, Thai,
Arabic and so on), and a text layer that depends on a font's coverage either
fails on those pages or silently drops their text. This layer covers every
character in Unicode's basic plane. The few beyond it (rare Chinese characters,
emoji) are written as the replacement character.

Each character is half an em wide. A run of text is stretched to span the box
Google gave for it, so a word's selection sits on the printed word.
"""

from __future__ import annotations

import base64
import io
import re
import unicodedata
import zlib

import numpy as np
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject,
                           TextStringObject)

FONT_RESOURCE = "/IntakeGL"
FONT_NAME = "/IntakeGlyphless"
ADVANCE = 0.5  # width of every character at font size 1
REPLACEMENT = "�"

# A 628-byte TrueType font with two empty glyphs, each 500/1000 em wide.
# Generated once with fontTools (FontBuilder, glyph order [".notdef", "blank"]).
_FONT_ZLIB_B64 = (
    "eNqFkj1Lw1AUht+bpH4hFkEcJYNDB0WQ0knRQamdhLpJQa81JpCPhiaFdnN0EfwF/gJnRydHBf+CIKiz1TW+uQmlUcEXzjn3OZyce+7N"
    "hQAwg3PoMPcP1tZ3kt1tQFSYPW77MsQsIvIVuWp7gzMoiYfUOZY8XbVbLa5vaDWHCf1dfHH9Qlt2/Lgvhqr+gm7C67Rl/v19yr7sh9xX"
    "I0+RzUD61vyH3SM/kfthJ4p9O3oEtD3yCtJZabeb14dH5Y1P6Ppb2u21cnk3HnOpztl2BFWZTZNlChIqo41Yw0/pYshbKMHA/yp036LS"
    "08HMZsh8QebYZNneOm2BXqhopBX0hmIzSTKfPP86B1RdLc8LlEfTaJgksaMxzcwSFvO1hjlUG0EsXavuDULHs6Koadk9T3bRQIAYEi4s"
    "1OFhgBAOo8U3EaHJaKNHluj+cWNFpX9ZK514MnC/AdseTos="
)
FONT_BYTES = zlib.decompress(base64.b64decode(_FONT_ZLIB_B64))


def clean(text: str) -> str:
    """The text as it goes into the layer: one line, single spaces, only characters a two-byte code can carry."""
    out = []
    for char in " ".join(text.split()):
        code = ord(char)
        if code == 0 or 0xD800 <= code <= 0xDFFF or code > 0xFFFF:
            out.append(REPLACEMENT)   # NUL, lone surrogates, and characters beyond the basic plane
        else:
            out.append(char)
    return "".join(out)


_LEFT_TO_RIGHT = {"L", "EN", "AN"}
_RIGHT_TO_LEFT = {"R", "AL"}


def right_to_left(text: str) -> bool:
    """Whether the text has Hebrew or Arabic letters in it, which read from the right."""
    return any(unicodedata.bidirectional(char) in _RIGHT_TO_LEFT for char in text)


def visual(text: str) -> str:
    """A word as it must be stored so that it reads back in the right order.

    A PDF holds text in the order it sits on the page, left to right. For Arabic
    or Hebrew that is the reverse of the order the letters are read in, and text
    extractors (pdftotext among them) turn such runs back around. A word stored
    in reading order comes back reversed, and no search finds it. So a word with
    right-to-left letters is stored turned around: its right-to-left stretches
    letter by letter, and any digits or Latin letters inside it kept as they are.
    Applying this twice gives the word back.
    """
    if not any(unicodedata.bidirectional(char) in _RIGHT_TO_LEFT for char in text):
        return text
    runs, current, current_ltr = [], "", None
    for char in text:
        ltr = unicodedata.bidirectional(char) in _LEFT_TO_RIGHT
        if current and ltr != current_ltr:
            runs.append((current, current_ltr))
            current = ""
        current, current_ltr = current + char, ltr
    runs.append((current, current_ltr))
    return "".join(run if ltr else run[::-1] for run, ltr in reversed(runs))


def search_words(text: str) -> list[str]:
    """The words a search would look for: runs of letters or digits, punctuation and spacing ignored.

    Two copies of the same text can differ in how punctuation attaches ("total:" against "total :")
    without differing in anything a search can see. They can't differ in their words. Scripts
    written without spaces (Chinese, Japanese, Korean) count character by character, because
    whether a text extractor puts spaces between those characters is its own choice.
    """
    words = []
    for word in re.findall(r"\w+", unicodedata.normalize("NFKC", text or "")):
        run = ""
        for char in word:
            if unicodedata.east_asian_width(char) in {"W", "F"}:
                if run:
                    words.append(run)
                    run = ""
                words.append(char)
            else:
                run += char
        if run:
            words.append(run)
    return words


def _hex(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()


def fit(points, characters: int):
    """Text matrix that stretches `characters` half-em cells over the four-corner box.

    Corners come in reading order: start-top, end-top, end-bottom, start-bottom,
    in PDF points with y up. Returns None for a box with no width or height.

    The run is laid along the nearest of the four page directions, centred on
    the box, with the box's own width and height. A scan is usually a degree or
    so askew, and the printed words tilt with it. Text that tilts with them
    reads back badly: pdftotext, which Paperless uses to pull the text out,
    cuts a tilted line in the middle of a word. Laid square on the word's
    centre, the run sits within a point of the printed word and every word
    reads back whole.
    """
    p0, p1, p2, p3 = np.asarray(points, dtype=float)
    along = ((p1 - p0) + (p2 - p3)) / 2      # reading direction
    up = ((p0 - p3) + (p1 - p2)) / 2         # baseline to top
    if not (np.isfinite(along).all() and np.isfinite(up).all()):
        return None
    length, height = float(np.linalg.norm(along)), float(np.linalg.norm(up))
    if characters <= 0 or length < 1e-6 or height < 1e-6:
        return None
    quarter = int(round(np.degrees(np.arctan2(along[1], along[0])) / 90)) % 4
    direction = np.array([(1, 0), (0, 1), (-1, 0), (0, -1)][quarter], dtype=float)
    normal = np.array([-direction[1], direction[0]])
    if float(np.dot(up, normal)) < 0:        # a mirrored box; keep the text readable left to right
        normal = -normal
    along, up = direction * length, normal * height
    origin = np.mean([p0, p1, p2, p3], axis=0) - (along + up) / 2
    a, b = along / (ADVANCE * characters)
    c, d = up
    return [float(a), float(b), float(c), float(d), float(origin[0]), float(origin[1])]


def _number(value: float) -> str:
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def content_stream(runs: list[dict]) -> bytes:
    """One invisible text object holding every run on the page."""
    parts = ["q", "BT", "3 Tr", f"{FONT_RESOURCE} 1 Tf"]
    for run in runs:
        parts.append(" ".join(_number(value) for value in run["matrix"]) + " Tm")
        parts.append(f"<{_hex(run['text'])}> Tj")
    parts += ["ET", "Q"]
    return ("\n".join(parts) + "\n").encode("ascii")


def _to_unicode() -> bytes:
    """Identity map from two-byte codes to the same UTF-16 code units, one range per high byte."""
    ranges = [f"<{high:02X}00> <{high:02X}FF> <{high:02X}00>" for high in range(256)]
    blocks = []
    for start in range(0, len(ranges), 100):
        chunk = ranges[start:start + 100]
        blocks.append(f"{len(chunk)} beginbfrange\n" + "\n".join(chunk) + "\nendbfrange")
    return ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            + "\n".join(blocks) +
            "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n").encode("ascii")


def _stream(data: bytes, extra: dict | None = None):
    stream = DecodedStreamObject()
    stream.set_data(data)
    for key, value in (extra or {}).items():
        stream[NameObject(key)] = value
    return stream.flate_encode()


def _font(writer: PdfWriter):
    font_file = writer._add_object(_stream(FONT_BYTES, {"/Length1": NumberObject(len(FONT_BYTES))}))
    descriptor = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/FontDescriptor"),
        NameObject("/FontName"): NameObject(FONT_NAME),
        NameObject("/Flags"): NumberObject(5),
        NameObject("/FontBBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(500), NumberObject(1000)]),
        NameObject("/ItalicAngle"): NumberObject(0),
        NameObject("/Ascent"): NumberObject(1000),
        NameObject("/Descent"): NumberObject(0),
        NameObject("/CapHeight"): NumberObject(1000),
        NameObject("/StemV"): NumberObject(80),
        NameObject("/FontFile2"): font_file,
    }))
    cid_to_gid = writer._add_object(_stream(b"\x00\x01" * 65536))   # every code -> the empty glyph
    cid_font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/CIDFontType2"),
        NameObject("/BaseFont"): NameObject(FONT_NAME),
        NameObject("/CIDSystemInfo"): DictionaryObject({
            NameObject("/Registry"): TextStringObject("Adobe"),
            NameObject("/Ordering"): TextStringObject("Identity"),
            NameObject("/Supplement"): NumberObject(0),
        }),
        NameObject("/FontDescriptor"): descriptor,
        NameObject("/CIDToGIDMap"): cid_to_gid,
        NameObject("/DW"): NumberObject(500),
    }))
    to_unicode = writer._add_object(_stream(_to_unicode()))
    return writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject(FONT_NAME),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([cid_font]),
        NameObject("/ToUnicode"): to_unicode,
    }))


def overlay(pages: list[tuple[tuple[float, float], list[dict]]]) -> PdfReader:
    """A PDF with one page per entry, holding only that page's invisible text. All pages share one font."""
    writer = PdfWriter()
    font = _font(writer)
    for (width, height), runs in pages:
        page = writer.add_blank_page(width=width, height=height)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject(FONT_RESOURCE): font}),
        })
        page[NameObject("/Contents")] = writer._add_object(_stream(content_stream(runs)))
    buffer = io.BytesIO()
    writer.write(buffer)
    return PdfReader(io.BytesIO(buffer.getvalue()))
