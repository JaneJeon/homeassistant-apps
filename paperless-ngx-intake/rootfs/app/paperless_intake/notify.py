"""Tell Home Assistant what happened, through its webhooks.

Two webhooks already exist and drive the ingest counter and the 45-minute
watchdog: "started" and "finished" (named consume_started and document_added
in Home Assistant). A third carries holds, commit failures, commit
warnings and long waits to an alert automation, and later a "resolved" message
when an alerted scan is committed or discarded. Payloads never contain document text.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, ha_url: str, *, started: str, finished: str, alert: str, timeout: float = 10.0,
                 session: requests.Session | None = None):
        self.ha_url = ha_url.rstrip("/")
        self.ids = {"started": started, "finished": finished, "alert": alert}
        self.timeout = timeout
        self.session = session or requests.Session()

    def _post(self, which: str, payload: dict) -> bool:
        webhook_id = self.ids.get(which)
        if not webhook_id:
            log.info("webhook %s not configured; skipped", which)
            return False
        try:
            response = self.session.post(f"{self.ha_url}/api/webhook/{webhook_id}", json=payload, timeout=self.timeout)
        except requests.RequestException as error:
            log.warning("webhook %s failed: %s", which, type(error).__name__)
            return False
        if response.status_code >= 400:
            log.warning("webhook %s returned HTTP %s", which, response.status_code)
            return False
        return True

    def started(self, item_id: str) -> bool:
        return self._post("started", {"source": "paperless-intake", "item": item_id})

    def finished(self, item_id: str) -> bool:
        return self._post("finished", {"source": "paperless-intake", "item": item_id})

    def alert(self, *, kind: str, item_id: str, scanned_at: str, pages: int | None, gap: str) -> bool:
        return self._post("alert", {
            "source": "paperless-intake",
            "kind": kind,
            "item": item_id,
            "scanned_at": scanned_at,
            "pages": pages,
            "gap": gap,
        })
