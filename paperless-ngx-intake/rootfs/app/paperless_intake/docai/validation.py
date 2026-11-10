"""Validate complete page coverage and independently scoped Google text anchors."""

from __future__ import annotations

import copy
import hashlib
import io
from pathlib import Path

from pypdf import PdfReader

from .errors import IntegrityError, ResultValidationError
from .types import ProcessingResult, SourcePDF


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inspect_source(path: Path) -> SourcePDF:
    path = path.resolve(strict=True)
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ResultValidationError("The selected parser accepts PDF input only.")
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ResultValidationError("Encrypted PDF input is not supported.")
        count = len(reader.pages)
    except ResultValidationError:
        raise
    except Exception as error:
        raise ResultValidationError("Source PDF could not be inspected.") from error
    if count < 1:
        raise ResultValidationError("Source PDF has no pages.")
    return SourcePDF(path=path, data=data, sha256=sha256(data), page_count=count)


def assert_unchanged(source: SourcePDF) -> None:
    try:
        current = sha256(source.path.read_bytes())
    except OSError as error:
        raise IntegrityError("Source PDF became unreadable during processing.") from error
    if current != source.sha256:
        raise IntegrityError("Source PDF changed during processing; result rejected.")


def validate_page_order(order: tuple[int, ...], page_count: int) -> None:
    if any(type(number) is not int for number in order) or sorted(order) != list(range(1, page_count + 1)):
        raise ResultValidationError("Page mapping must contain every source page exactly once.")


def _segments(anchor: dict, text_length: int):
    segments = anchor.get("textSegments", [])
    if not isinstance(segments, list):
        raise ResultValidationError("Text anchor segments must be a list.")
    previous_end = None
    for segment in segments:
        try:
            if not isinstance(segment, dict):
                raise ValueError("Segment is not an object")
            indices = (segment.get("startIndex", 0), segment["endIndex"])
            for value in indices:
                if type(value) is not int and not (isinstance(value, str) and value.isascii() and value.isdecimal()):
                    raise ValueError("Index is not an integer")
            start, end = (int(value) for value in indices)
        except (TypeError, KeyError, ValueError) as error:
            raise ResultValidationError("Invalid Google text anchor indices.") from error
        if not 0 <= start <= end <= text_length:
            raise ResultValidationError("Text anchor lies outside its own chunk text.")
        if previous_end is not None and start < previous_end:
            raise ResultValidationError("Text anchor segments overlap or run in reverse order.")
        previous_end = end
        yield segment, start, end


def _validate_anchors(value, text_length: int) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "textAnchor":
                if not isinstance(child, dict):
                    raise ResultValidationError("Text anchor must be an object.")
                list(_segments(child, text_length))
            _validate_anchors(child, text_length)
    elif isinstance(value, list):
        for child in value:
            _validate_anchors(child, text_length)


def anchored_text(text: str, anchor: dict) -> str:
    parts = []
    previous_end = None
    for _, start, end in _segments(anchor, len(text)):
        if start == end:
            continue
        if previous_end is not None and start > previous_end:
            gap = text[previous_end:start]
            # Preserve source whitespace between spans. When another span owns
            # meaningful intervening content, show a break rather than invent a word.
            parts.append(gap if not gap.strip() else "\n")
        parts.append(text[start:end])
        previous_end = end
    return "".join(parts)


def validate_chunk_document(document: dict, source_pages: tuple[int, ...]) -> None:
    """One policy shared by live/cache backends before any anchor rebasing.

    Global selector page numbers must match the explicit source mapping. Page
    anchors must cover all meaningful chunk text exactly once and in order.
    Whitespace gaps are allowed and retained by anchored_text within a page.
    """
    if not isinstance(document, dict) or not isinstance(document.get("text", ""), str):
        raise ResultValidationError("Chunk document has invalid text.")
    pages = document.get("pages")
    if not isinstance(pages, list) or not pages or len(pages) != len(source_pages):
        raise ResultValidationError("Chunk page count does not match its source mapping.")
    if any(type(number) is not int or number < 1 for number in source_pages) or len(set(source_pages)) != len(source_pages):
        raise ResultValidationError("Chunk source mapping has invalid or duplicate page numbers.")
    if any(not isinstance(page, dict) or type(page.get("pageNumber")) is not int for page in pages):
        raise ResultValidationError("Chunk pages require integer global page numbers.")
    if tuple(page["pageNumber"] for page in pages) != tuple(source_pages):
        raise ResultValidationError("Response page numbers do not match the explicit global source mapping.")
    error = document.get("error", {})
    if not isinstance(error, dict) or error.get("code", 0):
        raise ResultValidationError("Google document reports a processing error.")
    text = document.get("text", "")
    if "\0" in text:
        raise ResultValidationError("OCR text contains a NUL character.")
    _validate_anchors(document, len(text))
    previous_end = 0
    for page in pages:
        layout = page.get("layout", {})
        if not isinstance(layout, dict) or not isinstance(layout.get("textAnchor", {}), dict):
            raise ResultValidationError("Page layout has invalid text anchors.")
        for _, start, end in _segments(layout.get("textAnchor", {}), len(text)):
            if start < previous_end:
                raise ResultValidationError("Page text anchors overlap or run out of page order.")
            if text[previous_end:start].strip():
                raise ResultValidationError("Page text anchors omit non-whitespace chunk text.")
            previous_end = end
    if text[previous_end:].strip():
        raise ResultValidationError("Page text anchors omit non-whitespace chunk text.")


def validate_result(result: ProcessingResult, source: SourcePDF) -> None:
    if not isinstance(result, ProcessingResult) or not result.chunks:
        raise ResultValidationError("Backend did not return OCR chunks.")
    covered = []
    for chunk in result.chunks:
        document = chunk.document
        validate_chunk_document(document, chunk.source_pages)
        if not chunk.processor_version or len(chunk.response_sha256) != 64:
            raise ResultValidationError("Chunk provenance lacks pinned version or response digest.")
        try:
            int(chunk.response_sha256, 16)
        except ValueError as error:
            raise ResultValidationError("Chunk response digest is not SHA-256.") from error
        covered.extend(chunk.source_pages)
    validate_page_order(tuple(covered), source.page_count)


def _rebase_anchors(value, offset: int) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "textAnchor":
                for segment in child.get("textSegments", []):
                    segment["startIndex"] = str(int(segment.get("startIndex", 0)) + offset)
                    segment["endIndex"] = str(int(segment["endIndex"]) + offset)
            _rebase_anchors(child, offset)
    elif isinstance(value, list):
        for child in value:
            _rebase_anchors(child, offset)


def document_for_renderer(result: ProcessingResult, source: SourcePDF) -> dict:
    """Create a checked derivative document; never modify raw chunk responses."""
    validate_result(result, source)
    text_parts = []
    pages_by_source = {}
    offset = 0
    for chunk in result.chunks:
        chunk_text = chunk.document.get("text", "")
        for source_page, page in zip(chunk.source_pages, chunk.document["pages"]):
            page_copy = copy.deepcopy(page)
            _rebase_anchors(page_copy, offset)
            page_copy["pageNumber"] = source_page
            pages_by_source[source_page] = page_copy
        text_parts.append(chunk_text)
        offset += len(chunk_text)
    return {"text": "".join(text_parts), "pages": [pages_by_source[number] for number in range(1, source.page_count + 1)]}


def ordered_text(result: ProcessingResult, source: SourcePDF, page_order: tuple[int, ...]) -> str:
    validate_page_order(page_order, source.page_count)
    combined = document_for_renderer(result, source)
    return "\n\n".join(
        anchored_text(combined["text"], combined["pages"][number - 1].get("layout", {}).get("textAnchor", {}))
        for number in page_order
    )


def private_output(path: Path, work_dir: Path) -> Path:
    try:
        path = path.resolve(strict=True)
        root = work_dir.resolve(strict=True)
    except OSError as error:
        raise ResultValidationError("Renderer output does not exist.") from error
    if not path.is_relative_to(root) or not path.is_file():
        raise ResultValidationError("Renderer output escaped the private work directory.")
    path.chmod(0o600)
    return path
