"""An in-memory stand-in for the Paperless client, for fast tests of resume and failure paths.

The real behaviours it imitates were observed on stock Paperless 3.3.4 on
2026-10-31; the end-to-end tests check the same things against the real thing.
"""

from __future__ import annotations

import hashlib
import io
import itertools

from pypdf import PdfReader

from paperless_intake.paperless import PaperlessError, PaperlessTaskFailed, Version


class Crash(Exception):
    """Simulates the app dying right after a call reached Paperless."""


class FakePaperless:
    def __init__(self, version="3.3.4"):
        self.version = version
        self.tags = {"Physical Mail": 1, "Mail intake": 2}
        self.docs: dict[int, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.task_ids = itertools.count(1)
        self.crash_after: set[str] = set()
        self.unreachable = False
        self.calls: list[str] = []

    def _check(self, name):
        self.calls.append(name)
        if self.unreachable:
            raise PaperlessError("Paperless unreachable: ConnectionError")

    def server_version(self):
        self._check("server_version")
        return self.version

    def tag_id(self, name):
        self._check("tag_id")
        if name not in self.tags:
            raise PaperlessError(f"Expected exactly one tag named {name!r}, found 0")
        return self.tags[name]

    def find_by_checksum(self, sha):
        self._check("find_by_checksum")
        for doc_id, doc in self.docs.items():
            if doc["versions"][-1]["checksum"] == sha:
                return doc_id
        return None

    @staticmethod
    def _text(data: bytes) -> str:
        try:
            return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        except Exception:
            return ""

    def post_document(self, filename, data, *, tag_ids):
        self._check("post_document")
        sha = hashlib.sha256(data).hexdigest()
        task = f"t{next(self.task_ids)}"
        if self.find_by_checksum(sha):
            self.tasks[task] = {"status": "failure", "result": "Not consuming: It is a duplicate"}
        else:
            doc_id = next(self.ids)
            self.docs[doc_id] = {"tags": list(tag_ids), "notes": [], "owner": 7, "versions": [
                {"id": doc_id, "checksum": sha, "label": None, "data": data, "archive": True,
                 "content": "TESSERACT TEXT"}]}
            self.tasks[task] = {"status": "success", "related_document": doc_id}
        if "post_document" in self.crash_after:
            self.crash_after.discard("post_document")
            raise Crash("after post_document")
        return task

    def update_version(self, doc_id, filename, data, *, label):
        self._check("update_version")
        text = self._text(data)
        short = len(" ".join(text.split())) <= 50
        version_id = next(self.ids)
        self.docs[doc_id]["versions"].insert(0, {
            "id": version_id, "checksum": hashlib.sha256(data).hexdigest(), "label": label, "data": data,
            "archive": short, "content": "TESSERACT TEXT" if short else text})
        task = f"t{next(self.task_ids)}"
        self.tasks[task] = {"status": "success", "related_document": doc_id}
        if "update_version" in self.crash_after:
            self.crash_after.discard("update_version")
            raise Crash("after update_version")
        return task

    def wait_for_task(self, task_id, **_):
        self._check("wait_for_task")
        task = self.tasks[task_id]
        if task["status"] == "failure":
            raise PaperlessTaskFailed(f"Paperless task {task_id} failure", task["result"])
        return task

    @staticmethod
    def task_document_id(task):
        return task.get("related_document")

    def versions(self, doc_id):
        self._check("versions")
        root_id = doc_id
        return [Version(v["id"], v["checksum"], v["label"], v["id"] == root_id) for v in self.docs[doc_id]["versions"]]

    def document(self, doc_id):
        self._check("document")
        doc = self.docs[doc_id]
        return {"id": doc_id, "tags": doc["tags"], "owner": doc["owner"], "content": doc["versions"][0]["content"],
                "versions": [{"id": v["id"], "checksum": v["checksum"]} for v in doc["versions"]]}

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

    def add_note(self, doc_id, text):
        self._check("add_note")
        self.docs[doc_id]["notes"].append(text)


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def started(self, item_id):
        self.sent.append(("started", item_id))
        return True

    def finished(self, item_id):
        self.sent.append(("finished", item_id))
        return True

    def alert(self, **payload):
        self.sent.append(("alert", payload))
        return True
