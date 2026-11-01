"""Access tokens for the Document AI service account, from the key held in app options.

The key arrives as JSON from Supervisor (a `!secret` reference in options),
so no credential file is written anywhere. The token provider has the same
shape the October backend expects: call it for a token, call refresh() after
an HTTP 401.
"""

from __future__ import annotations

import threading

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class ServiceAccountTokenProvider:
    def __init__(self, info: dict):
        self._info = dict(info)
        self._credentials = None
        self._request = None
        self._lock = threading.Lock()

    def _get(self, force_refresh: bool) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        with self._lock:
            if self._credentials is None:
                self._credentials = service_account.Credentials.from_service_account_info(self._info, scopes=SCOPES)
                self._request = Request()
            if force_refresh or not self._credentials.valid:
                self._credentials.refresh(self._request)
            return self._credentials.token

    def __call__(self) -> str:
        return self._get(force_refresh=False)

    def refresh(self) -> str:
        return self._get(force_refresh=True)

    @property
    def client_email(self) -> str:
        return self._info.get("client_email", "")
