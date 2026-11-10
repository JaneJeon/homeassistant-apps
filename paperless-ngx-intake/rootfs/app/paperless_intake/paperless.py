"""The parts of the Paperless-ngx REST API this app uses, pinned to API version 10.

Everything here is documented API: post_document, update_version, tasks,
documents (with their versions list and owner), download, metadata, notes and tags.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

API_VERSION = "10"


class PaperlessError(RuntimeError):
    """Paperless was unreachable or refused a request. The scan waits and retries."""


class PaperlessTaskFailed(RuntimeError):
    """Paperless accepted an upload but its consume task failed."""

    def __init__(self, message: str, result: str | None = None):
        super().__init__(message)
        self.result = result or ""


@dataclass(frozen=True)
class Version:
    id: int
    checksum: str | None
    label: str | None
    is_root: bool


class Paperless:
    def __init__(self, base_url: str, token: str, *, timeout: float = 60.0, session: requests.Session | None = None,
                 sleep=time.sleep, monotonic=time.monotonic):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Accept": f"application/json; version={API_VERSION}",
        })
        self.timeout = timeout
        self._sleep = sleep
        self._monotonic = monotonic

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(method, self.base_url + path, timeout=self.timeout, **kwargs)
        except requests.RequestException as error:
            raise PaperlessError(f"Paperless unreachable: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise PaperlessError(f"Paperless {method} {path} returned HTTP {response.status_code}")
        return response

    # Version pin ---------------------------------------------------------------

    def server_version(self) -> str:
        """The Paperless version, from the X-Version header every API response carries."""
        response = self._request("GET", "/api/tags/", params={"page_size": 1, "fields": "id"})
        version = response.headers.get("X-Version")
        if not version:
            raise PaperlessError("Paperless did not report its version")
        return version

    # Lookups --------------------------------------------------------------------

    def tag_id(self, name: str) -> int:
        response = self._request("GET", "/api/tags/", params={"name__iexact": name})
        results = response.json().get("results", [])
        if len(results) != 1:
            raise PaperlessError(f"Expected exactly one tag named {name!r}, found {len(results)}")
        return int(results[0]["id"])

    def find_by_checksum(self, sha256: str) -> int | None:
        response = self._request("GET", "/api/documents/", params={"checksum__iexact": sha256, "fields": "id"})
        results = response.json().get("results", [])
        return int(results[0]["id"]) if results else None

    def document(self, doc_id: int) -> dict:
        return self._request("GET", f"/api/documents/{doc_id}/").json()

    def versions(self, doc_id: int) -> list[Version]:
        return [Version(int(v["id"]), v.get("checksum"), v.get("version_label"), bool(v.get("is_root")))
                for v in self.document(doc_id).get("versions") or []]

    def metadata(self, doc_id: int, version_id: int | None = None) -> dict:
        params = {"version": version_id} if version_id else None
        return self._request("GET", f"/api/documents/{doc_id}/metadata/", params=params).json()

    def download_original(self, doc_id: int, version_id: int) -> bytes:
        return self._request("GET", f"/api/documents/{doc_id}/download/",
                             params={"original": "true", "version": version_id}).content

    # Writes ---------------------------------------------------------------------

    def post_document(self, filename: str, data: bytes, *, tag_ids: list[int]) -> str:
        files = {"document": (filename, data, "application/pdf")}
        form = [("tags", str(tag)) for tag in tag_ids]
        response = self._request("POST", "/api/documents/post_document/", files=files, data=form)
        return str(response.json())

    def update_version(self, doc_id: int, filename: str, data: bytes, *, label: str) -> str:
        files = {"document": (filename, data, "application/pdf")}
        response = self._request("POST", f"/api/documents/{doc_id}/update_version/", files=files,
                                 data={"version_label": label[:64]})
        return str(response.json())

    def set_owner(self, doc_id: int, owner: int | None) -> None:
        self._request("PATCH", f"/api/documents/{doc_id}/", json={"owner": owner})

    def add_note(self, doc_id: int, text: str) -> None:
        self._request("POST", f"/api/documents/{doc_id}/notes/", json={"note": text})

    def notes(self, doc_id: int) -> list[dict]:
        return self._request("GET", f"/api/documents/{doc_id}/notes/").json()

    # Tasks ----------------------------------------------------------------------

    def wait_for_task(self, task_id: str, *, timeout: float = 1800.0, poll: float = 3.0) -> dict:
        """Wait for a consume task; return the task record. Raise on failure."""
        deadline = self._monotonic() + timeout
        while True:
            response = self._request("GET", "/api/tasks/", params={"task_id": task_id})
            body = response.json()
            results = body.get("results", body) if isinstance(body, dict) else body
            if results:
                task = results[0]
                status = str(task.get("status", "")).lower()
                if status == "success":
                    return task
                if status in {"failure", "revoked"}:
                    raise PaperlessTaskFailed(f"Paperless task {task_id} {status}", str(task.get("result")))
            if self._monotonic() >= deadline:
                raise PaperlessError(f"Paperless task {task_id} did not finish in time")
            self._sleep(poll)

    @staticmethod
    def task_document_id(task: dict) -> int | None:
        for key in ("related_document",):
            if task.get(key):
                return int(task[key])
        result_data = task.get("result_data") or {}
        if isinstance(result_data, dict) and result_data.get("document_id"):
            return int(result_data["document_id"])
        return None
