"""Putting a searchable version on a document in Paperless, and reading it back.

One place for both callers: a scan as it is stored (pipeline.py) and a document
that is already stored (documents.py). Both steps look before they act, so
either can be run again after being cut short.
"""

from __future__ import annotations

import hashlib
from typing import Callable

from . import checks
from .paperless import Paperless, PaperlessError, PaperlessTaskFailed

COPY_RESAVED = "Paperless made its own archive file of the searchable copy and shows that in its place"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def await_upload(paperless: Paperless, intent, task: str, *, what: str, sha: str, name: str,
                 doc_id: int | None = None) -> dict:
    """Wait for Paperless to take in an upload, and settle its intent mark once the outcome is known.

    If Paperless doesn't say in time, the mark stays (or is put down, for an upload an earlier
    run started), so the queue starts no other upload until this one is settled.
    """
    try:
        found = paperless.wait_for_task(task)
    except PaperlessTaskFailed:
        intent.close()
        raise
    except PaperlessError:
        if intent.read() is None:
            intent.open(what=what, sha256=sha, name=name, doc_id=doc_id)
            intent.task(task)
        raise
    intent.close()
    return found


def ensure_added(paperless: Paperless, doc_id: int, wanted: str, read: Callable[[], bytes], name: str, label: str, *,
                 task: str | None, remember: Callable[[str | None], None], intent) -> bool:
    """Make sure the file with this fingerprint is one of the document's file versions.

    Each step looks before it acts, so a run that was killed anywhere can be run again:
    nothing is uploaded when Paperless already lists the file; an upload an earlier run
    started (task) is waited for first; and the intent mark goes down before the file goes
    out, so an upload whose task id was never written down is settled by the queue (by
    looking at Paperless) before this step runs again. remember() is told the task id as
    soon as Paperless answers. False when the local file can no longer be trusted: it is
    gone, or it isn't the bytes that were recorded.
    """
    if any(version.checksum == wanted for version in paperless.versions(doc_id)):
        return True
    if task:
        try:
            await_upload(paperless, intent, task, what="version", sha=wanted, name=name, doc_id=doc_id)
        except PaperlessTaskFailed:
            remember(None)
        if any(version.checksum == wanted for version in paperless.versions(doc_id)):
            return True
    try:
        data = read()
    except OSError:
        return False
    if sha256(data) != wanted:
        return False
    intent.open(what="version", sha256=wanted, name=name, doc_id=doc_id)
    task = paperless.update_version(doc_id, name, data, label=label)
    remember(task)
    intent.task(task)
    await_upload(paperless, intent, task, what="version", sha=wanted, name=name, doc_id=doc_id)
    return True


def read_back(paperless: Paperless, doc_id: int, wanted: str, google_text: str, *,
              versions: list | None = None, document: dict | None = None) -> tuple[list[str], dict]:
    """What Paperless now has for this searchable version. Returns warnings and what was measured."""
    warnings, record = [], {}
    versions = versions if versions is not None else paperless.versions(doc_id)
    version = next((v for v in versions if v.checksum == wanted and not v.is_root), None)
    if version is None:
        return ["the searchable copy isn't among the document's versions"], record
    if versions[0].id != version.id:
        warnings.append("the searchable copy isn't the newest version, so search may not use Google's text")
    stored = sha256(paperless.download_original(doc_id, version.id))
    record["version_download_sha256"] = stored
    if stored != wanted:
        warnings.append("the searchable copy in Paperless doesn't match byte for byte")
    metadata = paperless.metadata(doc_id, version.id)
    record["version_has_archive"] = metadata.get("has_archive_version")
    if metadata.get("has_archive_version"):
        # Paperless leaves a PDF that has text alone only while its OCR mode is auto or
        # off and its archive file generation is auto or never. This is how the app
        # notices that one of them changed: by what Paperless did, not by what it is set to.
        warnings.append(f"{COPY_RESAVED} (see Paperless's OCR mode and archive file generation settings)")
    document = document if document is not None else paperless.document(doc_id)
    match = checks.text_matches(google_text, document.get("content") or "")
    record["text_match"] = match
    if not match["ok"]:
        warnings.append(f"Paperless's search text differs from Google's "
                        f"({match['missing_words']} of {match['google_words']} words missing)")
    record["version_id"] = version.id
    return warnings, record
