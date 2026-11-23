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
from paperless_intake.documents import DONE, NOT_NEEDED, Documents
from paperless_intake.paperless import Paperless, PaperlessError
from paperless_intake.pipeline import Pipeline
from paperless_intake.service import Service
from paperless_intake.store import COMMITTED, COMMITTED_WARNING, WAITING, Store

from fake_paperless import RecordingNotifier
from synth import FakeGoogle, PageSpec, make_scan, numbered_letter, token_provider

ROOT = Path(__file__).resolve().parent.parent
VERSION = tested_paperless_version(ROOT / "rootfs" / "app" / "tested-paperless-version")
PROCESSOR = ("projects/paperlss-document-pipeline/locations/us/processors/e2e/"
             "processorVersions/pretrained-ocr-v2.1-2024-08-07")
PASSWORD = "synthetic-e2e-password"
# The global permissions the app's own Paperless user needs, as DOCS.md tells Jane to set them.
# Every test drives the app with this user, not the admin, so a missing permission fails here.
INTAKE_PERMISSIONS = ["add_document", "view_document", "change_document", "add_note", "view_note",
                      "view_tag", "view_paperlesstask"]
E2E = os.environ.get("INTAKE_E2E") == "1"
# Extra settings for the Paperless under test, as KEY=VALUE,KEY=VALUE. The suite runs against
# stock settings and against Paperless with its own OCR and archive files switched off.
PAPERLESS_ENV = [pair for pair in os.environ.get("INTAKE_E2E_PAPERLESS_ENV", "").split(",") if "=" in pair]
OCR_OFF = "PAPERLESS_OCR_MODE=off" in PAPERLESS_ENV
# Set when the Paperless under test is configured to re-save the searchable copy, which the app must notice.
COPY_REWORKED = os.environ.get("INTAKE_E2E_EXPECT_COPY_REWORKED") == "1"


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
            *[flag for pair in PAPERLESS_ENV for flag in ("-e", pair)],
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
        admin_id = cls.api.get(f"{cls.base}/api/users/", params={"username__iexact": "admin"}).json()["results"][0]["id"]
        cls.intake_token, intake_id = cls._user("intake", INTAKE_PERMISSIONS)
        cls.intake_id = intake_id
        cls.reader_token, _ = cls._user("reader", ["view_document"])    # another user, like an MCP client
        # Jane's tags are owned by her account, so the app's user needs view permission on them.
        view = {"view": {"users": [intake_id], "groups": []}, "change": {"users": [], "groups": []}}
        cls.tag = cls._tag("Scanner", admin_id, view)
        cls.intake_tag = cls._tag("Mail Intake", admin_id, view)
        cls.hooks = HookListener()
        cls._jane_workflows()

    @classmethod
    def _user(cls, name: str, permissions: list[str]) -> tuple[str, int]:
        response = cls.api.post(f"{cls.base}/api/users/", json={
            "username": name, "password": PASSWORD, "is_superuser": False, "is_staff": False,
            "user_permissions": permissions})
        response.raise_for_status()
        token = requests.post(f"{cls.base}/api/token/", data={"username": name, "password": PASSWORD}, timeout=10)
        token.raise_for_status()
        return token.json()["token"], response.json()["id"]

    @classmethod
    def _tag(cls, name: str, owner: int, permissions: dict) -> int:
        response = cls.api.post(f"{cls.base}/api/tags/", json={"name": name, "owner": owner,
                                                               "set_permissions": permissions})
        response.raise_for_status()
        return response.json()["id"]

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

    def harness(self, *, loop=False, **overrides):
        """The app, wired as it runs. loop: also let it look after documents that are already stored.

        Every test shares one Paperless, so the loop sees the other tests' documents too. Only the
        tests about the loop switch it on, and they assert on their own document.
        """
        root = Path(tempfile.mkdtemp(prefix="intake-e2e-app-"))
        self.addCleanup(shutil.rmtree, root, True)
        values = dict(
            paperless_url=self.base, paperless_token=self.intake_token, google_service_account=None,
            processor_version=PROCESSOR, scanner_tag="Scanner", intake_tag="Mail Intake", ha_url="http://ha.invalid",
            webhook_started="s", webhook_finished="f", webhook_alert="a", daily_page_cap=500, min_free_mib=0,
            keep_local_copies_days=7, watchdog_minutes=45, capture_dir=root / "capture", intake_dir=root / "intake",
            tested_paperless_version=VERSION, stable_seconds=0, poll_seconds=0,
            # The test machine's own load must not hold the app back here; the gate has unit tests of its own.
            min_available_mib=0, max_io_pressure=101, max_memory_pressure=101, pace_seconds=0,
        )
        values.update(overrides)
        settings = Settings(**values)
        settings.capture_dir.mkdir(parents=True)
        google = FakeGoogle()
        store = Store(settings.items_dir)
        notifier = RecordingNotifier()
        client = Paperless(self.base, self.intake_token)

        def backend(item):
            return GoogleDocumentAIBackend(GoogleBackendConfig(PROCESSOR, item.google_dir()),
                                           token_provider=token_provider, transport=google)

        pipeline = Pipeline(settings, store, client, notifier, token_provider=token_provider, backend_factory=backend)
        self.documents = Documents(settings, store, client, pipeline)
        pipeline.documents = self.documents
        self.google = google
        self.backend = backend
        service = Service(settings, store, pipeline, notifier, documents=self.documents if loop else None)
        return settings, store, service, client, notifier

    def run_until_quiet(self, service, rounds=20):
        service.claim_captures()
        for _ in range(rounds):
            if not service.step():
                break

    def loop_until(self, service, doc_id, rounds=80):
        """Let the loop work until it has an outcome for this document. Other tests' documents come by too."""
        for _ in range(rounds):
            if self.documents.exists(doc_id) and self.documents.load(doc_id).status not in {"working", "later"}:
                return self.documents.load(doc_id)
            if not service.step():
                self.documents._look_again_at = 0.0          # no need to sit out the five minutes between looks
        self.fail(f"the loop reached no outcome for document {doc_id}")

    def stored_by_other_means(self, pdf: bytes, name: str, *, owner_cleared=True, tagged=True) -> int:
        """A document that reaches Paperless without the app: uploaded by Jane's own account.

        tagged: she then gives it the Scanner tag in Paperless. The tag is what makes a
        document the app's to look at, and adding it by hand is how a scan that lacks it is mended.
        """
        response = self.api.post(f"{self.base}/api/documents/post_document/",
                                 files={"document": (name, pdf, "application/pdf")})
        response.raise_for_status()
        admin = Paperless(self.base, self.token)
        doc_id = admin.task_document_id(admin.wait_for_task(str(response.json())))
        changes = {}
        if owner_cleared:       # as a consume-folder document is: nobody's, so every user sees it
            changes["owner"] = None
        if tagged:       # added afterwards, so that no "document added" workflow fires for it
            changes["tags"] = [self.tag]
        if changes:
            self.api.patch(f"{self.base}/api/documents/{doc_id}/", json=changes).raise_for_status()
        return doc_id

    def give_the_loop_its_turns(self, service, rounds=200):
        """Let the loop work until it has nothing left to do, and fail if it never gets there.

        Documents from earlier tests share this Paperless. If the rounds ran out first, a check that the
        loop left a document alone would pass without the loop ever having reached it.
        """
        for _ in range(rounds):
            self.documents._look_again_at = 0.0              # no need to sit out the five minutes between looks
            if not service.step():
                return
        self.fail(f"the loop still had work to do after {rounds} rounds, so it may never have reached this document")

    FIELDS_THAT_ARE_JANES = ("title", "created", "tags", "owner", "correspondent", "document_type", "storage_path",
                             "archive_serial_number", "custom_fields")

    def search(self, word: str) -> list[int]:
        response = self.api.get(f"{self.base}/api/documents/", params={"query": word, "fields": "id"})
        return [row["id"] for row in response.json()["results"]]

    def test_the_app_can_ask_paperless_how_its_uploads_ended(self):
        """The tasks API as the app's own user, as the queue uses it to settle an upload in doubt (gate.Intent)."""
        settings, store, service, client, notifier = self.harness()
        (settings.capture_dir / "20261101_100000_000501.pdf").write_bytes(numbered_letter(1, tag=uuid.uuid4().hex[:8]).pdf)
        self.run_until_quiet(service)
        item = store.all()[0]
        self.assertEqual(client.task_status(item.state["upload_task"]), "success")
        self.assertIsNone(client.task_status(str(uuid.uuid4())))
        deadline = time.time() + 120
        while client.unfinished_uploads() and time.time() < deadline:
            time.sleep(2)
        self.assertEqual(client.unfinished_uploads(), 0)
        # A mark for an upload that landed is settled by its checksum; one Paperless never got, once it is idle.
        intent = service.pipeline.intent
        intent.open(what="original", sha256=item.sha, name="landed.pdf")
        self.assertEqual(intent.settle(client, settings.intake_tag), "it landed")
        intent.open(what="version", sha256="0" * 64, name="lost.pdf", doc_id=item.state["doc_id"])
        intent.task(item.state["version_task"])
        self.assertIn("task for it ended (success)", intent.settle(client, settings.intake_tag))
        self.assertIsNone(intent.read())

    def test_commit_reads_back_and_searches_on_google_text(self):
        settings, store, service, client, notifier = self.harness()
        marker = "zebracrossing" + uuid.uuid4().hex[:6]
        scan = numbered_letter(3, tag=marker)
        (settings.capture_dir / "20261101_100000_000500.pdf").write_bytes(scan.pdf)
        before = len(self.hooks.received)
        self.run_until_quiet(service)
        item = store.all()[0]
        self.assertEqual(item.status, COMMITTED_WARNING if COPY_REWORKED else COMMITTED, item.state.get("reason"))
        doc_id = item.state["doc_id"]
        document = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()
        self.assertEqual(len(document["versions"]), 2)
        self.assertIn(self.tag, document["tags"])
        self.assertIn(self.intake_tag, document["tags"])
        self.assertIsNone(document["owner"])                                     # like consume-folder scans
        self.assertEqual(str(document["created"])[:10], "2026-11-01")            # the scan date, not Paperless's guess
        if COPY_REWORKED:
            # Paperless was told to archive every PDF. It re-saved the copy, and the app says so.
            read_back = [e for e in item.events() if e["event"] == "read_back"][-1]
            self.assertTrue(read_back["version_has_archive"])
            self.assertIn("made its own archive file of the searchable copy", item.state.get("reason") or "")
            self.assertIn("alert", [k for k, _ in notifier.sent])
            return
        # What Paperless made of the raw scan. With its own OCR and archive files off, nothing:
        # version 1 is shown as the scanner saved it.
        raw = self.api.get(f"{self.base}/api/documents/{doc_id}/metadata/", params={"version": doc_id}).json()
        self.assertEqual(bool(raw["has_archive_version"]), not OCR_OFF)
        reader = requests.get(f"{self.base}/api/documents/{doc_id}/", timeout=10,
                              headers={"Authorization": f"Token {self.reader_token}"})
        self.assertEqual(reader.status_code, 200)                                # other users still see it
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

    def test_a_stack_put_in_flipped_turned_and_askew_reads_back_and_searches(self):
        """The whole point, against real Paperless: any way the stack goes in, the same document comes out.

        Also the two things version 1.0 held real scans for: a page a degree askew,
        and text in a script the old text-layer font didn't cover. And the thing 1.1.0
        refused a real scan for: black and white pages saved as one-bit images beside
        a JPEG page, as a scanner that detects colour writes them.
        """
        settings, store, service, client, notifier = self.harness()
        marker = "quokkaferry" + uuid.uuid4().hex[:6]
        korean = "한국어를"
        pages = [PageSpec([f"Synthetic account statement line {n}.{i} for testing purposes only" for i in range(1, 6)]
                          + [f"Reference {marker}", f"주의: {korean} 사용하시는 경우 언어 지원 서비스를 무료로 이용하실 수 있습니다"],
                          footer=f"Page {n} of 3") for n in (1, 2, 3)]
        scan = make_scan(pages, flipped=True, turn=180, skew=1.3, encodings=["jpeg", "ccitt", "flate"])
        (settings.capture_dir / "20261122_100000_000600.pdf").write_bytes(scan.pdf)
        self.run_until_quiet(service)
        item = store.all()[0]
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertEqual(item.state["page_order"], [3, 2, 1])
        self.assertEqual(item.state["rotations"], [180, 180, 180])
        doc_id = item.state["doc_id"]
        document = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()
        self.assertEqual(len(document["versions"]), 2)
        self.assertIn(marker, document["content"])
        self.assertIn(korean, document["content"])                               # Paperless pulled the Korean text out
        self.assertLess(document["content"].index("line 1.1"), document["content"].index("line 3.1"))   # reading order
        read_back = [e for e in item.events() if e["event"] == "read_back"][0]
        self.assertFalse(read_back["version_has_archive"])
        self.assertTrue(read_back["text_match"]["ok"], read_back["text_match"])
        deadline = time.time() + 60
        while time.time() < deadline and doc_id not in self.search(marker):
            time.sleep(2)
        self.assertIn(doc_id, self.search(marker))
        self.assertEqual([k for k, _ in notifier.sent], ["started", "finished"])      # nothing for Jane to do

    def test_nearly_blank_scan_is_original_only(self):
        settings, store, service, client, _ = self.harness()
        scan = make_scan([PageSpec(["Hi " + uuid.uuid4().hex[:4]])])
        (settings.capture_dir / "short.pdf").write_bytes(scan.pdf)
        self.run_until_quiet(service)
        item = store.all()[0]
        self.assertEqual((item.status, item.state["mode"]), (COMMITTED, "original_only"), item.state.get("reason"))
        self.assertEqual(len(client.versions(item.state["doc_id"])), 1)
        note = self.api.get(f"{self.base}/api/documents/{item.state['doc_id']}/notes/").json()[0]["note"]
        if OCR_OFF:
            self.assertIn("no search text", note)                                # nobody read this one
            self.assertNotIn("did its own OCR", note)

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
        client = Paperless(self.base, self.intake_token)
        doc_id = client.find_by_checksum(scan.sha256)
        self.assertIsNotNone(doc_id)
        version = numbered_letter(1, tag=uuid.uuid4().hex[:8])
        client.wait_for_task(client.update_version(doc_id, "manual-drop.searchable.pdf", version.pdf, label="e2e"))
        time.sleep(5)
        self.assertEqual(self.hooks.paths()[before:], ["/consume_started", "/document_added"])

    def test_a_scan_that_came_through_paperlesss_own_consume_folder_gets_its_searchable_version(self):
        """Every document stored before the app existed is one of these: a raw scan, upside down or not,
        that Paperless consumed itself. The loop gives it the same treatment as a scan arriving today,
        and changes nothing else about it."""
        settings, store, service, client, notifier = self.harness(loop=True)
        marker = "heronbridge" + uuid.uuid4().hex[:6]
        pages = [PageSpec([f"Synthetic old statement line {n}.{i} for testing purposes only" for i in range(1, 6)]
                          + [f"Reference {marker}"], footer=f"Page {n} of 3") for n in (1, 2, 3)]
        scan = make_scan(pages, flipped=True, turn=180)
        before_hooks = len(self.hooks.received)
        (self.tmp / "consume" / f"old-{marker}.pdf").write_bytes(scan.pdf)
        deadline = time.time() + 300
        doc_id = None
        while time.time() < deadline and not doc_id:
            time.sleep(3)
            doc_id = client.find_by_checksum(scan.sha256)
        self.assertIsNotNone(doc_id)
        deadline = time.time() + 60
        while time.time() < deadline and "/document_added" not in self.hooks.paths()[before_hooks:]:
            time.sleep(2)
        hooks = self.hooks.paths()[before_hooks:]
        before = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()
        self.assertEqual((len(before["versions"]), before["owner"]), (1, None))
        self.assertIn(self.tag, before["tags"])                                  # Jane's workflow tagged it
        if OCR_OFF:
            self.assertEqual(before["content"].strip(), "")                      # nobody had read it

        record = self.loop_until(service, doc_id)
        self.assertEqual((record.status, record.state["source"], record.state["kind"]), (DONE, "paperless", "scanned"),
                         record.state.get("reason"))
        after = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()
        self.assertEqual(len(after["versions"]), 2)
        for name in self.FIELDS_THAT_ARE_JANES:
            self.assertEqual(after.get(name), before.get(name), name)
        self.assertIn(marker, after["content"])
        self.assertLess(after["content"].index("line 1.1"), after["content"].index("line 3.1"))   # reading order
        stored = client.download_original(doc_id, doc_id)
        self.assertEqual(stored, scan.pdf)                                       # version 1 is still the raw scan
        notes = self.api.get(f"{self.base}/api/documents/{doc_id}/notes/").json()
        self.assertEqual(len(notes), 1)
        self.assertIn("This scan was already in Paperless, without a searchable version", notes[0]["note"])
        self.assertIn("3 of 3 pages were turned upright", notes[0]["note"])
        deadline = time.time() + 60
        while time.time() < deadline and doc_id not in self.search(marker):
            time.sleep(2)
        self.assertIn(doc_id, self.search(marker))
        time.sleep(5)
        self.assertEqual(self.hooks.paths()[before_hooks:], hooks)               # no ingest webhook fired again
        self.assertEqual([kind for kind, _ in notifier.sent], [])                # nothing said to Jane
        # Looked at again, it is left alone.
        self.documents._look_again_at = 0.0
        self.assertEqual(len(client.versions(doc_id)), 2)
        calls = self.google.calls
        service.step()
        self.assertEqual((len(client.versions(doc_id)), self.google.calls), (2, calls))

    def test_a_pdf_that_already_has_text_is_left_exactly_as_it_is(self):
        """A document with the tag whose file already has text inside it, as when a scanner is set to read
        the pages itself: there is nothing to read off the page images."""
        import types
        settings, store, service, client, notifier = self.harness(loop=True)
        marker = "ottercanal" + uuid.uuid4().hex[:6]
        scan = numbered_letter(2, tag=marker)
        # Make a PDF with text in it, without storing anything: the app's own searchable copy of a scan.
        # The file, Google's answers and the working files each get a folder of their own, as the app keeps them.
        scratch = settings.intake_dir / "scratch"
        source = scratch / "source" / "scan.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(scan.pdf)
        (scratch / "work").mkdir()
        answers = types.SimpleNamespace(google_dir=lambda: scratch / "google")
        made = service.pipeline.make_copy(source, lambda: self.backend(answers), lambda: scratch / "work",
                                          "a PDF with text")
        self.assertIsNotNone(made.searchable, made.why_not)
        doc_id = self.stored_by_other_means(made.searchable, f"statement-{marker}.pdf")
        before = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()
        record = self.loop_until(service, doc_id)
        self.assertEqual((record.status, record.state["kind"]), (NOT_NEEDED, "has_text"), record.state.get("reason"))
        after = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()
        self.assertEqual(len(after["versions"]), 1)
        for name in (*self.FIELDS_THAT_ARE_JANES, "content"):
            self.assertEqual(after.get(name), before.get(name), name)
        self.assertEqual(self.api.get(f"{self.base}/api/documents/{doc_id}/notes/").json(), [])
        self.assertIn(marker, after["content"])                                  # Paperless reads its text itself

    def test_a_document_that_belongs_to_another_user_is_seen_only_once_it_is_shared_with_the_app(self):
        """A scan that has an owner, and the Scanner tag. Paperless hides an owned document from
        every other user, so until it is shared with the app's user, or its owner is cleared, the app
        cannot know it exists. Shared with View and Change permission, it is picked up like any other,
        and stays its owner's."""
        settings, store, service, client, notifier = self.harness(loop=True)
        marker = "magpiequay" + uuid.uuid4().hex[:6]
        scan = numbered_letter(2, tag=marker)
        doc_id = self.stored_by_other_means(scan.pdf, f"janes-own-upload-{marker}.pdf", owner_cleared=False)
        owner = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()["owner"]
        self.assertIsNotNone(owner)
        self.assertNotIn(doc_id, [row["id"] for row in client.documents()])
        for _ in range(40):
            if not service.step():
                break
        self.assertFalse(self.documents.exists(doc_id))
        self.assertEqual(len(Paperless(self.base, self.token).versions(doc_id)), 1)
        # What a Paperless workflow with an "assign permissions" action would do for every new document.
        shared = {"view": {"users": [self.intake_id], "groups": []}, "change": {"users": [self.intake_id], "groups": []}}
        self.api.patch(f"{self.base}/api/documents/{doc_id}/", json={"set_permissions": shared}).raise_for_status()
        self.assertIn(doc_id, [row["id"] for row in client.documents()])
        record = self.loop_until(service, doc_id)
        self.assertEqual(record.status, DONE, record.state.get("reason"))
        after = self.api.get(f"{self.base}/api/documents/{doc_id}/").json()
        self.assertEqual((len(after["versions"]), after["owner"]), (2, owner))   # still hers
        self.assertIn(marker, after["content"])
        self.assertEqual(len(self.api.get(f"{self.base}/api/documents/{doc_id}/notes/").json()), 1)

    def test_a_scan_the_app_stored_is_recognised_even_if_the_apps_own_records_are_lost(self):
        settings, store, service, client, _ = self.harness()
        scan = numbered_letter(2, tag=uuid.uuid4().hex[:8])
        (settings.capture_dir / "20261101_110000_000700.pdf").write_bytes(scan.pdf)
        self.run_until_quiet(service)
        doc_id = store.all()[0].state["doc_id"]
        self.assertEqual(self.documents.load(doc_id).status, DONE)               # the hand-over, as the app runs it
        # A second, empty app: the Green after its app data was lost.
        settings, store, service, client, notifier = self.harness(loop=True)
        record = self.loop_until(service, doc_id)
        self.assertEqual((record.status, record.state["source"]), (DONE, "paperless"))
        self.assertEqual(len(client.versions(doc_id)), 2)                        # nothing was added
        self.assertEqual(len(self.api.get(f"{self.base}/api/documents/{doc_id}/notes/").json()), 1)

    def test_a_scan_that_is_half_way_in_is_left_to_the_scan(self):
        """One owner at a time, against real Paperless. The original is stored and the searchable version
        is not yet: the loop must recognise that document as the scan's and keep its hands off it."""
        settings, store, service, client, notifier = self.harness(loop=True)
        scan = numbered_letter(2, tag=uuid.uuid4().hex[:8])
        (settings.capture_dir / "20261101_120000_000800.pdf").write_bytes(scan.pdf)
        add_version = client.update_version

        def paperless_stops_answering(*args, **kwargs):
            raise PaperlessError("Paperless POST update_version returned HTTP 502")

        client.update_version = paperless_stops_answering
        service.claim_captures()
        self.assertTrue(service.step())
        item = store.all()[0]
        doc_id = item.state["doc_id"]
        self.assertEqual((item.status, item.state["retry_stage"]), (WAITING, "commit"))
        # How the loop tells: Paperless's list names the file each document holds.
        row = next(row for row in client.documents() if row["id"] == doc_id)
        root = next(version for version in row["versions"] if version["is_root"])
        self.assertEqual(root["checksum"], scan.sha256)
        for _ in range(40):                                                      # the loop gets its turns
            self.documents._look_again_at = 0.0
            if not service.step():
                break
        self.assertFalse(self.documents.exists(doc_id))
        self.assertEqual(len(client.versions(doc_id)), 1)
        client.update_version = add_version                                      # Paperless answers again
        service.pipeline.run(store.load(item.sha))
        self.assertEqual(store.load(item.sha).status, COMMITTED, store.load(item.sha).state.get("reason"))
        self.assertEqual(len(client.versions(doc_id)), 2)
        self.assertEqual(len(self.api.get(f"{self.base}/api/documents/{doc_id}/notes/").json()), 1)
        self.assertEqual(self.documents.load(doc_id).status, DONE)               # handed over, with nothing left to do
        self.documents._look_again_at = 0.0
        service.step()
        self.assertEqual(len(client.versions(doc_id)), 2)

    def test_the_same_file_by_another_route_and_then_by_the_scanner_is_two_documents(self):
        """A scan takes for its own only a document the app stored. The same file stored some other way stays
        someone else's document: its owner, tags and versions are not the scan's to change. And with no
        Scanner tag it is not the loop's either, though the app can see it."""
        if any(pair.startswith("PAPERLESS_CONSUMER_DELETE_DUPLICATES") for pair in PAPERLESS_ENV):
            self.skipTest("this Paperless refuses a file it already holds")
        settings, store, service, client, notifier = self.harness(loop=True)
        scan = numbered_letter(2, tag=uuid.uuid4().hex[:8])
        theirs = self.stored_by_other_means(scan.pdf, "uploaded-by-hand.pdf", owner_cleared=False, tagged=False)
        shared = {"view": {"users": [self.intake_id], "groups": []}, "change": {"users": [self.intake_id], "groups": []}}
        self.api.patch(f"{self.base}/api/documents/{theirs}/", json={"set_permissions": shared}).raise_for_status()
        self.assertIn(theirs, [row["id"] for row in client.documents()])         # in plain sight of the app
        self.give_the_loop_its_turns(service)
        self.assertFalse(self.documents.exists(theirs))
        before = self.api.get(f"{self.base}/api/documents/{theirs}/").json()
        self.assertEqual(len(before["versions"]), 1)
        (settings.capture_dir / "20261101_130000_000900.pdf").write_bytes(scan.pdf)   # the same bytes, scanned in
        self.run_until_quiet(service, rounds=40)
        item = store.all()[0]
        self.assertEqual(item.status, COMMITTED, item.state.get("reason"))
        self.assertNotEqual(item.state["doc_id"], theirs)                        # a document of its own
        after = self.api.get(f"{self.base}/api/documents/{theirs}/").json()
        for name in (*self.FIELDS_THAT_ARE_JANES, "versions"):
            self.assertEqual(after.get(name), before.get(name), name)            # theirs is untouched
        self.assertEqual(self.api.get(f"{self.base}/api/documents/{theirs}/notes/").json(), [])
        self.assertFalse(self.documents.exists(theirs))
        self.assertEqual(len(client.versions(item.state["doc_id"])), 2)

    def test_a_document_without_the_scanner_tag_is_left_alone_until_it_is_given_one(self):
        """The tag is the boundary, against real Paperless. The app asks Paperless only for documents with
        the tag, and this proves Paperless obeys that filter. A scanned file without the tag is not listed,
        fetched, read or added to, though the app can see it. Given the tag in Paperless, which is how a gap
        on one document is mended, it is taken on at the next look."""
        settings, store, service, client, notifier = self.harness(loop=True)
        marker = "ploverweir" + uuid.uuid4().hex[:6]
        scan = numbered_letter(2, tag=marker)
        doc_id = self.stored_by_other_means(scan.pdf, f"no-tag-{marker}.pdf", tagged=False)
        row = next(row for row in client.documents() if row["id"] == doc_id)
        self.assertNotIn(self.tag, row["tags"])
        self.assertIn(doc_id, [row["id"] for row in client.documents()])                    # the app can see it
        self.assertNotIn(doc_id, [row["id"] for row in client.documents(with_tag=self.tag)])  # but isn't sent it
        self.give_the_loop_its_turns(service)
        self.assertFalse(self.documents.exists(doc_id))
        self.assertEqual(len(client.versions(doc_id)), 1)
        self.assertEqual(self.api.get(f"{self.base}/api/documents/{doc_id}/notes/").json(), [])
        self.api.patch(f"{self.base}/api/documents/{doc_id}/", json={"tags": [self.tag]}).raise_for_status()
        row = next(row for row in client.documents() if row["id"] == doc_id)
        self.assertIn(self.tag, row["tags"])
        self.assertIn(doc_id, [row["id"] for row in client.documents(with_tag=self.tag)])
        record = self.loop_until(service, doc_id)
        self.assertEqual(record.status, DONE, record.state.get("reason"))
        self.assertEqual(len(client.versions(doc_id)), 2)
        self.assertEqual(len(self.api.get(f"{self.base}/api/documents/{doc_id}/notes/").json()), 1)

    def test_api_upload_without_the_intake_tag_signals_document_added(self):
        """Why the intake tag exists. If this starts failing, Paperless now honours sources on
        "document added", and the tag could go. Either way the main test shows no double signal."""
        before = len(self.hooks.received)
        client = Paperless(self.base, self.intake_token)
        scan = numbered_letter(1, tag=uuid.uuid4().hex[:8])
        client.wait_for_task(client.post_document("api-only.pdf", scan.pdf, tag_ids=[self.tag]))
        deadline = time.time() + 60
        while time.time() < deadline and "/document_added" not in self.hooks.paths()[before:]:
            time.sleep(2)
        self.assertEqual(self.hooks.paths()[before:], ["/document_added"])


if __name__ == "__main__":
    unittest.main()
