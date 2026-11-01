"""End-to-end tests against stock Paperless-ngx at the pinned version.

Runs Paperless in Docker (its version comes from rootfs/app/tested-paperless-version),
then drives the real pipeline with synthetic scans and a fake Google. Skipped
unless INTAKE_E2E=1. Needs Docker and a Redis reachable at REDIS_URL
(default: a redis container is started).
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import requests

from paperless_intake.config import Settings, tested_paperless_version
from paperless_intake.docai.google_backend import GoogleBackendConfig, GoogleDocumentAIBackend
from paperless_intake.paperless import Paperless
from paperless_intake.pipeline import Pipeline
from paperless_intake.service import Service
from paperless_intake.store import COMMITTED, WAITING, Store

from fake_paperless import RecordingNotifier
from synth import FONT, FakeGoogle, PageSpec, make_scan, numbered_letter, token_provider

ROOT = Path(__file__).resolve().parent.parent
VERSION = tested_paperless_version(ROOT / "rootfs" / "app" / "tested-paperless-version")
PROCESSOR = ("projects/paperlss-document-pipeline/locations/us/processors/e2e/"
             "processorVersions/pretrained-ocr-v2.1-2024-08-07")
PASSWORD = "synthetic-e2e-password"
E2E = os.environ.get("INTAKE_E2E") == "1"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HookListener:
    """Collects Paperless workflow webhooks, like Home Assistant would receive them."""

    def __init__(self):
        self.received: list[dict] = []
        hooks = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
                hooks.received.append({"path": self.path, "body": body, "at": time.time()})
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self) -> list[str]:
        return [hook["path"] for hook in self.received]


@unittest.skipUnless(E2E, "set INTAKE_E2E=1 to run against Paperless in Docker")
class PaperlessE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls._start()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def _start(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="intake-e2e-"))
        cls.port = free_port()
        cls.containers = []
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            redis_port = free_port()
            name = f"intake-e2e-redis-{uuid.uuid4().hex[:6]}"
            subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{redis_port}:6379",
                            "redis:7.4-alpine"], check=True, capture_output=True)
            cls.containers.append(name)
            redis_url = f"redis://127.0.0.1:{redis_port}"
        for sub in ("data", "media", "consume", "export"):
            (cls.tmp / sub).mkdir()
            os.chmod(cls.tmp / sub, 0o777)
        name = f"intake-e2e-paperless-{uuid.uuid4().hex[:6]}"
        subprocess.run([
            "docker", "run", "-d", "--name", name, "--network", "host",
            "-e", f"PAPERLESS_REDIS={redis_url}", "-e", "PAPERLESS_ADMIN_USER=admin",
            "-e", f"PAPERLESS_ADMIN_PASSWORD={PASSWORD}", "-e", "PAPERLESS_ADMIN_MAIL=admin@example.invalid",
            "-e", f"PAPERLESS_PORT={cls.port}", "-e", "PAPERLESS_BIND_ADDR=0.0.0.0",
            "-e", f"PAPERLESS_URL=http://localhost:{cls.port}",
            "-e", "PAPERLESS_WEBHOOKS_ALLOW_INTERNAL_REQUESTS=true", "-e", "PAPERLESS_TIME_ZONE=UTC",
            "-e", "PAPERLESS_CONSUMER_POLLING=2", "-e", f"PAPERLESS_SECRET_KEY={uuid.uuid4().hex}",
            "-v", f"{cls.tmp}/data:/usr/src/paperless/data", "-v", f"{cls.tmp}/media:/usr/src/paperless/media",
            "-v", f"{cls.tmp}/consume:/usr/src/paperless/consume", "-v", f"{cls.tmp}/export:/usr/src/paperless/export",
            f"ghcr.io/paperless-ngx/paperless-ngx:{VERSION}",
        ], check=True, capture_output=True)
        cls.containers.append(name)
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 600
        while True:
            try:
                response = requests.post(f"{cls.base}/api/token/", data={"username": "admin", "password": PASSWORD},
                                         timeout=5)
                if response.status_code == 200:
                    cls.token = response.json()["token"]
                    break
            except requests.RequestException:
                pass
            if time.time() > deadline:
                logs = subprocess.run(["docker", "logs", "--tail", "50", name], capture_output=True, text=True)
                raise RuntimeError("Paperless did not start:\n" + logs.stdout + logs.stderr)
            time.sleep(3)
        cls.api = requests.Session()
        cls.api.headers.update({"Authorization": f"Token {cls.token}", "Accept": "application/json; version=10"})
        cls.tag = cls.api.post(f"{cls.base}/api/tags/", json={"name": "Physical Mail"}).json()["id"]
        cls.intake_tag = cls.api.post(f"{cls.base}/api/tags/", json={"name": "Mail intake"}).json()["id"]
        cls.hooks = HookListener()
        cls._jane_workflows()

    @classmethod
    def _jane_workflows(cls):
        """Jane's three ingest workflows, with the planned edit to the third: it skips the intake tag.

        The third keeps its sources, API upload included. Paperless checks sources only on
        "consumption started" triggers, so they don't filter "document added".
        """
        hook = lambda path: {"type": 4, "webhook": {"url": f"http://127.0.0.1:{cls.hooks.port}/{path}",
                                                     "use_params": False, "as_json": True, "params": None,
                                                     "body": "{}", "headers": None, "include_document": False}}
        workflows = [
            {"name": "Mark physical mail", "order": 1, "enabled": True,
             "triggers": [{"type": 1, "sources": [1], "filter_filename": "*.pdf"}],
             "actions": [{"type": 1, "assign_tags": [cls.tag]}]},
            {"name": "Track consume start", "order": 2, "enabled": True,
             "triggers": [{"type": 1, "sources": [1], "filter_filename": "*.pdf"}], "actions": [hook("consume_started")]},
            {"name": "Track physical mail added", "order": 3, "enabled": True,
             "triggers": [{"type": 2, "sources": [1, 2, 3], "filter_has_tags": [cls.tag],
                           "filter_has_not_tags": [cls.intake_tag]}],
             "actions": [hook("document_added")]},
        ]
        for workflow in workflows:
            response = cls.api.post(f"{cls.base}/api/workflows/", json=workflow)
            response.raise_for_status()

    @classmethod
    def tearDownClass(cls):
        for name in getattr(cls, "containers", []):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def harness(self, **overrides):
        root = Path(tempfile.mkdtemp(prefix="intake-e2e-app-"))
        self.addCleanup(shutil.rmtree, root, True)
        values = dict(
            paperless_url=self.base, paperless_token=self.token, google_service_account=None,
            processor_version=PROCESSOR, physical_mail_tag="Physical Mail", intake_tag="Mail intake", ha_url="http://ha.invalid",
            webhook_started="s", webhook_finished="f", webhook_alert="a", daily_page_cap=500, min_free_mib=0,
            keep_local_copies_days=7, watchdog_minutes=45, capture_dir=root / "capture", intake_dir=root / "intake",
            font_path=FONT, tested_paperless_version=VERSION, stable_seconds=0, poll_seconds=0,
        )
        values.update(overrides)
        settings = Settings(**values)
        settings.capture_dir.mkdir(parents=True)
        google = FakeGoogle()
        store = Store(settings.items_dir)
        notifier = RecordingNotifier()
        client = Paperless(self.base, self.token)

        def backend(item):
            return GoogleDocumentAIBackend(GoogleBackendConfig(PROCESSOR, item.google_dir()),
                                           token_provider=token_provider, transport=google)

        pipeline = Pipeline(settings, store, client, notifier, token_provider=token_provider, backend_factory=backend)
        service = Service(settings, store, pipeline, notifier)
        return settings, store, service, client, notifier

    def run_until_quiet(self, service, rounds=20):
        service.claim_captures()
        for _ in range(rounds):
            if not service.step():
                break

    def search(self, word: str) -> list[int]:
        response = self.api.get(f"{self.base}/api/documents/", params={"query": word, "fields": "id"})
        return [row["id"] for row in response.json()["results"]]

    def test_commit_reads_back_and_searches_on_google_text(self):
        settings, store, service, client, notifier = self.harness()
        marker = "zebracrossing" + uuid.uuid4().hex[:6]
        scan = numbered_letter(3, tag=marker)
        (settings.capture_dir / "20261101_100000_000500.pdf").write_bytes(scan.pdf)
        before = len(self.hooks.received)
        self.run_until_quiet(service)
        item = store.all()[0]
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        doc_id = item.state["doc_id"]
        document = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()
        self.assertEqual(len(document["versions"]), 2)
        self.assertIn(self.tag, document["tags"])
        self.assertIn(self.intake_tag, document["tags"])
        self.assertIn(marker, document["content"])
        notes = self.api.get(f"{self.base}/api/documents/{doc_id}/notes/").json()
        self.assertEqual(len(notes), 1)
        read_back = [e for e in item.events() if e["event"] == "read_back"][0]
        self.assertFalse(read_back["version_has_archive"])                       # Paperless did not re-OCR
        self.assertTrue(read_back["text_match"]["ok"], read_back["text_match"])
        deadline = time.time() + 60
        while time.time() < deadline and doc_id not in self.search(marker):
            time.sleep(2)                                                        # the search index updates async
        self.assertIn(doc_id, self.search(marker))
        time.sleep(5)
        self.assertEqual(self.hooks.paths()[before:], [])                        # no Paperless ingest webhooks fire
        self.assertEqual([k for k, _ in notifier.sent], ["started", "finished"])

    def test_nearly_blank_scan_is_original_only(self):
        settings, store, service, client, _ = self.harness()
        scan = make_scan([PageSpec(["Hi " + uuid.uuid4().hex[:4]])])
        (settings.capture_dir / "short.pdf").write_bytes(scan.pdf)
        self.run_until_quiet(service)
        item = store.all()[0]
        self.assertEqual((item.status, item.state["mode"]), (COMMITTED, "original_only"), item.state.get("reason"))
        self.assertEqual(len(client.versions(item.state["doc_id"])), 1)

    def test_resume_after_losing_the_document_id_uploads_nothing_new(self):
        settings, store, service, client, _ = self.harness()
        scan = numbered_letter(1, tag=uuid.uuid4().hex[:8])
        (settings.capture_dir / "resume.pdf").write_bytes(scan.pdf)
        self.run_until_quiet(service)
        item = store.all()[0]
        doc_id = item.state["doc_id"]
        count = self.api.get(f"{self.base}/api/documents/").json()["count"]
        # Pretend the app died after Paperless accepted the upload but before the id was saved.
        item.state.update(doc_id=None, upload_task=None, note_added=False, status="committing")
        item.save()
        service.pipeline.run(store.load(item.sha))
        item = store.load(item.sha)
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["doc_id"], doc_id)
        self.assertEqual(self.api.get(f"{self.base}/api/documents/").json()["count"], count)
        self.assertEqual(len(client.versions(doc_id)), 2)

    def test_untested_paperless_version_waits_without_uploading(self):
        settings, store, service, client, notifier = self.harness(tested_paperless_version="0.0.0")
        count = self.api.get(f"{self.base}/api/documents/").json()["count"]
        (settings.capture_dir / "pin.pdf").write_bytes(numbered_letter(1, tag=uuid.uuid4().hex[:8]).pdf)
        self.run_until_quiet(service)
        item = store.all()[0]
        self.assertEqual((item.status, item.state["reason_kind"]), (WAITING, "version_mismatch"))
        self.assertIn(f"Paperless is {VERSION}", item.state["reason"])
        self.assertEqual(self.api.get(f"{self.base}/api/documents/").json()["count"], count)

    def test_consume_folder_still_fires_janes_workflows(self):
        """Control: a file dropped into Paperless's own consume folder still runs the ingest workflows."""
        before = len(self.hooks.received)
        scan = numbered_letter(1, tag=uuid.uuid4().hex[:8])
        (self.tmp / "consume" / "manual-drop.pdf").write_bytes(scan.pdf)
        deadline = time.time() + 300
        while time.time() < deadline and "/document_added" not in self.hooks.paths()[before:]:
            time.sleep(3)
        self.assertEqual(self.hooks.paths()[before:], ["/consume_started", "/document_added"])
        # The backfill adds a version to documents like this one. That must not signal again:
        # Paperless skips workflows for version documents.
        client = Paperless(self.base, self.token)
        doc_id = client.find_by_checksum(scan.sha256)
        self.assertIsNotNone(doc_id)
        version = numbered_letter(1, tag=uuid.uuid4().hex[:8])
        client.wait_for_task(client.update_version(doc_id, "manual-drop.searchable.pdf", version.pdf, label="e2e"))
        time.sleep(5)
        self.assertEqual(self.hooks.paths()[before:], ["/consume_started", "/document_added"])

    def test_api_upload_without_the_intake_tag_signals_document_added(self):
        """Why the intake tag exists. If this starts failing, Paperless now honours sources on
        "document added", and the tag could go. Either way the main test shows no double signal."""
        before = len(self.hooks.received)
        client = Paperless(self.base, self.token)
        scan = numbered_letter(1, tag=uuid.uuid4().hex[:8])
        client.wait_for_task(client.post_document("api-only.pdf", scan.pdf, tag_ids=[self.tag]))
        deadline = time.time() + 60
        while time.time() < deadline and "/document_added" not in self.hooks.paths()[before:]:
            time.sleep(2)
        self.assertEqual(self.hooks.paths()[before:], ["/document_added"])


if __name__ == "__main__":
    unittest.main()
