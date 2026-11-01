"""The service runs each item's work in a child process. These tests use a real one."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from paperless_intake.config import load
from paperless_intake.service import Service, SubprocessRunner
from paperless_intake.store import WAITING
from paperless_intake.wiring import build

from fake_paperless import RecordingNotifier
from synth import FONT, numbered_letter

APP = Path(__file__).resolve().parent.parent / "rootfs" / "app"


class WorkerProcessTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="intake-worker-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        options = self.root / "options.json"
        # No Google key: the child gets as far as "waiting for configuration" without any network.
        options.write_text(json.dumps({"paperless_token": "x", "paperless_url": "http://127.0.0.1:9",
                                       "google_service_account": "-", "ha_url": "http://127.0.0.1:9",
                                       "webhook_alert": "a"}))
        self.env = dict(os.environ, INTAKE_OPTIONS=str(options), INTAKE_CAPTURE_DIR=str(self.root / "capture"),
                        INTAKE_DIR=str(self.root / "intake"), INTAKE_FONT=str(FONT), INTAKE_STABLE_SECONDS="0",
                        PYTHONPATH=str(APP))
        saved = dict(os.environ)
        os.environ.update(self.env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        self.settings = load()
        self.settings.capture_dir.mkdir(parents=True)
        self.app = build(self.settings)
        self.clock = [1_800_000_000.0]
        self.notifier = RecordingNotifier()
        self.app.pipeline.notifier = self.notifier
        self.app.pipeline.clock = lambda: self.clock[0]

    def service(self, runner):
        return Service(self.settings, self.app.store, self.app.pipeline, self.notifier, runner=runner,
                       clock=lambda: self.clock[0])

    def claim(self, service):
        (self.settings.capture_dir / "scan.pdf").write_bytes(numbered_letter(1).pdf)
        service.claim_captures()
        self.clock[0] += 1
        service.claim_captures()
        [item] = self.app.store.all()
        return item

    def test_child_process_does_the_work_and_the_service_reloads_its_state(self):
        service = self.service(SubprocessRunner(env=self.env))
        item = self.claim(service)
        self.assertTrue(service.step())
        item = self.app.store.load(item.sha)
        self.assertEqual((item.status, item.state["reason_kind"]), (WAITING, "config"))
        self.assertTrue(any(e["event"] == "state" and e.get("status") == WAITING for e in item.events()))

    def test_a_child_killed_for_memory_leaves_the_scan_waiting_with_one_alert(self):
        killed = SubprocessRunner(command=(sys.executable, "-c", "import os, signal; os.kill(os.getpid(), 9)"))
        service = self.service(killed)
        item = self.claim(service)
        self.assertTrue(service.step())
        item = self.app.store.load(item.sha)
        self.assertEqual((item.status, item.state["reason_kind"], item.state["retry_stage"]),
                         (WAITING, "worker", "ocr"))
        self.assertIn("out of memory", item.state["reason"])
        self.assertFalse(service.step())                      # not due again until the retry time
        self.assertEqual([kind for kind, _ in self.notifier.sent if kind == "alert"], ["alert"])
        self.clock[0] += 31 * 60
        self.assertTrue(service.step())                       # retried once, killed again: no second alert
        self.assertEqual([kind for kind, _ in self.notifier.sent if kind == "alert"], ["alert"])
        self.assertFalse(service.step())                      # and rescheduled, not retried in a tight loop


if __name__ == "__main__":
    unittest.main()
