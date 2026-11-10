#!/usr/bin/env python3
"""Narrow local PDF demonstrator: original JPEG streams plus projected OCR.

Only one full-page JPEG per source page is supported. Page order and clockwise
cardinal rotations are explicit inputs. The inferred Google 4MP raster mapping
must pass geometry AND actual returned-image registration checks. Unsupported
geometry fails for review; there is no fallback to guessed placement.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path
import re
import struct
import unicodedata

import numpy as np
from PIL import Image
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_NAME = "SourcePreservedOCR"
POLICY = {
    "minimumImageCorrelation": 0.985,
    "maximumMeanAbsoluteGrayDifferenceOutOf255": 6.0,
    "maximumTransformedCanvasEdgeDifferencePixels": 0.5,
    "maximumTransformedSourceMinimumDifferencePixels": 0.0002,
    "maximumRotationResidualFromApprovedCardinalDegrees": 0.5,
    "maximumEncodedTextMatrixDifference": 0.001,
    "maximumAffineLineCornerErrorPoints": 0.15,
    "maximumLinePolygonOutsidePagePoints": 0.1,
}


class ReviewRequired(ValueError):
    """The narrowly supported geometry or validation contract was not met."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(text: str) -> str:
    return " ".join(text.split())


def anchored(text: str, anchor: dict) -> str:
    # Standalone default mirrors the package's validated reader. The native
    # adapter injects its canonical reader through render_document(text_reader=).
    parts, previous_end, previous_segment_end = [], None, None
    segments = anchor.get("textSegments", [])
    if not isinstance(segments, list):
        raise ReviewRequired("Text anchor segments must be a list")
    for segment in segments:
        try:
            values = (segment.get("startIndex", 0), segment["endIndex"])
            if any(type(value) is not int and not (isinstance(value, str) and value.isascii() and value.isdecimal()) for value in values):
                raise ValueError("Non-integer anchor index")
            start, end = map(int, values)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ReviewRequired("Invalid text anchor indices") from error
        if not 0 <= start <= end <= len(text) or (previous_segment_end is not None and start < previous_segment_end):
            raise ReviewRequired("Text anchor is out of bounds, overlapping, or reversed")
        previous_segment_end = end
        if start == end:
            continue
        if previous_end is not None and start > previous_end:
            gap = text[previous_end:start]
            parts.append(gap if not gap.strip() else "\n")
        parts.append(text[start:end])
        previous_end = end
    return "".join(parts)


def only_bidi_difference(expected: str, actual: str) -> bool:
    strip_rtl = lambda text: "".join(c for c in text if unicodedata.bidirectional(c) not in {"R", "AL", "AN"})
    return Counter(expected) == Counter(actual) and strip_rtl(expected) == strip_rtl(actual)


def source_image(page) -> tuple[bytes, Image.Image]:
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
    if degrees not in {0, 90, 180, 270}:
        raise ReviewRequired("Only explicit clockwise cardinal rotations are allowed")
    matrices = {
        0: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        90: [[0, 1, 0], [-1, 0, width], [0, 0, 1]],
        180: [[-1, 0, width], [0, -1, height], [0, 0, 1]],
        270: [[0, -1, height], [1, 0, 0], [0, 0, 1]],
    }
    return np.array(matrices[degrees], dtype=float), ((height, width) if degrees in {90, 270} else (width, height))


def google_rotation_matrix(google_page: dict) -> np.ndarray:
    """Decode only the unit-scale, orientation-preserving Google transform."""
    T = np.eye(3)
    transforms = google_page.get("transforms", [])
    if len(transforms) > 1:
        raise ReviewRequired("Multiple Google transforms are not supported")
    if transforms:
        item = transforms[0]
        if (item["rows"], item["cols"], item["type"]) != (2, 3, 6):
            raise ReviewRequired("Only 2x3 CV_64F Google transforms are supported")
        T[:2] = np.array(struct.unpack("<6d", base64.b64decode(item["data"], validate=True))).reshape(2, 3)
    if not np.isfinite(T).all() or not np.allclose(T[:2, :2].T @ T[:2, :2], np.eye(2), atol=1e-6, rtol=0) or abs(np.linalg.det(T[:2, :2]) - 1) > 1e-6:
        raise ReviewRequired("Google transform is not a unit-scale rotation")
    return T


def derive_cardinal_clockwise(google_page: dict) -> tuple[int, dict]:
    T = google_rotation_matrix(google_page)
    angle = math.degrees(math.atan2(T[1, 0], T[0, 0]))
    cardinal = int(round(angle / 90) * 90) % 360
    residual = (angle - cardinal + 180) % 360 - 180
    if abs(residual) > POLICY["maximumRotationResidualFromApprovedCardinalDegrees"]:
        raise ReviewRequired("Google rotation is outside the cardinal residual gate")
    return cardinal, {"decodedGoogleTransform": T.tolist(), "derivedCardinalClockwiseDegrees": cardinal,
                      "rotationResidualDegrees": residual, "maximumResidualDegrees": POLICY["maximumRotationResidualFromApprovedCardinalDegrees"]}


def validate_mapping(source_page, image: Image.Image, google_page: dict, clockwise: int) -> tuple[np.ndarray, dict]:
    width, height = float(source_page.mediabox.width), float(source_page.mediabox.height)
    returned = google_page["image"]
    returned_image = Image.open(io.BytesIO(base64.b64decode(returned["content"], validate=True)))
    if returned_image.size != (returned["width"], returned["height"]):
        raise ReviewRequired("Returned image dimensions do not match metadata")
    T = google_rotation_matrix(google_page)
    transforms = google_page.get("transforms", [])
    angle = math.degrees(math.atan2(T[1, 0], T[0, 0]))
    residual = (angle - clockwise + 180) % 360 - 180
    if abs(residual) > POLICY["maximumRotationResidualFromApprovedCardinalDegrees"]:
        raise ReviewRequired("Google rotation differs from the explicitly approved cardinal correction")
    scale = math.sqrt(4_000_000 / (width * height))
    iw, ih = math.floor(width * scale), math.floor(height * scale)
    if not transforms and (iw, ih) != returned_image.size:
        raise ReviewRequired("Observed image does not support the inferred 4MP PDF raster mapping")
    corners = np.array([[0, 0, 1], [iw, 0, 1], [iw, ih, 1], [0, ih, 1]])
    mapped_corners = (T @ corners.T).T[:, :2]
    minimum_error = float(np.max(np.abs(mapped_corners.min(axis=0))))
    edge_error = float(np.max(np.abs(mapped_corners.max(axis=0) - returned_image.size)))
    if minimum_error > POLICY["maximumTransformedSourceMinimumDifferencePixels"] or edge_error > POLICY["maximumTransformedCanvasEdgeDifferencePixels"]:
        raise ReviewRequired("Returned canvas does not support inferred raster geometry")
    H = T @ np.diag([iw / image.width, ih / image.height, 1])
    inverse = np.linalg.inv(H)
    predicted = image.convert("L").transform(returned_image.size, Image.Transform.AFFINE, tuple(inverse[:2].flatten()), Image.Resampling.BICUBIC, fillcolor=255)
    factor = min(1, 1000 / max(returned_image.size))
    compare_size = tuple(round(value * factor) for value in returned_image.size)
    a = np.asarray(predicted.resize(compare_size, Image.Resampling.LANCZOS), dtype=float)[4:-4, 4:-4]
    b = np.asarray(returned_image.convert("L").resize(compare_size, Image.Resampling.LANCZOS), dtype=float)[4:-4, 4:-4]
    correlation = float(np.corrcoef(a.ravel(), b.ravel())[0, 1])
    mae = float(np.mean(np.abs(a - b)))
    if not math.isfinite(correlation) or correlation < POLICY["minimumImageCorrelation"] or mae > POLICY["maximumMeanAbsoluteGrayDifferenceOutOf255"]:
        raise ReviewRequired("Actual source-to-returned-image registration failed; projection needs review")
    C, output_size = cardinal_matrix(clockwise, width, height)
    raster_to_source_pdf = np.array([[width / iw, 0, 0], [0, -height / ih, height], [0, 0, 1]])
    projection = C @ raster_to_source_pdf @ np.linalg.inv(T)
    evidence = {
        "originalImagePixels": list(image.size), "sourceMediaBoxPoints": [width, height],
        "returnedImagePixels": list(returned_image.size), "returnedImageActualFormat": returned_image.format,
        "inferredPreTransformRasterPixels": [iw, ih], "decodedGoogleTransform": T.tolist(),
        "googleImageToOutputPdfMatrix": projection.tolist(), "approvedCardinalClockwiseDegrees": clockwise,
        "retainedOriginalSkewDegrees": residual, "outputMediaBoxPoints": list(output_size),
        "transformedSourceMinimumErrorPixels": minimum_error, "transformedCanvasEdgeErrorPixels": edge_error,
        "registrationCorrelationAtMax1000Pixels": correlation, "registrationMeanAbsoluteGrayDifferenceOutOf255": mae,
        "mappingStatus": "Sample-specific inferred raster mapping passed declared geometry and image-registration gates; not a vendor guarantee",
    }
    return projection, evidence


def polygon(layout: dict, google_page: dict, projection: np.ndarray) -> list[list[float]]:
    box = layout["boundingPoly"]
    if "normalizedVertices" in box:
        w, h = google_page["image"]["width"], google_page["image"]["height"]
        points = [[v.get("x", 0) * w, v.get("y", 0) * h, 1] for v in box["normalizedVertices"]]
    else:
        points = [[v.get("x", 0), v.get("y", 0), 1] for v in box["vertices"]]
    if len(points) != 4:
        raise ReviewRequired("Line must have a four-corner polygon")
    return (projection @ np.array(points).T).T[:, :2].tolist()


def text_matrix(points: list, text: str) -> tuple[list[float], list[list[float]]]:
    p0, p1, p2, p3 = np.array(points)
    u, v = ((p1 - p0) + (p2 - p3)) / 2, ((p0 - p3) + (p1 - p2)) / 2
    lower_left = np.mean(points, axis=0) - (u + v) / 2
    advance = pdfmetrics.stringWidth(text.rstrip(), FONT_NAME, 1)
    ascent, descent = pdfmetrics.getAscentDescent(FONT_NAME, 1)
    if advance <= 0 or np.linalg.norm(u) <= 0 or np.linalg.norm(v) <= 0:
        raise ReviewRequired("Degenerate OCR line")
    a, b = u / advance
    c, d = v / (ascent - descent)
    e, f = lower_left - np.array([c, d]) * descent
    matrix = list(map(float, [a, b, c, d, e, f]))
    corners = [[a*x + c*y + e, b*x + d*y + f] for x, y in [(0, ascent), (advance, ascent), (advance, descent), (0, descent)]]
    return matrix, corners


def compose(cm, tm):
    a, b, c, d, e, f = tm
    A, B, C, D, E, F = cm
    return [A*a+C*b, B*a+D*b, A*c+C*d, B*c+D*d, A*e+C*f+E, B*e+D*f+F]


def thumbnail(source_pdf_path: Path, work_dir: Path) -> Path:
    """Local-only first-page thumbnail, without orientation inference or OCR."""
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
                    cardinal_clockwise: list[int], font_path: Path, prefix: str,
                    produce_archive: bool = True, allow_bidi_difference_source_pages: tuple[int, ...] = (),
                    text_reader=None) -> dict:
    """Return local archive_path, thumbnail_path and provenance; no network IO.

    cardinal_clockwise is indexed by SOURCE page. page_order is output-to-source.
    An explicitly accepted bidi difference is recorded, never silently rewritten.
    """
    original_bytes = source_pdf_path.read_bytes()
    read_text = text_reader or anchored
    source_sha = digest(original_bytes)
    reader = PdfReader(io.BytesIO(original_bytes))
    count = len(reader.pages)
    if sorted(page_order) != list(range(1, count + 1)) or len(page_order) != count or len(cardinal_clockwise) != count:
        raise ReviewRequired("Page order must preserve every source page exactly once")
    if len(document["pages"]) != count or [p["pageNumber"] for p in document["pages"]] != list(range(1, count + 1)):
        raise ReviewRequired("Response does not exactly match source page numbering/count")
    font = TTFont(FONT_NAME, str(font_path))
    pdfmetrics.registerFont(font)
    if any(not char.isspace() and ord(char) not in font.face.charToGlyph for char in document["text"]):
        raise ReviewRequired("Font does not cover all OCR characters")
    records = []
    for number, (page, gp, clockwise) in enumerate(zip(reader.pages, document["pages"], cardinal_clockwise), 1):
        image_bytes, image = source_image(page)
        projection, evidence = validate_mapping(page, image, gp, clockwise)
        lines = []
        for line in gp.get("lines", []):
            raw_text = read_text(document["text"], line["layout"]["textAnchor"])
            if not raw_text.strip():
                raise ReviewRequired("Whitespace-only line cannot be placed")
            text = re.sub(r"\s", " ", raw_text)
            points = polygon(line["layout"], gp, projection)
            matrix, fitted = text_matrix(points, text)
            affine_error = float(np.linalg.norm(np.array(fitted) - points, axis=1).max())
            outside = max(float(np.maximum(0, -np.array(points)).max()), float(np.maximum(0, np.array(points) - evidence["outputMediaBoxPoints"]).max()))
            if affine_error > POLICY["maximumAffineLineCornerErrorPoints"] or outside > POLICY["maximumLinePolygonOutsidePagePoints"]:
                raise ReviewRequired("Projected line does not fit supported geometry")
            lines.append({"text": text, "polygon": points, "matrix": matrix, "fittedPolygon": fitted, "affineErrorPoints": affine_error})
        expected = read_text(document["text"], gp["layout"]["textAnchor"])
        if normalized("".join(line["text"] for line in lines)) != normalized(expected):
            raise ReviewRequired("Line anchors do not cover complete page text")
        records.append({"sourcePage": number, "imageBytes": image_bytes, "expectedText": expected, "lines": lines, "evidence": evidence})
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive_path = out_dir / f"{prefix}.pdf"
    thumb_path = out_dir / f"{prefix}-thumbnail.webp"
    manifest_path = out_dir / f"{prefix}.manifest.json"
    if any(path.exists() for path in (archive_path, thumb_path, manifest_path)):
        raise FileExistsError("Output already exists; inspect before replacing")
    writer = PdfWriter()
    ordered = [records[n - 1] for n in page_order]
    for record in ordered:
        page = writer.add_page(reader.pages[record["sourcePage"] - 1])
        rotation = cardinal_clockwise[record["sourcePage"] - 1]
        size = record["evidence"]["outputMediaBoxPoints"]
        if rotation:
            # Exact cardinal coefficients matter. transfer_rotation_to_content()
            # produces tiny trig residuals, which changed Poppler resampling in
            # the inspected cards. This matches a source /Rotate-only reference.
            C, _ = cardinal_matrix(rotation, float(page.mediabox.width), float(page.mediabox.height))
            page.add_transformation(Transformation((C[0, 0], C[1, 0], C[0, 1], C[1, 1], C[0, 2], C[1, 2])))
            page.mediabox = RectangleObject([0, 0, *size])
            page.cropbox = RectangleObject([0, 0, *size])
        stream = io.BytesIO()
        overlay = canvas.Canvas(stream, pagesize=size, pageCompression=1, invariant=1)
        for line in record["lines"]:
            text = overlay.beginText()
            text.setTextRenderMode(3)
            text.setFont(FONT_NAME, 1)
            text.setTextTransform(*line["matrix"])
            text.textOut(line["text"])
            overlay.drawText(text)
        overlay.showPage()
        overlay.save()
        page.merge_page(PdfReader(io.BytesIO(stream.getvalue())).pages[0])
    buffer = io.BytesIO()
    writer.write(buffer)
    output_bytes = buffer.getvalue()
    reopened = PdfReader(io.BytesIO(output_bytes))
    checks = []
    for output_number, (page, record) in enumerate(zip(reopened.pages, ordered), 1):
        embedded = [obj.get_object() for obj in page["/Resources"]["/XObject"].values() if obj.get_object().get("/Subtype") == "/Image"]
        if len(embedded) != 1 or embedded[0].get("/Filter") != "/DCTDecode" or embedded[0]._data != record["imageBytes"]:
            raise ReviewRequired("Original JPEG stream was not preserved byte-for-byte")
        if page.rotation != 0 or not np.allclose([float(page.mediabox.width), float(page.mediabox.height)], record["evidence"]["outputMediaBoxPoints"], atol=1e-8, rtol=0):
            raise ReviewRequired("Output page orientation or size changed unexpectedly")
        captured = []
        def visit(text, cm, tm, font, size):
            if text.strip():
                matrix = compose(cm, tm)
                if captured and captured[-1]["matrix"] == matrix and captured[-1]["size"] == size:
                    captured[-1]["text"] += text
                else:
                    captured.append({"text": text, "matrix": matrix, "size": size})
        actual = normalized(page.extract_text(visitor_text=visit))
        expected = normalized(record["expectedText"])
        accepted_bidi = record["sourcePage"] in allow_bidi_difference_source_pages
        bidi_difference = actual != expected and accepted_bidi and only_bidi_difference(expected, actual)
        if actual != expected and not bidi_difference:
            raise ReviewRequired("Extracted page text changed")
        if len(captured) != len(record["lines"]):
            raise ReviewRequired("Line run count changed")
        max_delta = 0.0
        for run, line in zip(captured, record["lines"]):
            observed_text, intended_text = normalized(run["text"]), normalized(line["text"])
            if run["size"] != 1 or (observed_text != intended_text and not (accepted_bidi and only_bidi_difference(intended_text, observed_text))):
                raise ReviewRequired("Line content or font size changed")
            max_delta = max(max_delta, max(abs(a-b) for a, b in zip(run["matrix"], line["matrix"])))
        if max_delta > POLICY["maximumEncodedTextMatrixDifference"]:
            raise ReviewRequired("Encoded text matrix differs from projected position")
        modes = [int(args[0]) for args, op in page.get_contents().operations if op == b"Tr"]
        if not modes or any(mode != 3 for mode in modes):
            raise ReviewRequired("Text is not entirely invisible")
        checks.append({"outputPage": output_number, "sourcePage": record["sourcePage"], "originalJpegStreamByteIdentical": True,
                       "lineCount": len(record["lines"]), "maxEncodedTextMatrixDifference": max_delta,
                       "normalizedTextMatches": actual == expected, "explicitlyAcceptedBidiDifference": bidi_difference,
                       "expectedTextSha256": digest(expected.encode()), "extractedTextSha256": digest(actual.encode())})
    if digest(source_pdf_path.read_bytes()) != source_sha:
        raise ReviewRequired("Source PDF changed")
    if produce_archive:
        with archive_path.open("xb") as stream:
            stream.write(output_bytes)
        archive_path.chmod(0o600)
    first = ordered[0]
    thumb = Image.open(io.BytesIO(first["imageBytes"])).convert("RGB")
    rot = cardinal_clockwise[first["sourcePage"] - 1]
    transpose = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    if rot:
        thumb = thumb.transpose(transpose[rot])
    thumb.thumbnail((640, 640), Image.Resampling.LANCZOS)
    thumb.save(thumb_path, "WEBP", quality=90)
    thumb_path.chmod(0o600)
    provenance = {
        "status": "local_demonstrator_passed_automated_gates_pending_visual_review",
        "sourcePath": str(source_pdf_path), "sourceSha256": source_sha, "sourceByteCount": len(original_bytes),
        "archivePath": str(archive_path) if produce_archive else None,
        "archiveSha256": digest(output_bytes) if produce_archive else None, "archiveByteCount": len(output_bytes) if produce_archive else None,
        "thumbnailPath": str(thumb_path), "pageMappingOutputToSource": page_order,
        "cardinalClockwiseBySourcePage": cardinal_clockwise, "sourceUnchangedAfter": True,
        "policy": POLICY, "checks": checks,
        "pages": [{"outputPage": index, "sourcePage": record["sourcePage"], "originalJpegSha256": digest(record["imageBytes"]),
                   **record["evidence"], "lines": [{"textSha256": digest(line["text"].encode()), "polygon": line["polygon"], "fittedPolygon": line["fittedPolygon"],
                     "matrix": line["matrix"], "affineErrorPoints": line["affineErrorPoints"]} for line in record["lines"]]}
                  for index, record in enumerate(ordered, 1)],
        "limitations": [
            "Single full-page JPEG source pages only; all other source structures fail for review.",
            "4MP PDF raster scaling is an empirical sample fit checked against returned geometry and actual image registration; not a Google API guarantee.",
            "Global grayscale registration gates cannot prove every individual glyph or highlight is correct; visual field/edge checks remain necessary.",
            "Only explicit page ordering and cardinal rotations were applied. Minor original skew is retained.",
            "Original JPEG bitstreams are preserved; visible rendering still performs ordinary PDF raster sampling.",
            "Invisible text is placed at line level; within-line word selection is approximate, not token-accurate.",
            "OCR errors are retained, including explicitly recorded bidi extraction limitations where authorized.",
            "No live Paperless or cloud writes were performed. This is not a general reliability proof.",
        ],
    }
    with manifest_path.open("x") as stream:
        json.dump(provenance, stream, indent=2)
        stream.write("\n")
    manifest_path.chmod(0o600)
    return {"archive_path": archive_path if produce_archive else None, "thumbnail_path": thumb_path,
            "manifest_path": manifest_path, "provenance": provenance}
