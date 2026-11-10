"""Build the app's objects from settings. Shared by the service and its per-item worker."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .google_auth import ServiceAccountTokenProvider
from .notify import Notifier
from .paperless import Paperless
from .pipeline import Pipeline
from .store import Store

log = logging.getLogger("paperless_intake")


@dataclass
class App:
    settings: Settings
    store: Store
    notifier: Notifier
    pipeline: Pipeline


def build(settings: Settings, *, announce: bool = False) -> App:
    token_provider = None
    if settings.google_service_account:
        token_provider = ServiceAccountTokenProvider(settings.google_service_account)
        if announce:
            log.info("Google service account: %s", token_provider.client_email)
    elif announce:
        log.warning("google_service_account is not set; scans will wait until it is")
    store = Store(settings.items_dir)
    paperless = Paperless(settings.paperless_url, settings.paperless_token)
    notifier = Notifier(settings.ha_url, started=settings.webhook_started, finished=settings.webhook_finished,
                        alert=settings.webhook_alert)
    pipeline = Pipeline(settings, store, paperless, notifier, token_provider=token_provider)
    return App(settings, store, notifier, pipeline)
