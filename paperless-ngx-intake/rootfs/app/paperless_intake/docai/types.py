"""The small processing/renderer boundary; each OCR chunk owns its text anchors."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SourcePDF:
    path: Path
    data: bytes
    sha256: str
    page_count: int

    @property
    def byte_count(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class OCRChunk:
    source_pages: tuple[int, ...]
    document: dict
    processor_version: str
    response_sha256: str
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessingResult:
    chunks: tuple[OCRChunk, ...]
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RenderedResult:
    archive_path: Path | None
    thumbnail_path: Path
    provenance: dict = field(default_factory=dict)
    page_order: tuple[int, ...] | None = None


class Backend(Protocol):
    def process(self, source: SourcePDF, work_dir: Path) -> ProcessingResult: ...


class Renderer(Protocol):
    def render(self, source: SourcePDF, result: ProcessingResult, work_dir: Path,
               *, produce_archive: bool) -> RenderedResult: ...

    def thumbnail(self, source: SourcePDF, work_dir: Path) -> Path: ...


class ProvenanceSink(Protocol):
    def persist(self, provenance: dict, *, work_dir: Path) -> Path: ...
