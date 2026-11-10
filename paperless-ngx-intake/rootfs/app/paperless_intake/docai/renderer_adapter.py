"""Thin adapter for the separate, independently reviewed source-raster renderer."""

from pathlib import Path

from .types import ProcessingResult, RenderedResult, SourcePDF
from .validation import anchored_text, document_for_renderer, validate_page_order
from .page_order import plan_page_order
from . import __version__


class SourcePreservedRenderer:
    def __init__(self, core_module, *, page_order: tuple[int, ...],
                 cardinal_clockwise: tuple[int, ...], font_path: Path,
                 allow_bidi_difference_source_pages: tuple[int, ...] = ()):
        self.core = core_module
        self.page_order = tuple(page_order)
        self.cardinals = tuple(cardinal_clockwise)
        self.font_path = font_path
        self.allowed_bidi = tuple(allow_bidi_difference_source_pages)

    def render(self, source: SourcePDF, result: ProcessingResult, work_dir: Path,
               *, produce_archive: bool) -> RenderedResult:
        validate_page_order(self.page_order, source.page_count)
        if len(self.cardinals) != source.page_count or any(angle not in {0, 90, 180, 270} for angle in self.cardinals):
            raise ValueError("Renderer requires one explicit cardinal angle per source page.")
        document = document_for_renderer(result, source)
        rendered = self.core.render_document(source.path, document, work_dir,
                                             page_order=list(self.page_order), cardinal_clockwise=list(self.cardinals),
                                             font_path=self.font_path, prefix="document-ai", produce_archive=produce_archive,
                                             allow_bidi_difference_source_pages=self.allowed_bidi, text_reader=anchored_text)
        return RenderedResult(archive_path=Path(rendered["archive_path"]) if rendered.get("archive_path") else None,
                              thumbnail_path=Path(rendered["thumbnail_path"]), provenance=rendered.get("provenance", {}),
                              page_order=self.page_order)

    def thumbnail(self, source: SourcePDF, work_dir: Path) -> Path:
        return Path(self.core.thumbnail(source.path, work_dir))


class AutomaticSourcePreservedRenderer:
    """Derive mechanical decisions, then run the existing registration gates."""
    def __init__(self, *, font_path: Path, allow_bidi_extraction_reordering: bool = False):
        from . import pdf_renderer
        self.core = pdf_renderer
        self.font_path = Path(font_path)
        self.allow_bidi = allow_bidi_extraction_reordering

    def render(self, source: SourcePDF, result: ProcessingResult, work_dir: Path,
               *, produce_archive: bool) -> RenderedResult:
        document = document_for_renderer(result, source)
        plan = plan_page_order(document)
        if plan.status == "needs_review":
            raise self.core.ReviewRequired("Page order needs review: " + plan.reason)
        validate_page_order(plan.mapping, source.page_count)
        decisions = [self.core.derive_cardinal_clockwise(page) for page in document["pages"]]
        cardinals = tuple(angle for angle, evidence in decisions)
        adapter = SourcePreservedRenderer(self.core, page_order=plan.mapping, cardinal_clockwise=cardinals,
                                         font_path=self.font_path,
                                         allow_bidi_difference_source_pages=tuple(range(1, source.page_count + 1)) if self.allow_bidi else ())
        rendered = adapter.render(source, result, work_dir, produce_archive=produce_archive)
        provenance = dict(rendered.provenance)
        provenance.update(module="paperless_intake.docai.pdf_renderer", module_version=__version__,
                          decision_status="mechanical_decisions_passed_registration_gates",
                          page_order_decision=plan.to_dict(),
                          logical_order_verified=plan.status in {"complete_numbering_in_order", "reorder_from_complete_numbering"},
                          orientation_decisions=[{"sourcePage": i, **evidence} for i, (_, evidence) in enumerate(decisions, 1)],
                          bidi_extraction_reordering_policy=self.allow_bidi)
        # The durable parser sink receives this evidence before getters publish.
        return RenderedResult(rendered.archive_path, rendered.thumbnail_path, provenance, plan.mapping)

    def thumbnail(self, source: SourcePDF, work_dir: Path) -> Path:
        return Path(self.core.thumbnail(source.path, work_dir))
