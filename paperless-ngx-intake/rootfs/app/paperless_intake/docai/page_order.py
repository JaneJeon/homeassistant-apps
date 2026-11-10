"""Conservative, pure English-v1 pagination planner.

Recognizes Page/Pg n [of m], or a standalone n of m next to a Page/Pg label.
The complete marker must lie in the top or bottom 20% of the Google page, whose
page layout must report PAGE_UP when evidence is used. Nearby means an
above/left label, at least 50% cross-axis overlap and no
more than three text-heights of separation. Slash forms are unsupported.

Only complete, unambiguous, sufficiently confident 1..N series can produce a
permutation. All other pages are retained in source order. Numeric confidence
is a conservative gate, not a calibrated probability of correctness.
"""
from __future__ import annotations

from collections import defaultdict
import copy
from dataclasses import asdict, dataclass
import math
import re

from .errors import ResultValidationError
from .validation import anchored_text

_INLINE = re.compile(r"\b(?:page|pg)\.?\s*:?[ \t]*(?P<number>[0-9]+)(?:\s+of\s+(?P<total>[0-9]+))?(?![\w])", re.I)
_BARE_OF = re.compile(r"\s*(?P<number>[0-9]+)\s+of\s+(?P<total>[0-9]+)\s*", re.I)
_LABEL = re.compile(r"\s*(?:page|pg)\.?\s*:?\s*", re.I)


@dataclass(frozen=True)
class OrderPlan:
    """mapping is a complete output-page -> one-based source-page permutation."""
    mapping: tuple[int, ...]
    status: str
    evidence: tuple[dict, ...]
    reason: str

    def to_dict(self) -> dict:
        result = asdict(self)
        result["mapping"] = list(self.mapping)
        return result


def _text_and_indices(text: str, anchor: dict) -> tuple[str, list[int | None]]:
    value = anchored_text(text, anchor)  # Shared validation and span-boundary policy.
    indices, previous = [], None
    for segment in anchor.get("textSegments", []):
        start, end = int(segment.get("startIndex", 0)), int(segment["endIndex"])
        if start == end:
            continue
        if previous is not None and start > previous:
            gap = text[previous:start]
            indices.extend(range(previous, start) if not gap.strip() else [None])
        indices.extend(range(start, end))
        previous = end
    if len(value) != len(indices):
        raise ResultValidationError("Pagination anchor index map disagrees with canonical text.")
    return value, indices


def _box(layout: dict, page: dict) -> tuple[float, float, float, float] | None:
    polygon = layout.get("boundingPoly", {})
    vertices = polygon.get("normalizedVertices")
    if vertices is None:
        vertices = polygon.get("vertices")
        image = page.get("image", {})
        dimension = page.get("dimension", {})
        width, height = image.get("width"), image.get("height")
        if width is None and dimension.get("unit") == "pixels":
            width, height = dimension.get("width"), dimension.get("height")
        if not vertices or not isinstance(width, (int, float)) or not isinstance(height, (int, float)) or width <= 0 or height <= 0:
            return None
        vertices = [{"x": v.get("x", 0) / width, "y": v.get("y", 0) / height} for v in vertices]
    if not isinstance(vertices, list) or len(vertices) != 4:
        return None
    try:
        xs, ys = [float(v.get("x", 0)) for v in vertices], [float(v.get("y", 0)) for v in vertices]
    except (AttributeError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in xs + ys):
        return None
    box = min(xs), min(ys), max(xs), max(ys)
    return box if box[2] > box[0] and box[3] > box[1] else None


def _band(box) -> str | None:
    if box is None:
        return None
    if box[3] <= 0.2:
        return "top"
    if box[1] >= 0.8:
        return "bottom"
    return None


def _union(boxes):
    boxes = [box for box in boxes if box is not None]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None


def _confidence(value) -> float | None:
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


def _node(value: dict, page: dict, text: str, index: int) -> dict:
    layout = value.get("layout", {})
    content, indices = _text_and_indices(text, layout.get("textAnchor", {}))
    return {"text": content, "indices": indices, "index": index, "box": _box(layout, page),
            "confidence": _confidence(layout.get("confidence")), "orientation": layout.get("orientation"),
            "boundingPoly": copy.deepcopy(layout.get("boundingPoly", {})),
            "textAnchor": copy.deepcopy(layout.get("textAnchor", {}))}


def _nearby(label: dict, number_line: dict, page: dict) -> bool:
    a, b = label["box"], number_line["box"]
    if not a or not b or _band(a) != _band(b):
        return False
    image = page.get("image", {})
    dimension = page.get("dimension", {})
    width = image.get("width", dimension.get("width"))
    height = image.get("height", dimension.get("height"))
    if not isinstance(width, (float, int)) or not isinstance(height, (float, int)) or width <= 0 or height <= 0:
        return False  # Cannot compare physical text-height distances from normalized x/y alone.
    ax0, ay0, ax1, ay1 = a[0]*width, a[1]*height, a[2]*width, a[3]*height
    bx0, by0, bx1, by1 = b[0]*width, b[1]*height, b[2]*width, b[3]*height
    text_height = max(ay1-ay0, by1-by0)
    overlap_x = max(0, min(ax1, bx1)-max(ax0, bx0))
    overlap_y = max(0, min(ay1, by1)-max(ay0, by0))
    above = overlap_x >= 0.5*min(ax1-ax0, bx1-bx0) and -0.25*text_height <= by0-ay1 <= 3*text_height
    left = overlap_y >= 0.5*min(ay1-ay0, by1-by0) and -0.25*text_height <= bx0-ax1 <= 3*text_height
    return above or left


def _numeric_tokens(node: dict, match, tokens: list[dict]) -> tuple[list[dict], float | None]:
    evidence, all_confidences = [], []
    for role in ("number", "total"):
        if match.groupdict().get(role) is None:
            continue
        start, end = match.span(role)
        wanted = {index for index in node["indices"][start:end] if index is not None}
        covered = set()
        for token in tokens:
            overlap = wanted.intersection(index for index in token["indices"] if index is not None)
            if overlap:
                covered.update(overlap)
                evidence.append({"role": role, "tokenIndex": token["index"], "text": token["text"],
                                 "normalizedBox": token["box"], "confidence": token["confidence"],
                                 "boundingPoly": token["boundingPoly"], "textAnchor": token["textAnchor"],
                                 "sourceCharacterIndices": sorted(overlap)})
                all_confidences.append(token["confidence"] if token["box"] is not None else None)
        if not wanted or covered != wanted:
            all_confidences.append(None)
    minimum = min(all_confidences) if all_confidences and all(value is not None for value in all_confidences) else None
    return evidence, minimum


def _marker(source_page: int, node: dict, match, tokens: list[dict], labels: list[dict]) -> dict | None:
    number_tokens, confidence = _numeric_tokens(node, match, tokens)
    matched_indices = {i for i in node["indices"][match.start():match.end()] if i is not None}
    matching = [token for token in tokens if matched_indices.intersection(i for i in token["indices"] if i is not None)]
    marker_box = _union([token["box"] for token in matching]) or node["box"]
    marker_box = _union([marker_box] + [label["box"] for label in labels])
    band = _band(marker_box)
    if band is None:
        return None
    return {"kind": "marker", "sourcePage": source_page, "band": band,
            "number": int(match.group("number")), "total": int(match.group("total")) if match.groupdict().get("total") is not None else None,
            "text": match.group(0), "normalizedBox": marker_box, "lineIndex": node["index"],
            "lineBoundingPoly": node["boundingPoly"], "lineTextAnchor": node["textAnchor"],
            "numberTokenConfidenceMinimum": confidence, "numericTokens": number_tokens,
            "nearbyLabels": [{"text": label["text"], "lineIndex": label["index"], "normalizedBox": label["box"], "confidence": label["confidence"],
                              "boundingPoly": label["boundingPoly"], "textAnchor": label["textAnchor"]} for label in labels]}


def _page_markers(page: dict, source_page: int, text: str) -> tuple[list[dict], list[dict]]:
    nodes = [_node(line, page, text, index) for index, line in enumerate(page.get("lines", []))]
    tokens = [_node(token, page, text, index) for index, token in enumerate(page.get("tokens", []))]
    nodes = [node for node in nodes if node["orientation"] in {None, "PAGE_UP", "ORIENTATION_UNSPECIFIED"}]
    labels = [node for node in nodes if _LABEL.fullmatch(node["text"]) and _band(node["box"])]
    found, issues = [], []
    for node in nodes:
        if not _band(node["box"]):
            continue
        for match in _INLINE.finditer(node["text"]):
            if re.match(r"\s*(?:of\b|[/.-]\s*[0-9])", node["text"][match.end():], re.I):
                issues.append({"kind": "unsupported_marker", "sourcePage": source_page, "lineIndex": node["index"],
                               "reason": "An explicit Page/Pg marker has an unsupported or incomplete denominator/date-like suffix."})
                continue
            marker = _marker(source_page, node, match, tokens, [])
            if marker:
                found.append(marker)
        match = _BARE_OF.fullmatch(node["text"])
        if match:
            nearby = [label for label in labels if _nearby(label, node, page)]
            if nearby:
                marker = _marker(source_page, node, match, tokens, nearby)
                if marker:
                    found.append(marker)
    return found, issues


def plan_page_order(document: dict, *, min_number_token_confidence: float = 0.90) -> OrderPlan:
    """Plan printed-pagination order without IO or modifying the input Document."""
    if type(min_number_token_confidence) not in {int, float} or not math.isfinite(min_number_token_confidence) or not 0.90 <= min_number_token_confidence <= 1:
        raise ValueError("The declared numeric-confidence threshold may be tightened from 0.90 to 1, not weakened")
    pages = document.get("pages")
    if not isinstance(pages, list):
        raise ValueError("Document.pages must be a list")
    identity = tuple(range(1, len(pages) + 1))
    if not pages:
        return OrderPlan(identity, "needs_review", (), "There are no input pages to plan.")
    if not isinstance(document.get("text", ""), str) or any(not isinstance(page, dict) for page in pages):
        return OrderPlan(identity, "needs_review", (), "Invalid OCR document structure.")
    if [page.get("pageNumber") for page in pages] != list(identity) or any(type(page.get("pageNumber")) is not int for page in pages):
        return OrderPlan(identity, "needs_review", (), "Input pages must have complete source page numbers 1..N in source order.")
    markers, issues = [], []
    try:
        for source_page, page in enumerate(pages, 1):
            observed, page_issues = _page_markers(page, source_page, document.get("text", ""))
            markers.extend(observed)
            issues.extend(page_issues)
            if observed and page.get("layout", {}).get("orientation") != "PAGE_UP":
                issues.append({"kind": "page_frame_issue", "sourcePage": source_page,
                               "reason": "The page layout does not establish an upright PAGE_UP frame for outer-band pagination."})
    except (ResultValidationError, ValueError, TypeError, KeyError, AttributeError) as error:
        return OrderPlan(identity, "needs_review", tuple(markers), f"Invalid OCR pagination evidence: {error}")
    if not markers and not issues:
        return OrderPlan(identity, "unknown_no_pagination_evidence", (), "No supported pagination markers were found in the outer page bands; existing order is unverified.")
    groups = defaultdict(list)
    for marker in markers:
        groups[(marker["band"], marker["total"])].append(marker)
    series = []
    count = len(pages)
    for (band, total), group in sorted(groups.items(), key=lambda item: (item[0][0], -1 if item[0][1] is None else item[0][1])):
        by_page = defaultdict(set)
        for marker in group:
            by_page[marker["sourcePage"]].add(marker["number"])
        ambiguous = any(len(numbers) != 1 for numbers in by_page.values())
        pairs = [(page, next(iter(numbers))) for page, numbers in sorted(by_page.items()) if len(numbers) == 1]
        numbers = [number for _, number in pairs]
        confidence_ok = all(marker["numberTokenConfidenceMinimum"] is not None and marker["numberTokenConfidenceMinimum"] >= min_number_token_confidence for marker in group)
        invalid = any(number < 1 or (total is not None and (total < 1 or number > total)) for _, number in pairs)
        duplicate = len(numbers) != len(set(numbers))
        full = not ambiguous and not invalid and not duplicate and len(pairs) == count and sorted(numbers) == list(identity) and (total is None or total == count)
        inner_complete = total is not None and 0 < total < count and not ambiguous and not invalid and not duplicate and len(pairs) == total and sorted(numbers) == list(range(1, total + 1))
        entry = {"kind": "series", "band": band, "total": total, "sourcePageNumberPairs": [list(pair) for pair in pairs],
                 "sourcePagesWithoutThisSeries": [page for page in identity if page not in by_page],
                 "oneUnambiguousNumberPerObservedPage": not ambiguous, "duplicateNumbersAcrossPages": duplicate,
                 "numericValuesValid": not invalid, "numericConfidenceThreshold": min_number_token_confidence,
                 "allNumericTokensPassThreshold": confidence_ok, "completeFullDocumentSeries": full,
                 "completeInnerDistinctDenominatorSeries": inner_complete}
        series.append(entry)
        if ambiguous or invalid or duplicate or not confidence_ok:
            issues.append({"kind": "series_issue", "band": band, "total": total,
                           "reason": "Ambiguous, duplicate, invalid, missing-confidence or below-threshold numeric pagination evidence."})
    evidence = tuple(markers + series + issues)
    if issues:
        return OrderPlan(identity, "needs_review", evidence, "Pagination evidence is ambiguous, unsupported or below the declared numeric-confidence gate; no pages were moved.")
    full = [group for group in series if group["completeFullDocumentSeries"]]
    if full:
        permutations = {tuple(page for page, number in sorted(group["sourcePageNumberPairs"], key=lambda pair: pair[1])) for group in full}
        if len(permutations) != 1:
            return OrderPlan(identity, "needs_review", evidence, "Complete pagination series disagree about page order.")
        proposal = next(iter(permutations))
        positions = {source_page: output_page for output_page, source_page in enumerate(proposal)}
        for group in series:
            ordered = [number for page, number in sorted(group["sourcePageNumberPairs"], key=lambda pair: positions[pair[0]])]
            if ordered != sorted(ordered):
                return OrderPlan(identity, "needs_review", evidence, "A secondary pagination series conflicts with the proposed full-document order; no pages were moved.")
        if sorted(proposal) != list(identity):
            raise AssertionError("Planner produced a non-bijective mapping")
        return OrderPlan(proposal, "complete_numbering_in_order" if proposal == identity else "reorder_from_complete_numbering", evidence,
                         "One consistent complete 1..N pagination order passes the numeric-confidence gate; all observed secondary series agree. This verifies printed pagination only.")
    for group in series:
        numbers = [number for _, number in group["sourcePageNumberPairs"]]
        if numbers != sorted(numbers):
            return OrderPlan(identity, "needs_review", evidence, "Partial pagination is out of source order; placement of unnumbered or missing pages is unresolved, so no pages were moved.")
    return OrderPlan(identity, "partial_in_order", evidence,
                     "Observed partial series already ascend in source order. Unnumbered pages remain exactly where they are; complete document order is unverified.")
