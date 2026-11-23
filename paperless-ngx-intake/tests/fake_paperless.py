"""An in-memory stand-in for the Paperless client, for fast tests of resume and failure paths.

The real behaviours it imitates were observed on stock Paperless 3.3.4 on
2026-10-31; the end-to-end tests check the same things against the real thing.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import shutil
import subprocess
import tempfile

from pypdf import PdfReader

from paperless_intake.paperless import (NO_TAG, NOT_FOUND, TIMEOUT, UNREACHABLE, PaperlessError, PaperlessTaskFailed,
                                       Version)


class Crash(Exception):
    """Simulates the app dying right after a call reached Paperless."""


class FakePaperless:
    def __init__(self, version="3.3.4"):
        self.version = version
        self.tags = {"Scanner": 1, "Mail Intake": 2}
        self.docs: dict[int, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.task_ids = itertools.count(1)
        self.crash_after: set[str] = set()
        self.unreachable = False
        self.calls: list[str] = []
        self.own_ocr = True          # False: Paperless's OCR mode is off, so a raw scan gets no text
        self.archive_everything = False    # True: archive file generation is "always", even for a PDF with text
        self.me = 7                  # the app's own Paperless user; it sees its own documents and unowned ones
        self.refuse_duplicates = False     # True: Paperless is set to refuse a file it already holds
        # A Green short of memory: Paperless takes an upload but files it only when land() is called.
        self.slow_uploads = False
        self.pending: list = []
        self.upload_times_out = False      # True: the next upload's answer doesn't come before the app stops waiting
        self.on_effect = None              # told after every call that changed something (the kill harness)
        self.fail_once: dict = {}          # call name: the PaperlessError that call raises, once

    def add_old_scan(self, data: bytes, *, name="20261014_101500_000042.pdf", owner=None, tags=(), pages=None) -> int:
        """A scan stored before the app, through Paperless's own consume folder. Jane's rule tagged it Scanner."""
        return self.add_foreign(data, name=name, owner=owner, tags=[self.tags["Scanner"], *tags], pages=pages)

    def add_foreign(self, data: bytes, *, name="upload.pdf", owner=None, tags=(), pages=None) -> int:
        """A document that reached Paperless some other way: added by hand, from the phone, by email."""
        doc_id = next(self.ids)
        if pages is None:
            try:
                pages = len(PdfReader(io.BytesIO(data)).pages)
            except Exception:
                pages = None
        own_text = self._text(data) if data.startswith(b"%PDF-") else ""
        has_text = len(" ".join(own_text.split())) > 50
        self.docs[doc_id] = {"tags": list(tags), "notes": [], "owner": owner, "created": None, "name": name,
                             "pages": pages, "versions": [
            {"id": doc_id, "checksum": hashlib.sha256(data).hexdigest(), "label": None, "data": data,
             "archive": self.own_ocr and not has_text,
             "content": own_text if has_text else ("TESSERACT TEXT" if self.own_ocr else "")}]}
        return doc_id

    def _visible(self, doc_id) -> bool:
        return doc_id in self.docs and self.docs[doc_id]["owner"] in {None, self.me}

    def _missing(self, doc_id):
        return PaperlessError(f"Paperless GET /api/documents/{doc_id}/ returned HTTP 404", NOT_FOUND)

    def documents(self, *, with_tag=None):
        self._check("documents")
        return [{"id": doc_id, "tags": doc["tags"], "page_count": doc.get("pages"),
                 "original_file_name": doc.get("name"),
                 "versions": [{"id": v["id"], "checksum": v["checksum"], "version_label": v["label"],
                               "is_root": v["id"] == doc_id} for v in doc["versions"]]}
                for doc_id, doc in sorted(self.docs.items())
                if self._visible(doc_id) and (with_tag is None or with_tag in doc["tags"])]

    def notes(self, doc_id):
        self._check("notes")
        if not self._visible(doc_id):
            raise self._missing(doc_id)
        return [{"note": text} for text in self.docs[doc_id]["notes"]]

    def _check(self, name):
        self.calls.append(name)
        if name in self.fail_once:
            raise self.fail_once.pop(name)
        if self.unreachable:
            raise PaperlessError("Paperless unreachable: ConnectionError", UNREACHABLE)

    def _effect(self, name):
        """After a call has changed something in Paperless: where a test may have the app die."""
        if self.on_effect is not None:
            self.on_effect(f"paperless {name}")
        if name in self.crash_after:
            self.crash_after.discard(name)
            raise Crash(f"after {name}")

    def _answer_lost(self):
        if self.upload_times_out:
            self.upload_times_out = False
            raise PaperlessError("Paperless unreachable: ReadTimeout", TIMEOUT)

    def land(self):
        """Paperless gets round to filing the uploads it took while it was slow."""
        for apply in self.pending:
            apply()
        self.pending = []

    def server_version(self):
        self._check("server_version")
        return self.version

    def tag_id(self, name):
        self._check("tag_id")
        found = [number for tag, number in self.tags.items() if tag.lower() == name.lower()]     # name__iexact
        if len(found) != 1:
            raise PaperlessError(f"Expected exactly one tag named {name!r}, found {len(found)}", NO_TAG)
        return found[0]

    def find_by_checksum(self, sha, *, with_tag=None):
        self._check("find_by_checksum")
        for doc_id, doc in self.docs.items():
            if (doc["versions"][-1]["checksum"] == sha and self._visible(doc_id)
                    and (with_tag is None or with_tag in doc["tags"])):
                return doc_id
        return None

    @staticmethod
    def _text(data: bytes) -> str:
        """Pull text out the way Paperless does, with pdftotext -layout, when it is installed."""
        if shutil.which("pdftotext"):
            with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
                handle.write(data)
                handle.flush()
                done = subprocess.run(["pdftotext", "-q", "-layout", "-enc", "UTF-8", handle.name, "-"],
                                      capture_output=True, check=False)
                if done.returncode == 0:
                    return done.stdout.decode("utf-8", "replace")
        try:
            return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        except Exception:
            return ""

    def post_document(self, filename, data, *, tag_ids, created=None):
        self._check("post_document")
        sha = hashlib.sha256(data).hexdigest()
        task = f"t{next(self.task_ids)}"
        # Paperless 3 takes a file it already holds and makes a second document of it, unless it is
        # set to refuse duplicates (PAPERLESS_CONSUMER_DELETE_DUPLICATES), which was the rule before 3.0.
        if self.refuse_duplicates and any(doc["versions"][-1]["checksum"] == sha for doc in self.docs.values()):
            self.tasks[task] = {"status": "failure", "result": f"Not consuming {filename}: Document already exists"}
        else:
            def apply():
                doc_id = next(self.ids)
                self.docs[doc_id] = {"tags": list(tag_ids), "notes": [], "owner": self.me, "created": created,
                                     "name": filename, "pages": None, "versions": [
                    {"id": doc_id, "checksum": sha, "label": None, "data": data, "archive": self.own_ocr,
                     "content": "TESSERACT TEXT" if self.own_ocr else ""}]}
                self.tasks[task] = {"status": "success", "related_document": doc_id}
            self.tasks[task] = {"status": "pending"}
            self.pending.append(apply) if self.slow_uploads else apply()
        self._effect("post_document")
        self._answer_lost()
        return task

    def update_version(self, doc_id, filename, data, *, label):
        self._check("update_version")
        if not self._visible(doc_id):
            raise self._missing(doc_id)
        text = self._text(data)
        short = len(" ".join(text.split())) <= 50
        task = f"t{next(self.task_ids)}"

        def apply():
            version_id = next(self.ids)
            self.docs[doc_id]["versions"].insert(0, {
                "id": version_id, "checksum": hashlib.sha256(data).hexdigest(), "label": label, "data": data,
                "archive": short or self.archive_everything, "content": "TESSERACT TEXT" if short else text})
            self.tasks[task] = {"status": "success", "related_document": doc_id}
        self.tasks[task] = {"status": "pending"}
        self.pending.append(apply) if self.slow_uploads else apply()
        self._effect("update_version")
        self._answer_lost()
        return task

    def wait_for_task(self, task_id, **_):
        self._check("wait_for_task")
        task = self.tasks[task_id]
        if task["status"] == "failure":
            raise PaperlessTaskFailed(f"Paperless task {task_id} failure", task["result"])
        if task["status"] == "pending":
            raise PaperlessError(f"Paperless task {task_id} did not finish in time", TIMEOUT)
        return task

    def task_status(self, task_id):
        self._check("task_status")
        task = self.tasks.get(task_id)
        return task["status"] if task else None

    def unfinished_uploads(self):
        self._check("unfinished_uploads")
        return sum(1 for task in self.tasks.values() if task["status"] in {"pending", "started"})

    @staticmethod
    def task_document_id(task):
        return task.get("related_document")

    def versions(self, doc_id):
        self._check("versions")
        if not self._visible(doc_id):
            raise self._missing(doc_id)
        root_id = doc_id
        return [Version(v["id"], v["checksum"], v["label"], v["id"] == root_id) for v in self.docs[doc_id]["versions"]]

    def document(self, doc_id):
        self._check("document")
        if not self._visible(doc_id):
            raise self._missing(doc_id)
        doc = self.docs[doc_id]
        return {"id": doc_id, "tags": doc["tags"], "owner": doc["owner"], "content": doc["versions"][0]["content"],
                "versions": [{"id": v["id"], "checksum": v["checksum"], "version_label": v["label"],
                              "is_root": v["id"] == doc_id} for v in doc["versions"]]}

    def metadata(self, doc_id, version_id=None):
        self._check("metadata")
        version = next(v for v in self.docs[doc_id]["versions"] if v["id"] == version_id)
        return {"has_archive_version": version["archive"], "original_checksum": version["checksum"]}

    def download_original(self, doc_id, version_id):
        self._check("download_original")
        return next(v for v in self.docs[doc_id]["versions"] if v["id"] == version_id)["data"]

    def set_owner(self, doc_id, owner):
        self._check("set_owner")
        self.docs[doc_id]["owner"] = owner
        self._effect("set_owner")

    def add_note(self, doc_id, text):
        self._check("add_note")
        if not self._visible(doc_id):
            raise self._missing(doc_id)
        self.docs[doc_id]["notes"].append(text)
        self._effect("add_note")


class RecordingNotifier:
    def __init__(self):
        self.sent = []
        self.down = False                  # True: Home Assistant doesn't take webhooks
        self.on_send = None                # told after every webhook that went out (the kill harness)

    def has(self, which):
        return True

    def _sent(self, entry):
        if self.down:
            return False
        self.sent.append(entry)
        if self.on_send is not None:
            self.on_send(f"webhook {entry[0]}")
        return True

    def started(self, item_id):
        return self._sent(("started", item_id))

    def finished(self, item_id):
        return self._sent(("finished", item_id))

    def alert(self, **payload):
        return self._sent(("alert", payload))
