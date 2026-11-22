"""Decisions that belong to the whole stack, not to one page.

A mail piece goes into the feeder as one stack that stays together. It can go
in four ways: correctly, turned head to foot, flipped over, or both.

- Turned head to foot: same page order, every image upside down.
- Flipped over: the last sheet feeds first and the scanner sees each back
  before its front, so the whole page sequence is reversed, backs included.
  Three double-sided sheets come out 6 5 4 3 2 1, not 5 6 3 4 1 2. The scanner
  dropping blank sides changes nothing here: dropping blanks and reversing give
  the same pages in either order.

Seen in Jane's own scans before this was written: documents 19 (six pages),
35 and 37 (two pages each) were each exactly reversed. None showed sheets
reversed with their two sides kept in order.

So one stack carries two yes-or-no questions, "is it turned?" and "is it
flipped?", and every page is evidence for both. One answer per stack is far
more reliable than one guess per page, because a page with little text says
little, and the other pages can speak for it.

Nothing here can lose a page. A wrong answer leaves a page upside down or the
pages in scan order, with every image still present, so these decisions never
hold a scan. What was decided, and on what evidence, is recorded.
"""

from __future__ import annotations

from .page_order import OrderPlan

# A page with fewer words than this follows the stack's rotation when the two disagree.
STRONG_WORDS = 12
# The stack "has" a rotation when this share of its pages with plenty of text agree.
STACK_AGREEMENT = 0.8

FULL_ORDER = {"complete_numbering_in_order", "reorder_from_complete_numbering"}


def decide_rotations(readings: list[dict]) -> tuple[list[int], list[dict]]:
    """Quarter turns (clockwise) that put each source page upright.

    `readings` holds, per source page, Google's own reading of that page:
    {"cardinal": 0|90|180|270, "words": n, ...}. A page with plenty of text
    keeps Google's reading. That covers the sheet printed with its back upside
    down, and the odd sideways page. A page with little text follows the stack
    when the stack is clear about head-to-foot.
    """
    # One vote per page with plenty of text. Counting words instead would let a single dense
    # page, such as terms printed upside down on the back of a bill, speak for the whole stack.
    votes = {0: 0, 180: 0}
    for reading in readings:
        if reading["cardinal"] in votes and reading["words"] >= STRONG_WORDS:
            votes[reading["cardinal"]] += 1
    total = votes[0] + votes[180]
    stack = None
    if total and max(votes.values()) >= STACK_AGREEMENT * total:
        stack = 0 if votes[0] >= votes[180] else 180
    turns, decisions = [], []
    for number, reading in enumerate(readings, 1):
        turn = reading["cardinal"]
        followed = False
        if stack is not None and reading["words"] < STRONG_WORDS and turn in votes and turn != stack:
            turn, followed = stack, True
        turns.append(turn)
        decisions.append({"sourcePage": number, **reading, "appliedCardinalClockwiseDegrees": turn,
                          "followedStack": followed, "stackHeadToFoot": stack})
    return turns, decisions


def _series(plan: OrderPlan) -> list[dict]:
    return [entry for entry in plan.evidence if isinstance(entry, dict) and entry.get("kind") == "series"]


def direction_votes(plan: OrderPlan, page_count: int) -> tuple[int, int]:
    """Count printed-number evidence for "as scanned" and for "reversed".

    Within one numbered series, two pages next to each other in the scan vote
    forward when the later one has the higher number and reverse when it has
    the lower. A series with a single numbered page and a printed total equal
    to the page count votes by position: "Page 2 of 2" found first says reversed.
    Series with doubtful numbers (ambiguous, repeated, low confidence) don't vote.

    One step casts no vote: down onto 1 in a series with no printed total
    ("Page 2" then "Page 1"). A flipped stack looks like that, and so does a
    correctly fed statement followed by an enclosure whose numbering starts
    again. With a printed total ("2 of 5" then "1 of 5") a fresh start would
    need a second document of the same length, so that step does vote.
    """
    forward = reverse = 0
    for series in _series(plan):
        if not (series.get("oneUnambiguousNumberPerObservedPage") and series.get("numericValuesValid")
                and series.get("allNumericTokensPassThreshold")) or series.get("duplicateNumbersAcrossPages"):
            continue
        pairs = sorted((int(page), int(number)) for page, number in series.get("sourcePageNumberPairs") or [])
        for (_, earlier), (_, later) in zip(pairs, pairs[1:]):
            if later > earlier:
                forward += 1
            elif later < earlier and (later != 1 or series.get("total") is not None):
                reverse += 1
        if len(pairs) == 1 and series.get("total") == page_count and page_count > 1:
            page, number = pairs[0]
            mirrored = page_count + 1 - number
            if page == number and page != mirrored:
                forward += 1
            elif page == mirrored and page != number:
                reverse += 1
    return forward, reverse


def decide_order(plan: OrderPlan, page_count: int) -> dict:
    """Output order as a list of source pages, with the reason in plain words.

    A complete printed 1..N series is followed exactly, whatever shuffle it
    shows. Otherwise the stack is either as scanned or wholly reversed, and
    the printed numbers that were read vote on which.
    """
    identity = list(range(1, page_count + 1))
    forward, reverse = direction_votes(plan, page_count)
    base = {"forwardVotes": forward, "reverseVotes": reverse, "plannerStatus": plan.status}
    if plan.status in FULL_ORDER and sorted(plan.mapping) == identity:
        return {**base, "mapping": list(plan.mapping), "status": plan.status, "verified": True}
    if reverse and not forward:
        return {**base, "mapping": identity[::-1], "status": "reversed_stack_from_printed_numbers", "verified": False}
    if forward and not reverse:
        return {**base, "mapping": identity, "status": "partial_in_order", "verified": False}
    if forward and reverse:
        return {**base, "mapping": identity, "status": "unverified_mixed_evidence", "verified": False}
    if any(series.get("sourcePageNumberPairs") for series in _series(plan)):
        return {**base, "mapping": identity, "status": "unverified_numbers_inconclusive", "verified": False}
    return {**base, "mapping": identity, "status": "unknown_no_pagination_evidence", "verified": False}
