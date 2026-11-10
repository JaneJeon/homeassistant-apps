"""Generated synthetic fixtures only; no personal documents or copied OCR text."""

import io
import json
from pathlib import Path

from pypdf import PdfWriter

from paperless_intake.docai.types import OCRChunk, ProcessingResult, RenderedResult
from paperless_intake.docai.validation import sha256


PROCESSOR = "projects/paperlss-document-pipeline/locations/us/processors/synthetic/processorVersions/synthetic-pinned"


def make_pdf(page_count: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def make_result(page_count: int = 2) -> ProcessingResult:
    chunks = []
    for page_number in range(1, page_count + 1):
        text = f"Synthetic page {page_number}: café example.\n"
        doc = {"text": text, "pages": [{"pageNumber": page_number, "layout": {"textAnchor": {"textSegments": [{"endIndex": str(len(text))}]}},
                                       "tokens": [{"layout": {"textAnchor": {"textSegments": [{"startIndex": "0", "endIndex": "9"}]}}}]}]}
        raw = json.dumps({"document": doc}, ensure_ascii=False).encode()
        chunks.append(OCRChunk((page_number,), doc, PROCESSOR, sha256(raw), {"synthetic": True}))
    return ProcessingResult(tuple(chunks), {"synthetic": True, "network_requests": 0})


class FixtureBackend:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def process(self, source, work_dir):
        self.calls.append(source)
        return self.result or make_result(source.page_count)


def thumbnail_file(work_dir: Path) -> Path:
    from PIL import Image
    path = work_dir / "synthetic-thumbnail.webp"
    Image.new("RGB", (16, 16), "white").save(path, format="WEBP")
    return path


class FixtureRenderer:
    def __init__(self, order=None):
        self.order = order
        self.calls = []

    def render(self, source, result, work_dir, *, produce_archive):
        self.calls.append(source)
        archive = None
        if produce_archive:
            archive = work_dir / "synthetic-archive.pdf"
            archive.write_bytes(source.data)
        return RenderedResult(archive, thumbnail_file(work_dir), {"synthetic_renderer": True}, self.order)

    def thumbnail(self, source, work_dir):
        self.calls.append(source)
        return thumbnail_file(work_dir)
