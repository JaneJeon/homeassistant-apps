"""The parts of the Paperless-ngx REST API this app uses, pinned to API version 10.

Everything here is documented API: post_document, update_version, tasks,
documents (with their versions list and owner), download, metadata, notes and tags.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

API_VERSION = "10"
# An upload can take Paperless minutes to answer when the Green is short of memory. A short wait
# here only means the app stops listening while Paperless goes on taking the file in.
UPLOAD_TIMEOUT = (10.0, 600.0)          # seconds to connect, seconds to wait for the answer

# What kind of failure it was. Some say something about Paperless itself, not about one document:
UNREACHABLE = "unreachable"     # the connection failed or was refused
TIMEOUT = "timeout"             # Paperless didn't answer in time
BUSY = "busy"                   # HTTP 502, 503 or 504
LOGIN = "login"                 # HTTP 401: Paperless refused the app's token, so every request fails
NO_TAG = "no_tag"               # a tag the app needs isn't in Paperless (renamed, say), so every scan fails
# ...and some are about one request or one document:
NOT_FOUND = "not_found"         # HTTP 404
REFUSED = "refused"             # any other HTTP error
OUTSIDE = frozenset({UNREACHABLE, TIMEOUT, BUSY, LOGIN, NO_TAG})
COMPLETE = frozenset({"success", "failure", "revoked"})


class PaperlessError(RuntimeError):
    """Paperless was unreachable or refused a request.

    kind says which. outside: the failure is about Paperless, not about the document the app was
    working on, so the next document would meet it too.
    """

    def __init__(self, message: str, kind: str = REFUSED, *, answered: bool | None = True):
        super().__init__(message)
        self.kind = kind
        # Whether the outcome of this request is known: True, Paperless answered it (with an error);
        # False, it never reached Paperless (the connection was refused or never made); None, not known
        # (no answer came, or a 502/503/504 that a proxy may have sent after passing the request on).
        self.answered = answered

    @property
    def known(self) -> bool:
        """Whether Paperless is known to have taken nothing from this request."""
        return self.answered is not None

    @property
    def outside(self) -> bool:
        return self.kind in OUTSIDE


class PaperlessTaskFailed(RuntimeError):
    """Paperless accepted an upload but its consume task failed."""

    def __init__(self, message: str, result: str | None = None):
        super().__init__(message)
        self.result = result or ""


def _never_connected(error: Exception) -> bool:
    """A connection that was refused or never made: nothing of the request reached Paperless."""
    from urllib3.exceptions import NewConnectionError
    seen, current = set(), error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, NewConnectionError):
            return True
        current = getattr(current, "reason", None) or (current.args[0] if getattr(current, "args", None)
                                                       and isinstance(current.args[0], Exception) else None)
    return False


def _when(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() if value else None
    except ValueError:
        return None


def _task(row: dict) -> dict:
    return {"status": str(row.get("status", "")).lower(), "created": _when(row.get("date_created")),
            "started": _when(row.get("date_started"))}


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

    def _request(self, method: str, path: str, *, timeout=None, **kwargs) -> requests.Response:
        try:
            response = self.session.request(method, self.base_url + path, timeout=timeout or self.timeout, **kwargs)
        except requests.ConnectTimeout as error:
            raise PaperlessError(f"Paperless unreachable: {type(error).__name__}", TIMEOUT, answered=False) from None
        except requests.Timeout as error:
            raise PaperlessError(f"Paperless unreachable: {type(error).__name__}", TIMEOUT, answered=None) from None
        except requests.RequestException as error:
            raise PaperlessError(f"Paperless unreachable: {type(error).__name__}", UNREACHABLE,
                                 answered=False if _never_connected(error) else None) from None
        if response.status_code >= 400:
            status = response.status_code
            kind = (LOGIN if status == 401 else NOT_FOUND if status == 404
                    else BUSY if status in {502, 503, 504} else REFUSED)
            raise PaperlessError(f"Paperless {method} {path} returned HTTP {status}", kind,
                                 answered=None if kind == BUSY else True)
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
            raise PaperlessError(f"Expected exactly one tag named {name!r}, found {len(results)}", NO_TAG)
        return int(results[0]["id"])

    def find_by_checksum(self, sha256: str, *, with_tag: int | None = None) -> int | None:
        """A document whose first file is this one. with_tag: only one that also carries this tag."""
        response = self._request("GET", "/api/documents/", params={"checksum__iexact": sha256, "fields": "id,tags"})
        for row in response.json().get("results", []):
            if with_tag is None or with_tag in (row.get("tags") or []):
                return int(row["id"])
        return None

    def document(self, doc_id: int) -> dict:
        return self._request("GET", f"/api/documents/{doc_id}/").json()

    def documents(self, *, with_tag: int | None = None) -> list[dict]:
        """The documents this user can see, without their text: id, tags, versions, page count and file name.

        with_tag: only the ones that carry this tag. Paperless does the filtering, so nothing else is sent.
        Without it, every document this user can see.
        """
        rows, page = [], 1
        while True:
            params = {"page": page, "page_size": 100, "ordering": "id",
                      "fields": "id,tags,versions,page_count,original_file_name"}
            if with_tag is not None:
                params["tags__id__all"] = with_tag
            body = self._request("GET", "/api/documents/", params=params).json()
            rows.extend(body.get("results") or [])
            if not body.get("next"):
                return rows
            page += 1

    def versions(self, doc_id: int) -> list[Version]:
        return self.versions_in(self.document(doc_id))

    @staticmethod
    def versions_in(document: dict) -> list[Version]:
        """The file versions listed in a document that was already fetched, newest first."""
        return [Version(int(v["id"]), v.get("checksum"), v.get("version_label"), bool(v.get("is_root")))
                for v in document.get("versions") or []]

    def metadata(self, doc_id: int, version_id: int | None = None) -> dict:
        params = {"version": version_id} if version_id else None
        return self._request("GET", f"/api/documents/{doc_id}/metadata/", params=params).json()

    def download_original(self, doc_id: int, version_id: int) -> bytes:
        return self._request("GET", f"/api/documents/{doc_id}/download/",
                             params={"original": "true", "version": version_id}).content

    # Writes ---------------------------------------------------------------------

    def post_document(self, filename: str, data: bytes, *, tag_ids: list[int], created: str | None = None) -> str:
        files = {"document": (filename, data, "application/pdf")}
        form = [("tags", str(tag)) for tag in tag_ids]
        if created:
            # Given a date, Paperless uses it and doesn't guess one from its own reading of the pages.
            form.append(("created", created))
        response = self._request("POST", "/api/documents/post_document/", files=files, data=form,
                                 timeout=UPLOAD_TIMEOUT)
        return str(response.json())

    def update_version(self, doc_id: int, filename: str, data: bytes, *, label: str) -> str:
        files = {"document": (filename, data, "application/pdf")}
        response = self._request("POST", f"/api/documents/{doc_id}/update_version/", files=files,
                                 data={"version_label": label[:64]}, timeout=UPLOAD_TIMEOUT)
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
                raise PaperlessError(f"Paperless task {task_id} did not finish in time", TIMEOUT)
            self._sleep(poll)

    def task(self, task_id: str) -> dict | None:
        """A task's status (pending, started, success, failure, revoked) and when it was created. None if there is none."""
        body = self._request("GET", "/api/tasks/", params={"task_id": task_id}).json()
        results = body.get("results", body) if isinstance(body, dict) else body
        return _task(results[0]) if results else None

    def consume_tasks(self, *, created_after: float) -> list[dict]:
        """The files Paperless was given after this time and hasn't finished taking in: its unfinished consume tasks.

        Paperless shows this user its own tasks and those with no owner (the consume folder's).
        The status is checked here as well, so a Paperless that ignored the filter can't make
        the list empty by mistake.
        """
        params = {"task_type": "consume_file", "is_complete": "false", "page_size": 100,
                  "date_created_after": datetime.fromtimestamp(created_after, timezone.utc).isoformat()}
        tasks, page = [], 1
        while page <= 10:
            body = self._request("GET", "/api/tasks/", params={**params, "page": page}).json()
            results = body.get("results", []) if isinstance(body, dict) else body
            tasks += [_task(row) for row in results]
            if not (isinstance(body, dict) and body.get("next")):
                break
            page += 1
        return [task for task in tasks if task["status"] not in COMPLETE]

    @staticmethod
    def task_document_id(task: dict) -> int | None:
        for key in ("related_document",):
            if task.get(key):
                return int(task[key])
        result_data = task.get("result_data") or {}
        if isinstance(result_data, dict) and result_data.get("document_id"):
            return int(result_data["document_id"])
        return None
