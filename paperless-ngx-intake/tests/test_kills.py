"""Killed at any moment, the app still takes every job to its end, once.

The harness counts every boundary a kill can fall on: each durable write (os.replace) and
move (os.rename), each call that changes something in Paperless, each webhook. It kills the
app at the first boundary, runs it to the end, and checks what is left; then at the second,
and so on, until a run passes every boundary without being killed. A kill is raised as an
exception nothing in the app catches, so no line after it runs, as with kill -9.

Two kinds of kill. The worker: the live service sees its worker die (exit -9) and says so to
the queue, which counts it against the job. The service: the whole app dies, and starts again
with a new run of the queue and its start-up recovery. That is never a try.

What must hold at the end, every time: the job is done; Paperless has exactly one document,
and on it exactly one searchable version and one note; Google's pages are counted once;
"started" and "finished" are paired (except in the one instant the app takes a webhook as
sent); no try was counted for a kill of the service; and nothing is left in doubt or leased.
"""

from __future__ import annotations

import os
import unittest
import unittest.mock
from pathlib import Path

from paperless_intake.documents import DONE
from paperless_intake.store import COMMITTED

from synth import make_scan
from test_documents import alert_kinds, letter
from test_pipeline import Harness

PAGES = 2
ROUNDS = 40


class Killed(BaseException):
    """The app, or its worker, dies here."""


class KillSwitch:
    def __init__(self):
        self.at = None
        self.count = 0
        self.fired = None

    def arm(self, at: int) -> None:
        self.at, self.count, self.fired = at, 0, None

    def tick(self, where: str) -> None:
        self.count += 1
        if self.at is not None and self.count == self.at:
            self.at, self.fired = None, where
            raise Killed(where)


class KillHarness(unittest.TestCase):
    def run_once(self, at: int, mode: str, scenario: str):
        h = Harness(self)
        switch = KillSwitch()
        h.paperless.on_effect = switch.tick
        h.notifier.on_send = switch.tick
        real_replace, real_rename = os.replace, os.rename

        def replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            switch.tick(f"write {Path(dst).name}")

        def rename(src, dst, *args, **kwargs):
            real_rename(src, dst, *args, **kwargs)
            switch.tick(f"move {Path(dst).name}")

        if scenario == "scan":
            h.drop(make_scan(letter(PAGES, "kill")))
            done = lambda: (h.store.all() and h.store.all()[0].status == COMMITTED        # noqa: E731
                            and h.documents.all() and h.documents.all()[0].status == DONE)
        else:
            doc_id = h.paperless.add_old_scan(make_scan(letter(PAGES, "kill")).pdf)
            done = lambda: h.documents.exists(doc_id) and h.documents.load(doc_id).status == DONE  # noqa: E731
        windows = set()
        worker_kill = False
        switch.arm(at)
        with unittest.mock.patch.object(os, "replace", replace), unittest.mock.patch.object(os, "rename", rename):
            for _ in range(ROUNDS):
                if done():
                    break
                try:
                    h.settle()
                except Killed:
                    if switch.fired.startswith("write "):      # killed after writing "sending", before it went
                        windows |= self.windows(h)
                    job = h.service.queue.running
                    if mode == "worker" and job is not None:
                        # Counted against the job only if it was still in hand: a worker that dies after
                        # writing the outcome has nothing left to fail.
                        worker_kill = worker_kill or self.in_hand(h, job)
                        h.service.queue.returned(job, -9)       # the live service saw its worker die
                    else:
                        h.restart()                             # the whole app died, and starts again
                    continue
                h.clock.advance(31 * 60)                        # past any wait a kill may have led to
        return h, switch, windows, worker_kill, done()

    @staticmethod
    def in_hand(h, job) -> bool:
        if job.kind == "scan":
            return h.store.load(job.record.sha).status in {"captured", "ready", "committing", "waiting"}
        return h.documents.load(job.record.doc_id).status == "working"

    @staticmethod
    def windows(h) -> set[str]:
        """The one instant per webhook in which the app takes it as sent: written as sending, no result yet."""
        found = set()
        for item in h.store.all():
            if item.state.get("started_sending") and item.state.get("started_sent") is None:
                found.add("started")
            if item.state.get("finished_sending") and not item.state.get("finished_sent"):
                found.add("finished")
        return found

    def check(self, h, switch, windows, worker_kill, finished, scenario, label):
        self.assertTrue(finished, f"{label}: the job never finished")
        docs = h.paperless.docs
        self.assertEqual(len(docs), 1, f"{label}: {len(docs)} documents in Paperless")
        [doc] = docs.values()
        self.assertEqual((len(doc["versions"]), len(doc["notes"])), (2, 1), f"{label}: versions and notes")
        counted = h.pipeline.usage.this_month()
        self.assertTrue(PAGES <= counted <= PAGES * h.google.calls, f"{label}: {counted} pages counted, "
                                                                    f"{h.google.calls} requests")
        if h.google.calls == 1:
            self.assertEqual(counted, PAGES, label)
        self.assertIsNone(h.pipeline.intent.read(), f"{label}: an upload left in doubt")
        for record in h.documents.all():
            self.assertNotIn("lease", record.state, label)
            tries = record.state.get("tries") or {}
            if not worker_kill:
                self.assertEqual(tries.get("count", 0), 0, f"{label}: a try was counted for a kill of the app")
            elif scenario == "document":
                self.assertEqual(tries.get("odd"), 1, f"{label}: the worker's death is one try, no more")
        for item in h.store.all():
            self.assertNotIn("lease", item.state, label)
            self.assertEqual(item.state.get("outbox") or [], [], f"{label}: an alert still owed")
            self.assertEqual(list(item.root.glob(".*.tmp")), [], f"{label}: temporary files left")
        started = [entry for entry in h.notifier.sent if entry[0] == "started"]
        ended = [entry for entry in h.notifier.sent if entry[0] == "finished"]
        if scenario == "scan":
            # Killed between writing "sending" and the webhook going out: taken as sent, and so never sent twice.
            expected = (0, 1) if "started" in windows else (1, 0) if "finished" in windows else (1, 1)
            self.assertEqual((len(started), len(ended)), expected, f"{label}: webhooks {h.notifier.sent}")
            self.assertEqual(alert_kinds(h), ["waiting", "resolved"] if worker_kill else [],
                             f"{label}: alerts")
        else:
            self.assertEqual(h.notifier.sent, [], label)

    def sweep(self, mode: str, scenario: str):
        at, fired = 1, []
        while True:
            h, switch, windows, worker_kill, finished = self.run_once(at, mode, scenario)
            label = f"{scenario}, {mode} killed at boundary {at} ({switch.fired})"
            if switch.fired is None:
                self.check(h, switch, windows, worker_kill, finished, scenario, f"{scenario}, never killed")
                break
            fired.append(switch.fired)
            try:
                self.check(h, switch, windows, worker_kill, finished, scenario, label)
            except AssertionError as error:
                raise AssertionError(f"{label}: {error}") from None
            at += 1
            self.assertLess(at, 400, "too many boundaries: the harness is lost")
        return fired

    def test_a_new_scan_killed_at_every_step_of_the_service(self):
        fired = self.sweep("service", "scan")
        self.assertIn("paperless post_document", fired)
        self.assertIn("paperless update_version", fired)
        self.assertIn("webhook started", fired)
        self.assertIn("webhook finished", fired)
        self.assertTrue(any(where.startswith("move ") for where in fired))

    def test_a_new_scan_whose_worker_is_killed_at_every_step(self):
        self.sweep("worker", "scan")

    def test_a_stored_scan_killed_at_every_step_of_the_service(self):
        fired = self.sweep("service", "document")
        self.assertIn("paperless update_version", fired)
        self.assertIn("paperless add_note", fired)

    def test_a_stored_scan_whose_worker_is_killed_at_every_step(self):
        self.sweep("worker", "document")


if __name__ == "__main__":
    unittest.main()
