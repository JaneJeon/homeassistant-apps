"""Join Google's result to the renderer: read the pages, decide turn and order for the stack, render."""

from pathlib import Path

from . import pdf_renderer, stack
from .page_order import OrderPlan, plan_page_order
from .types import ProcessingResult, RenderedResult, SourcePDF
from .validation import anchored_text, document_for_renderer, validate_page_order


class StackRenderer:
    """Builds the searchable copy for one scan.

    The turn and the order are decided for the stack as a whole (see stack.py).
    Neither decision can stop a scan. pdf_renderer.ReviewRequired is raised only
    when the copy can't be built safely at all.
    """

    def decide(self, document: dict, plan: OrderPlan | None = None) -> tuple[dict, list[int], list[dict]]:
        plan = plan or plan_page_order(document)
        order = stack.decide_order(plan, len(document["pages"]))
        readings = [pdf_renderer.read_rotation(page) for page in document["pages"]]
        turns, rotation_decisions = stack.decide_rotations(readings)
        return order, turns, rotation_decisions

    def render(self, source: SourcePDF, result: ProcessingResult, work_dir: Path, *,
               plan: OrderPlan | None = None) -> RenderedResult:
        document = document_for_renderer(result, source)
        order, turns, rotation_decisions = self.decide(document, plan)
        mapping = tuple(order["mapping"])
        validate_page_order(mapping, source.page_count)
        rendered = pdf_renderer.render_document(source.path, document, work_dir, page_order=list(mapping),
                                                cardinal_clockwise=turns, prefix="document-ai",
                                                text_reader=anchored_text)
        provenance = dict(rendered["provenance"])
        provenance.update(page_order_decision=order, orientation_decisions=rotation_decisions,
                          quality_notes=pdf_renderer.quality_notes(rendered["provenance"]))
        return RenderedResult(Path(rendered["archive_path"]), None, provenance, mapping)
