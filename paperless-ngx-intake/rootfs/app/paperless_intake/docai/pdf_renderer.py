"""Build the searchable copy: the scanner's own page images, turned upright and
put in reading order, with Google's text laid invisibly on top.

Three things here can damage a document, so they are checked hard. If one
fails, ReviewRequired is raised and no searchable copy is made:

- every source page appears in the output exactly once;
- each page's image is the scanner's JPEG, byte for byte;
- the added text is invisible.

Everything else is about where the invisible text sits. That decides how well
a search highlight lines up with the printed word, and nothing more: the page
images are the scanner's either way, and the original scan is stored beside
this copy. So placement is measured and recorded, and never stops a scan.
Version 1.0 treated placement as a gate and held the first two real scans it
saw, one over a character its font lacked and one over image alignment.

How text gets from Google's frame to the page: Google returns each page as an
upright, deskewed image, with text boxes in that image's frame, plus the
transform it applied to get there. Inverting that transform takes a box back to
the scanned page, and the quarter turn chosen for the page takes it to the
output page.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path
import struct

import numpy as np
from PIL import Image
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject

from . import glyphless

# Measures recorded with every page. `registration` values are the limits the
# 1.0 gates used; they are kept as a yardstick, not as gates.
POLICY = {
    "registrationCorrelationYardstick": 0.985,
    "registrationMeanAbsoluteGrayDifferenceYardstick": 6.0,
    "rasterAgreementPixels": 1.5,
    "wordMatchYardstick": 0.98,
}
ORIENTATION_TURNS = {"PAGE_UP": 0, "PAGE_RIGHT": 270, "PAGE_DOWN": 180, "PAGE_LEFT": 90}


class ReviewRequired(ValueError):
    """The searchable copy can't be built safely from this scan."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def anchored(text: str, anchor: dict) -> str:
    """Default reader for a Google text anchor. The app passes its validated reader instead."""
    parts, previous_end = [], None
    for segment in anchor.get("textSegments", []) or []:
        start, end = int(segment.get("startIndex", 0)), int(segment["endIndex"])
        if start == end:
            continue
        if previous_end is not None and start > previous_end:
            gap = text[previous_end:start]
            parts.append(gap if not gap.strip() else "\n")
        parts.append(text[start:end])
        previous_end = end
    return "".join(parts)


def source_image(page) -> tuple[bytes, Image.Image]:
    """The page's one full-page JPEG. Anything else is a scan format this renderer doesn't handle."""
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    if list(page.mediabox) != [0, 0, width, height] or list(page.cropbox) != list(page.mediabox):
        raise ReviewRequired("Nonzero-origin or cropped source page")
    if page.rotation != 0 or float(page.get("/UserUnit", 1)) != 1 or page.get("/Annots"):
        raise ReviewRequired("Source rotation, user unit, or annotations require separate handling")
    objects = page["/Resources"].get("/XObject", {})
    if len(objects) != 1:
        raise ReviewRequired("Exactly one full-page image is required")
    name, reference = next(iter(objects.items()))
    obj = reference.get_object()
    if obj.get("/Subtype") != "/Image" or obj.get("/Filter") != "/DCTDecode" or obj.get("/SMask") or obj.get("/Mask"):
        raise ReviewRequired("Only a direct JPEG image without masks is supported")
    ops = page.get_contents().operations
    if [op for _, op in ops] != [b"q", b"cm", b"Do", b"Q"]:
        raise ReviewRequired("Source must contain only one image and its full-page placement")
    if not np.allclose(list(map(float, ops[1][0])), [width, 0, 0, height, 0, 0], rtol=0, atol=1e-9) or ops[2][0] != [name]:
        raise ReviewRequired("Image does not exactly fill the source page")
    data = obj._data
    image = Image.open(io.BytesIO(data))
    if image.format != "JPEG" or image.size != (obj["/Width"], obj["/Height"]):
        raise ReviewRequired("JPEG bytes and image metadata disagree")
    return data, image


def cardinal_matrix(degrees: int, width: float, height: float) -> tuple[np.ndarray, tuple[float, float]]:
    """Clockwise quarter turn of a page, as a matrix on PDF points, and the turned page's size."""
    if degrees not in {0, 90, 180, 270}:
        raise ReviewRequired("Only clockwise quarter turns are allowed")
    matrices = {
        0: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        90: [[0, 1, 0], [-1, 0, width], [0, 0, 1]],
        180: [[-1, 0, width], [0, -1, height], [0, 0, 1]],
        270: [[0, -1, height], [1, 0, 0], [0, 0, 1]],
    }
    return np.array(matrices[degrees], dtype=float), ((height, width) if degrees in {90, 270} else (width, height))


def google_transform(google_page: dict) -> tuple[np.ndarray, str | None]:
    """The transform Google applied to the page image, or identity with a note when it can't be used."""
    transforms = google_page.get("transforms") or []
    if not transforms:
        return np.eye(3), None
    if len(transforms) > 1:
        return np.eye(3), "Google reported more than one transform"
    item = transforms[0]
    try:
        if (item["rows"], item["cols"], item["type"]) != (2, 3, 6):
            return np.eye(3), "Google's transform isn't a 2x3 matrix of doubles"
        T = np.eye(3)
        T[:2] = np.array(struct.unpack("<6d", base64.b64decode(item["data"], validate=True))).reshape(2, 3)
    except (KeyError, TypeError, ValueError, struct.error):
        return np.eye(3), "Google's transform couldn't be read"
    linear = T[:2, :2]
    if (not np.isfinite(T).all() or not np.allclose(linear.T @ linear, np.eye(2), atol=1e-3, rtol=0)
            or abs(np.linalg.det(linear) - 1) > 1e-3):
        return np.eye(3), "Google's transform isn't a plain rotation"
    return T, None


def read_rotation(google_page: dict) -> dict:
    """Google's own reading of which way up this page is, and how much text it found.

    The transform is the main source: it is the rotation Google applied to make
    the page upright. Its nearest quarter turn is what the page needs; the rest
    is the scan's own slight skew, which is kept. With no usable transform, the
    page layout's orientation label is used.
    """
    T, note = google_transform(google_page)
    words = len(google_page.get("tokens") or []) or len(google_page.get("lines") or [])
    reading = {"words": words, "transformNote": note}
    if google_page.get("transforms") and note is None:
        angle = math.degrees(math.atan2(T[1, 0], T[0, 0]))
        cardinal = int(round(angle / 90) * 90) % 360
        reading.update(cardinal=cardinal, source="transform", googleAngleDegrees=angle,
                       skewDegrees=(angle - cardinal + 180) % 360 - 180)
        return reading
    label = (google_page.get("layout") or {}).get("orientation")
    if label in ORIENTATION_TURNS and label != "PAGE_UP":
        reading.update(cardinal=ORIENTATION_TURNS[label], source="orientation_label", orientationLabel=label)
        return reading
    reading.update(cardinal=0, source="none")
    return reading


def raster_size(T: np.ndarray, returned: tuple[int, int], page_points: tuple[float, float]) -> tuple[float, float, str]:
    """Size of the page raster Google rotated to make the image it returned.

    The returned image is the box around that raster after rotation, so its
    size and the rotation angle give the raster's size back. Google's rasters of
    these scans have been about 4 megapixels; when that guess agrees with the
    solved size it is used, as in version 1.0. Otherwise the solved size is.
    """
    gw, gh = returned
    cos, sin = abs(float(T[0, 0])), abs(float(T[1, 0]))
    determinant = cos * cos - sin * sin
    if abs(determinant) < 0.1:
        return float(gw), float(gh), "returned_image_size"
    solved = ((cos * gw - sin * gh) / determinant, (cos * gh - sin * gw) / determinant)
    if solved[0] <= 0 or solved[1] <= 0:
        return float(gw), float(gh), "returned_image_size"
    width, height = page_points
    scale = math.sqrt(4_000_000 / (width * height))
    guess = (math.floor(width * scale), math.floor(height * scale))
    if max(abs(guess[0] - solved[0]), abs(guess[1] - solved[1])) <= POLICY["rasterAgreementPixels"]:
        return float(guess[0]), float(guess[1]), "four_megapixel_raster"
    return float(solved[0]), float(solved[1]), "solved_from_returned_image"


def registration(image: Image.Image, google_page: dict, T: np.ndarray, raster: tuple[float, float]) -> dict:
    """How closely the scan, pushed through the mapping, matches the image Google returned. A measure only."""
    try:
        returned = google_page["image"]
        returned_image = Image.open(io.BytesIO(base64.b64decode(returned["content"], validate=True)))
        H = T @ np.diag([raster[0] / image.width, raster[1] / image.height, 1])
        inverse = np.linalg.inv(H)
        predicted = image.convert("L").transform(returned_image.size, Image.Transform.AFFINE,
                                                 tuple(inverse[:2].flatten()), Image.Resampling.BICUBIC, fillcolor=255)
        factor = min(1, 1000 / max(returned_image.size))
        compare_size = tuple(max(16, round(value * factor)) for value in returned_image.size)
        a = np.asarray(predicted.resize(compare_size, Image.Resampling.LANCZOS), dtype=float)[4:-4, 4:-4]
        b = np.asarray(returned_image.convert("L").resize(compare_size, Image.Resampling.LANCZOS), dtype=float)[4:-4, 4:-4]
        correlation = float(np.corrcoef(a.ravel(), b.ravel())[0, 1])
        mae = float(np.mean(np.abs(a - b)))
        if not math.isfinite(correlation):      # a page with no variation, such as a blank one
            return {"measured": False, "reason": "no variation to compare"}
        return {"measured": True, "correlation": correlation, "meanAbsoluteGrayDifference": mae,
                "withinYardstick": correlation >= POLICY["registrationCorrelationYardstick"]
                and mae <= POLICY["registrationMeanAbsoluteGrayDifferenceYardstick"]}
    except Exception as error:      # a measure, never a reason to stop
        return {"measured": False, "reason": type(error).__name__}


def _returned_size(google_page: dict) -> tuple[int, int] | None:
    for key in ("image", "dimension"):
        block = google_page.get(key) or {}
        width, height = block.get("width"), block.get("height")
        if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
            return int(round(width)), int(round(height))
    return None


def polygon(layout: dict, returned: tuple[int, int], projection: np.ndarray) -> list[list[float]] | None:
    """A text box's four corners on the output page, or None when Google gave no usable box."""
    box = layout.get("boundingPoly") or {}
    if box.get("normalizedVertices"):
        points = [[v.get("x", 0) * returned[0], v.get("y", 0) * returned[1], 1] for v in box["normalizedVertices"]]
    elif box.get("vertices"):
        points = [[v.get("x", 0), v.get("y", 0), 1] for v in box["vertices"]]
    else:
        return None
    if len(points) != 4:
        return None
    try:
        projected = (projection @ np.array(points, dtype=float).T).T[:, :2]
    except (TypeError, ValueError):
        return None
    return projected.tolist() if np.isfinite(projected).all() else None


def _ends_a_word(raw_text: str, node: dict) -> bool:
    if raw_text[-1:].isspace():
        return True
    return (node.get("detectedBreak") or {}).get("type") in {"SPACE", "WIDE_SPACE", "HYPHEN"}


def page_runs(document: dict, google_page: dict, returned: tuple[int, int], projection: np.ndarray,
              read_text) -> tuple[list[dict], dict]:
    """One invisible run per word, each stretched over the box Google gave for it.

    Words without a usable box or without text are left out and counted. Lines
    are used when a page has no word-level results.
    """
    level = "tokens" if google_page.get("tokens") else "lines"
    runs, skipped = [], 0
    for node in google_page.get(level) or []:
        layout = node.get("layout") or {}
        try:
            raw_text = read_text(document["text"], layout.get("textAnchor") or {})
        except Exception:
            skipped += 1
            continue
        text = glyphless.clean(raw_text)
        if not text:
            continue
        points = polygon(layout, returned, projection)
        matrix = glyphless.fit(points, len(text)) if points else None
        if matrix is None:
            skipped += 1
            continue
        runs.append({"text": text + (" " if _ends_a_word(raw_text, node) else ""), "matrix": matrix})
    return runs, {"level": level, "placed": len(runs), "leftOut": skipped}


def _words(text: str) -> Counter:
    return Counter(glyphless.search_words(text))


def thumbnail(source_pdf_path: Path, work_dir: Path) -> Path:
    """Local first-page thumbnail, without orientation inference or OCR."""
    _, image = source_image(PdfReader(source_pdf_path).pages[0])
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = work_dir / "source-thumbnail.webp"
    if result.exists():
        raise FileExistsError(result)
    image = image.convert("RGB")
    image.thumbnail((640, 640), Image.Resampling.LANCZOS)
    image.save(result, "WEBP", quality=90)
    result.chmod(0o600)
    return result


def render_document(source_pdf_path: Path, document: dict, out_dir: Path, *, page_order: list[int],
                    cardinal_clockwise: list[int], prefix: str, text_reader=None) -> dict:
    """Write the searchable copy and return its path with what was measured. No network use.

    cardinal_clockwise is indexed by SOURCE page. page_order lists source pages in output order.
    """
    original_bytes = source_pdf_path.read_bytes()
    read_text = text_reader or anchored
    source_sha = digest(original_bytes)
    reader = PdfReader(io.BytesIO(original_bytes))
    count = len(reader.pages)
    if sorted(page_order) != list(range(1, count + 1)) or len(cardinal_clockwise) != count:
        raise ReviewRequired("Page order must contain every source page exactly once")
    if len(document["pages"]) != count or [p["pageNumber"] for p in document["pages"]] != list(range(1, count + 1)):
        raise ReviewRequired("Response does not exactly match source page numbering/count")

    records = []
    for number, (page, google_page, clockwise) in enumerate(zip(reader.pages, document["pages"], cardinal_clockwise), 1):
        image_bytes, image = source_image(page)
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        T, transform_note = google_transform(google_page)
        returned = _returned_size(google_page)
        C, output_size = cardinal_matrix(clockwise, width, height)
        evidence = {"sourcePage": number, "appliedCardinalClockwiseDegrees": clockwise,
                    "sourceMediaBoxPoints": [width, height], "outputMediaBoxPoints": list(output_size),
                    "transformNote": transform_note}
        runs, stats = [], {"level": None, "placed": 0, "leftOut": 0}
        if returned is None:
            evidence["placement"] = "no text placed: Google gave no page size"
        else:
            iw, ih, how = raster_size(T, returned, (width, height))
            raster_to_source_pdf = np.array([[width / iw, 0, 0], [0, -height / ih, height], [0, 0, 1]])
            projection = C @ raster_to_source_pdf @ np.linalg.inv(T)
            runs, stats = page_runs(document, google_page, returned, projection, read_text)
            evidence["returnedImagePixels"] = list(returned)
            evidence["rasterPixels"] = [iw, ih]
            evidence["rasterMapping"] = how
            evidence["registration"] = registration(image, google_page, T, (iw, ih))
        expected = read_text(document["text"], (google_page.get("layout") or {}).get("textAnchor") or {})
        records.append({"sourcePage": number, "imageBytes": image_bytes, "expectedText": expected, "runs": runs,
                        "outputSize": output_size, "evidence": {**evidence, "text": stats}})

    ordered = [records[n - 1] for n in page_order]
    text_layer = glyphless.overlay([(record["outputSize"], record["runs"]) for record in ordered])
    writer = PdfWriter()
    for record, overlay_page in zip(ordered, text_layer.pages):
        page = writer.add_page(reader.pages[record["sourcePage"] - 1])
        rotation = cardinal_clockwise[record["sourcePage"] - 1]
        size = record["outputSize"]
        if rotation:
            # Exact quarter-turn coefficients: no trigonometry, so no resampling drift in viewers.
            C, _ = cardinal_matrix(rotation, float(page.mediabox.width), float(page.mediabox.height))
            page.add_transformation(Transformation((C[0, 0], C[1, 0], C[0, 1], C[1, 1], C[0, 2], C[1, 2])))
            page.mediabox = RectangleObject([0, 0, *size])
            page.cropbox = RectangleObject([0, 0, *size])
        if record["runs"]:
            page.merge_page(overlay_page)
    buffer = io.BytesIO()
    writer.write(buffer)
    output_bytes = buffer.getvalue()

    # The three hard checks, on the bytes that will be stored.
    reopened = PdfReader(io.BytesIO(output_bytes))
    if len(reopened.pages) != count:
        raise ReviewRequired("Output page count changed")
    checks = []
    for output_number, (page, record) in enumerate(zip(reopened.pages, ordered), 1):
        embedded = [obj.get_object() for obj in page["/Resources"]["/XObject"].values()
                    if obj.get_object().get("/Subtype") == "/Image"]
        if len(embedded) != 1 or embedded[0].get("/Filter") != "/DCTDecode" or embedded[0]._data != record["imageBytes"]:
            raise ReviewRequired("Original JPEG stream was not preserved byte-for-byte")
        if page.rotation != 0 or not np.allclose([float(page.mediabox.width), float(page.mediabox.height)],
                                                 record["outputSize"], atol=1e-8, rtol=0):
            raise ReviewRequired("Output page orientation or size changed unexpectedly")
        operations = page.get_contents().operations
        modes = [int(args[0]) for args, op in operations if op == b"Tr"]
        if record["runs"] and (not modes or any(mode != 3 for mode in modes)):
            raise ReviewRequired("Text is not entirely invisible")
        # A measure: are the words Google gave for this page the words now stored in the layer?
        # Read from the stored bytes themselves, so no text extractor's layout guesses come into it.
        expected, actual = _words(record["expectedText"]), Counter()
        for args, op in operations:
            if op == b"Tj":
                raw = args[0]
                data = raw.original_bytes if hasattr(raw, "original_bytes") else bytes(raw)
                actual.update(glyphless.search_words(data.decode("utf-16-be", "replace")))
        total = sum(expected.values())
        missing = sum((expected - actual).values())
        checks.append({"outputPage": output_number, "sourcePage": record["sourcePage"],
                       "originalJpegStreamByteIdentical": True, "wordsExpected": total, "wordsMissingFromLayer": missing,
                       "wordMatch": round(1 - missing / total, 4) if total else 1.0})
    if digest(source_pdf_path.read_bytes()) != source_sha:
        raise ReviewRequired("Source PDF changed")

    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive_path = out_dir / f"{prefix}.pdf"
    manifest_path = out_dir / f"{prefix}.manifest.json"
    if archive_path.exists() or manifest_path.exists():
        raise FileExistsError("Output already exists; inspect before replacing")
    with archive_path.open("xb") as stream:
        stream.write(output_bytes)
    archive_path.chmod(0o600)
    provenance = {
        "sourceSha256": source_sha, "sourceByteCount": len(original_bytes),
        "archiveSha256": digest(output_bytes), "archiveByteCount": len(output_bytes),
        "pageMappingOutputToSource": list(page_order),
        "cardinalClockwiseBySourcePage": list(cardinal_clockwise),
        "policy": POLICY, "checks": checks,
        "pages": [{"outputPage": index, "originalJpegSha256": digest(record["imageBytes"]), **record["evidence"]}
                  for index, record in enumerate(ordered, 1)],
    }
    with manifest_path.open("x") as stream:
        json.dump(provenance, stream, indent=2)
        stream.write("\n")
    manifest_path.chmod(0o600)
    return {"archive_path": archive_path, "manifest_path": manifest_path, "provenance": provenance}


def quality_notes(provenance: dict) -> list[str]:
    """Plain remarks about the text layer, for the record. None of them concern the page images."""
    notes = []
    pages = provenance.get("pages") or []
    left_out = sum((page.get("text") or {}).get("leftOut", 0) for page in pages)
    if left_out:
        notes.append(f"{left_out} word{'' if left_out == 1 else 's'} had no usable position and "
                     f"{'was' if left_out == 1 else 'were'} left out of the text layer")
    unplaced = [page["outputPage"] for page in pages if "placement" in page]
    if unplaced:
        notes.append(f"no text could be placed on page{'' if len(unplaced) == 1 else 's'} "
                     f"{', '.join(map(str, unplaced))}")
    off = [page["outputPage"] for page in pages
           if (page.get("registration") or {}).get("measured") and not page["registration"]["withinYardstick"]]
    if off:
        notes.append(f"the text position on page{'' if len(off) == 1 else 's'} {', '.join(map(str, off))} "
                     f"couldn't be confirmed against Google's page image, so highlights there may sit slightly off")
    odd = [page["outputPage"] for page in pages if page.get("transformNote")]
    if odd:
        notes.append(f"Google's rotation data for page{'' if len(odd) == 1 else 's'} {', '.join(map(str, odd))} "
                     f"couldn't be used, so text there is placed without it")
    low = [check["outputPage"] for check in provenance.get("checks") or []
           if check["wordsExpected"] and check["wordMatch"] < POLICY["wordMatchYardstick"]]
    if low:
        notes.append(f"some of Google's words on page{'' if len(low) == 1 else 's'} {', '.join(map(str, low))} "
                     f"didn't read back out of the text layer")
    return notes
