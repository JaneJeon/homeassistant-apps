"""The app's own numbers in Home Assistant: the entities, the values, and publishing them over MQTT."""

from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from paperless_intake import entities
from paperless_intake.documents import GAVE_UP, LATER, NEXT_RELEASE, NOT_A_PDF, Record
from paperless_intake.jobs import Hold, Job
from paperless_intake.mqtt import Publisher, broker_from_supervisor
from paperless_intake.report import Reachability, snapshot
from paperless_intake.store import COMMITTED

from fake_broker import FakeBroker
from synth import make_scan
from test_documents import letter, look
from test_pipeline import Harness
from test_queue import numbered_letter_missing_a_page

APP = Path(__file__).resolve().parent.parent / "rootfs" / "app"


class EntityTests(unittest.TestCase):
    def test_one_device_message_names_every_entity_with_its_fixed_id(self):
        message = entities.discovery()
        self.assertEqual(message["dev"]["name"], "Paperless-ngx Scanner")
        self.assertEqual(message["o"]["name"], "paperless-ngx-scanner")
        self.assertEqual((message["state_topic"], message["availability_topic"]),
                         ("paperless_ngx_scanner/state", "paperless_ngx_scanner/availability"))
        ids = {key: component["default_entity_id"] for key, component in message["cmps"].items()}
        self.assertEqual(ids, {
            "status": "sensor.paperless_ngx_scanner_status",
            "paused": "binary_sensor.paperless_ngx_scanner_paused",
            "paperless_reachable": "binary_sensor.paperless_ngx_scanner_paperless_reachable",
            "scans_held": "sensor.paperless_ngx_scanner_scans_held",
            "scans_retrying": "sensor.paperless_ngx_scanner_scans_retrying",
            "scans_failed": "sensor.paperless_ngx_scanner_scans_failed",
            "last_scan": "sensor.paperless_ngx_scanner_last_scan",
            "google_pages_today": "sensor.paperless_ngx_scanner_google_pages_today",
            "google_pages_this_month": "sensor.paperless_ngx_scanner_google_pages_this_month",
            "stored_to_reread": "sensor.paperless_ngx_scanner_stored_to_reread",
            "stored_set_aside": "sensor.paperless_ngx_scanner_stored_set_aside",
        })
        names = {key: component["name"] for key, component in message["cmps"].items()}
        self.assertEqual(names["scans_retrying"], "Scans retrying")
        self.assertEqual(names["stored_to_reread"], "Stored scans to re-read")
        cmps = message["cmps"]
        self.assertEqual((cmps["status"]["device_class"], cmps["status"]["options"]),
                         ("enum", ["Idle", "Scanning", "Re-reading", "Waiting"]))
        self.assertEqual((cmps["paused"]["p"], cmps["paused"]["device_class"]), ("binary_sensor", "problem"))
        self.assertEqual(cmps["paperless_reachable"]["device_class"], "connectivity")
        self.assertEqual(cmps["last_scan"]["device_class"], "timestamp")
        for key in ("scans_held", "scans_retrying", "scans_failed", "stored_to_reread", "stored_set_aside"):
            self.assertEqual((cmps[key]["unit_of_measurement"], cmps[key]["state_class"]), ("scans", "measurement"))
        for key in ("google_pages_today", "google_pages_this_month"):
            self.assertEqual((cmps[key]["unit_of_measurement"], cmps[key]["state_class"]), ("pages", "total_increasing"))
        self.assertEqual(len({component["unique_id"] for component in cmps.values()}), len(cmps))
        self.assertNotIn("entity_category", json.dumps(message))
        for key in ("status", "paused", "scans_held", "google_pages_today", "stored_set_aside"):
            self.assertEqual(cmps[key]["json_attributes_topic"], "paperless_ngx_scanner/state")

    def test_a_binary_sensor_with_no_answer_yet_is_unknown(self):
        template = entities.discovery()["cmps"]["paperless_reachable"]["value_template"]
        self.assertIn("'None' if value_json.paperless_reachable is none", template)


class ValueTests(unittest.TestCase):
    """Every value is worked out from the records, at every publish."""

    def test_counts_lists_and_times_come_from_the_records(self):
        h = Harness(self, daily_page_cap=100)
        h.drop(make_scan(letter(2)))
        h.settle()
        h.drop(numbered_letter_missing_a_page(), name="20261101_100000_000401.pdf")
        h.settle()
        photo = h.paperless.add_old_scan(b"\x89PNG not a pdf", name="photo.png")
        look(h)
        state = snapshot(h.service, reachable=True)
        self.assertEqual((state["status"], state["paused"], state["paperless_reachable"]), ("Idle", False, True))
        self.assertEqual((state["scans_held"], state["scans_retrying"], state["scans_failed"]), (1, 0, 0))
        self.assertEqual(state["scans_held_attributes"]["scans"][0]["file"], "20261101_100000_000401.pdf")
        self.assertIn("missing", state["scans_held_attributes"]["scans"][0]["reason"])
        self.assertEqual((state["google_pages_today"], state["google_pages_this_month"]), (4, 4))
        self.assertEqual(state["google_pages_today_attributes"], {"cap": 100, "stored_scans_share": 75})
        self.assertTrue(state["last_scan"].endswith("+00:00"))
        self.assertEqual((state["stored_set_aside"], state["stored_to_reread"]), (1, 0))
        self.assertEqual(state["stored_set_aside_attributes"]["documents"],
                         [{"document": photo, "reason": NOT_A_PDF}])
        json.dumps(state)

    def test_stored_scans_to_reread_counts_the_paperless_list_and_earlier_releases_leftovers(self):
        h = Harness(self)
        self.assertIsNone(snapshot(h.service)["stored_to_reread"])        # unknown until Paperless has been asked
        for tag in ("a", "b", "c"):
            h.paperless.add_old_scan(make_scan(letter(1, tag)).pdf)
        h.service.queue.held = {"scan": None, "document": None}
        h.documents.discover(h.documents.all())                             # one look at Paperless's list
        self.assertEqual(snapshot(h.service)["stored_to_reread"], 3)
        Record(h.documents.dir / "1", {"doc_id": 1, "source": "paperless", "added": [], "tries": {}}).set(
            NEXT_RELEASE, by="1.3.2")                                       # left by an earlier release: still to go
        Record(h.documents.dir / "2", {"doc_id": 2, "source": "paperless", "added": [], "tries": {}}).set(
            GAVE_UP, by="1.3.2")
        state = snapshot(h.service)
        self.assertEqual((state["stored_to_reread"], state["stored_set_aside"]), (2, 1))

    def test_waiting_means_a_scan_is_held_back_and_paused_means_an_outside_reason(self):
        h = Harness(self)
        h.green(io=50)
        h.drop(make_scan(letter(1)))
        h.settle()
        state = snapshot(h.service)
        self.assertEqual((state["status"], state["paused"]), ("Waiting", True))
        self.assertIn("disk is saturated", state["status_attributes"]["reason"])
        self.assertEqual(state["status_attributes"]["waits_for"], "io")
        self.assertIn("disk is saturated", state["paused_attributes"]["reason"])
        self.assertEqual(state["scans_retrying"], 1)

    def test_a_stored_scan_paced_or_waiting_for_tomorrow_is_not_waiting_and_not_paused(self):
        h = Harness(self)
        queue = h.service.queue
        queue.held["document"] = Hold("a pause of 2 minutes between stored scans", "pace", None, h.clock() + 60, False)
        state = snapshot(h.service)
        self.assertEqual((state["status"], state["paused"]), ("Re-reading", False))
        queue.held["document"] = Hold("stored scans have used today's share", "pages", None, h.clock() + 60, False)
        state = snapshot(h.service)
        self.assertEqual((state["status"], state["paused"], state["status_attributes"]["reason"]),
                         ("Idle", False, "stored scans have used today's share"))
        queue.held["document"] = Hold("only 300 MiB of memory", "memory_low", "memory", h.clock() + 60, True)
        state = snapshot(h.service)
        self.assertEqual((state["status"], state["paused"]), ("Idle", True))
        queue.held["document"] = None
        queue.running = Job("scan", None)
        self.assertEqual(snapshot(h.service)["status"], "Scanning")

    def test_a_stored_scan_waiting_on_paperless_pauses(self):
        h = Harness(self)
        Record(h.documents.dir / "3", {"doc_id": 3, "added": [], "tries": {}}).set(
            LATER, reason="Paperless unreachable: ConnectionError", waits_on="paperless", not_before_unix=0)
        self.assertEqual(snapshot(h.service)["paused_attributes"]["reason"], "Paperless unreachable: ConnectionError")

    def test_reachable_asks_paperless_at_most_once_a_minute(self):
        asked, now = [], [0.0]
        reachable = Reachability(lambda: asked.append(1), monotonic=lambda: now[0])
        self.assertTrue(reachable())
        self.assertTrue(reachable())
        now[0] = 61
        reachable()
        self.assertEqual(len(asked), 2)

        def down():
            raise OSError("no")
        self.assertFalse(Reachability(down)())


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        self.addCleanup(self.broker.close)
        self.config = {"host": "127.0.0.1", "port": self.broker.port, "username": "addons", "password": "pw",
                       "ssl": False, "protocol": "3.1.1"}

    def publisher(self, state=None, **kwargs):
        publisher = Publisher(state or (lambda: {"status": "Idle"}), broker=lambda: self.config, **kwargs)
        publisher.start()
        self.addCleanup(publisher.stop, 1)
        return publisher

    def test_discovery_online_and_state_are_published_retained_with_a_last_will(self):
        values = {"status": "Idle", "scans_held": 0}
        publisher = self.publisher(lambda: dict(values))
        self.assertTrue(self.broker.wait_for(lambda b: entities.STATE_TOPIC in b.retained))
        connect = self.broker.connects[0]
        self.assertEqual((connect["username"], connect["password"], connect["level"]), ("addons", "pw", 4))
        self.assertEqual(connect["will"], {"topic": entities.AVAILABILITY_TOPIC, "payload": b"offline", "qos": 1,
                                           "retain": True})
        self.assertEqual(json.loads(self.broker.retained[entities.DISCOVERY_TOPIC])["dev"]["name"],
                         "Paperless-ngx Scanner")
        self.assertEqual(self.broker.retained[entities.AVAILABILITY_TOPIC], b"online")
        values["scans_held"] = 1
        publisher.poke()
        self.assertTrue(self.broker.wait_for(
            lambda b: json.loads(b.retained[entities.STATE_TOPIC])["scans_held"] == 1))
        publisher.stop(2)
        self.assertTrue(self.broker.wait_for(lambda b: b.retained[entities.AVAILABILITY_TOPIC] == b"offline"))
        self.assertEqual(self.broker.clean_disconnects, 1)

    def test_no_broker_is_only_a_wait_and_the_app_connects_once_there_is_one(self):
        configs = [None, self.config]
        publisher = Publisher(lambda: {"status": "Idle"}, broker=lambda: configs.pop(0) if len(configs) > 1 else configs[0],
                              ask_again=0.1, every=0.1)
        publisher.start()
        self.addCleanup(publisher.stop, 1)
        self.assertTrue(self.broker.wait_for(lambda b: entities.STATE_TOPIC in b.retained))

    def test_a_state_that_cant_be_worked_out_doesnt_stop_publishing(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("a record half-written")
            return {"status": "Idle"}
        publisher = self.publisher(flaky, every=0.1)
        self.assertTrue(self.broker.wait_for(lambda b: entities.STATE_TOPIC in b.retained))
        publisher.poke()

    def test_a_broker_that_refuses_the_app_changes_nothing_for_the_work(self):
        broker = FakeBroker(refuse=True)
        self.addCleanup(broker.close)
        config = dict(self.config, port=broker.port)
        publisher = Publisher(lambda: {"status": "Idle"}, broker=lambda: config, every=0.1)
        publisher.start()
        self.addCleanup(publisher.stop, 1)
        self.assertTrue(broker.wait_for(lambda b: len(b.connects) >= 1))
        h = Harness(self)
        h.service.poke = publisher.poke
        h.drop(make_scan(letter(1)))
        h.settle()
        self.assertEqual(h.only_item().status, COMMITTED)
        self.assertNotIn(entities.STATE_TOPIC, broker.retained)

    def test_the_last_will_says_offline_when_the_app_is_killed(self):
        script = (
            "import sys, time\n"
            "from paperless_intake.mqtt import Publisher\n"
            f"p = Publisher(lambda: {{'status': 'Idle'}}, broker=lambda: {self.config!r})\n"
            "p.start()\n"
            "time.sleep(60)\n"
        )
        child = subprocess.Popen([sys.executable, "-c", script], env=dict(os.environ, PYTHONPATH=str(APP)))
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertTrue(self.broker.wait_for(lambda b: b.retained.get(entities.AVAILABILITY_TOPIC) == b"online"))
        child.send_signal(signal.SIGKILL)
        child.wait()
        self.assertTrue(self.broker.wait_for(lambda b: b.retained.get(entities.AVAILABILITY_TOPIC) == b"offline"))
        self.assertEqual(self.broker.clean_disconnects, 0)


class SupervisorTests(unittest.TestCase):
    def serve(self, body: dict, token: str = "app-token"):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                ok = self.headers.get("Authorization") == f"Bearer {token}"
                data = json.dumps(body if ok else {"result": "error", "message": "unauthorized"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", seen

    def test_the_broker_login_comes_from_the_supervisor(self):
        url, seen = self.serve({"result": "ok", "data": {"host": "core-mosquitto", "port": 1883, "ssl": False,
                                                         "protocol": "3.1.1", "username": "addons", "password": "pw",
                                                         "addon": "core_mosquitto"}})
        config = broker_from_supervisor(url=url, token="app-token")
        self.assertEqual(config, {"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "pw",
                                  "ssl": False, "protocol": "3.1.1"})
        self.assertEqual(seen, [("/services/mqtt", "Bearer app-token")])

    def test_no_mqtt_service_no_token_or_no_supervisor_is_no_broker(self):
        url, _ = self.serve({"result": "error", "message": "Service not enabled"})
        self.assertIsNone(broker_from_supervisor(url=url, token="app-token"))
        self.assertIsNone(broker_from_supervisor(url=url, token=""))
        self.assertIsNone(broker_from_supervisor(url="http://127.0.0.1:9", token="app-token"))


if __name__ == "__main__":
    unittest.main()
