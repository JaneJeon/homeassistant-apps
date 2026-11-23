"""The app's own numbers, worked out from the records each time they are published (entities.py says what each is).

Nothing is kept as a running total, so the numbers can't drift from the records, and they are
right from the first publish after a restart. The lists name a scan's file or a document's id
in Paperless, with the app's own reason. Never document text.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from . import __version__
from .documents import CAP_SHARE, GAVE_UP, LATER, LET_GO, NEXT_RELEASE, WORKING
from .entities import IDLE, REREADING, SCANNING, WAITING as WAITING_STATUS
from .store import COMMITTED, COMMITTED_WARNING, FAILED, HELD, WAITING

LISTED = 25                 # at most this many entries in a list attribute


def _utc(at: float | None) -> str | None:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds") if at else None


def _local(at: float | None) -> str | None:
    return datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds") if at else None


class Reachability:
    """Whether Paperless answers the cheapest question it has (its version). Asked at most once a minute."""

    def __init__(self, ask: Callable[[], object], *, every: float = 60.0, monotonic=time.monotonic):
        self.ask = ask
        self.every = every
        self.monotonic = monotonic
        self._at = None
        self._answer: bool | None = None

    def __call__(self) -> bool | None:
        if self._at is None or self.monotonic() - self._at >= self.every:
            self._at = self.monotonic()
            try:
                self.ask()
                self._answer = True
            except Exception:
                self._answer = False
        return self._answer


def to_reread(documents, records) -> int | None:
    """Stored scans still waiting for their searchable version: from the records, plus the last Paperless list.

    Unknown (None) until the app has asked Paperless for its list once.
    """
    listing = documents.read_listing()
    if listing is None:
        return None
    count = sum(1 for record in records if record.status in {LATER, WORKING}
                or (record.status == NEXT_RELEASE and record.state.get("by") != __version__))
    known = {record.doc_id for record in records if record.status != LET_GO}
    for row in listing.get("documents") or []:
        if row["id"] in known or documents.scan_for(row["id"], row.get("checksum") or "") is not None:
            continue
        count += 1
    return count


def snapshot(service, *, reachable: bool | None = None) -> dict:
    settings = service.settings
    queue = service.queue
    items = service.store.all()
    documents = service.documents
    records = documents.all() if documents is not None else []

    held = [item for item in items if item.status == HELD]
    retrying = sorted((item for item in items if item.status == WAITING),
                      key=lambda item: float(item.state.get("next_attempt_at_unix") or 0))
    committed = [float(item.state.get("committed_at_unix") or 0) for item in items
                 if item.status in {COMMITTED, COMMITTED_WARNING}]
    # Set aside: gave up for good, or left for the next release by this one (3 tries for a fault, 24 for an
    # outside reason, or 10 restarts). One left by an earlier release is still to re-read: this one tries it.
    set_aside = [record for record in records if record.status == GAVE_UP
                 or (record.status == NEXT_RELEASE and record.state.get("by") == __version__)]

    # Status. Waiting means one thing: a scan is due and is being held back. A stored scan paused by
    # plan (the pace, tomorrow's pages) is not Waiting: it shows Re-reading or Idle, with the reason.
    reason = waits_for = until = None
    running = queue.running
    document_hold = queue.held.get("document")
    if running is not None:
        status = SCANNING if running.kind == "scan" else REREADING
    elif retrying:
        first = retrying[0]
        status, reason = WAITING_STATUS, first.state.get("reason")
        waits_for, until = first.state.get("waits_on"), first.state.get("next_attempt_at_unix")
    elif document_hold is not None:
        status = REREADING if document_hold.kind == "pace" else IDLE
        reason, waits_for, until = document_hold.reason, document_hold.waits_on, document_hold.until
    else:
        status = IDLE

    # Paused: something outside holds work back. Not for the page cap or the pace, which are planned.
    outside = [hold for hold in queue.held.values() if hold is not None and hold.outside]
    outside_reason = outside[0].reason if outside else None
    for item in retrying:
        if item.state.get("waits_on") and outside_reason is None:
            outside_reason = item.state.get("reason")
    for record in records:
        if record.status == LATER and record.state.get("waits_on") and outside_reason is None:
            outside_reason = record.state.get("reason")
    if outside_reason is None and service.pipeline.intent.read() is not None:
        outside_reason = "an upload may not have reached Paperless yet"

    cap = settings.daily_page_cap
    usage = service.pipeline.usage
    return {
        "status": status,
        "status_attributes": {"reason": reason, "waits_for": waits_for, "until": _local(until)},
        "paused": outside_reason is not None,
        "paused_attributes": {"reason": outside_reason},
        "paperless_reachable": reachable,
        "scans_held": len(held),
        "scans_held_attributes": {"scans": [{"file": item.state.get("original_name"), "reason": item.state.get("reason")}
                                            for item in held[:LISTED]]},
        "scans_retrying": len(retrying),
        "scans_failed": sum(1 for item in items if item.status == FAILED),
        "last_scan": _utc(max(committed)) if committed else None,
        "google_pages_today": usage.today(),
        "google_pages_today_attributes": {"cap": cap, "stored_scans_share": int(cap * CAP_SHARE)},
        "google_pages_this_month": usage.this_month(),
        "stored_to_reread": to_reread(documents, records) if documents is not None else None,
        "stored_set_aside": len(set_aside),
        "stored_set_aside_attributes": {"documents": [{"document": record.doc_id, "reason": record.state.get("reason")}
                                                      for record in set_aside[:LISTED]]},
    }
