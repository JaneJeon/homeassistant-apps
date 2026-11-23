"""Publish the app's own numbers to Home Assistant over MQTT, with discovery. Best effort, always.

The broker. The manifest asks for the MQTT service (config.yaml, services: mqtt:want). The
Supervisor then answers GET http://supervisor/services/mqtt, with the app's own
SUPERVISOR_TOKEN, with the broker's address and a login (supervisor/api/services.py;
/services is open to every app, supervisor/api/middleware/security.py). Without a broker,
or with one that is down, the app carries on and asks again every few minutes.

Threads. paho's network loop runs in a thread of its own (loop_start), and the publisher in
another, so the connection stays up while the service waits hours on a worker, and nothing
here can hold up a job. The state is published retained on every change it is told of and
every five minutes, recomputed from the records each time. The availability topic has a last
will, so if the app dies every entity shows unavailable. On a clean stop the app says offline
itself. Only the service process loads this: a worker never connects.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable

import requests

from . import entities

log = logging.getLogger(__name__)

SUPERVISOR = "http://supervisor"
EVERY_SECONDS = 300.0
ASK_AGAIN_SECONDS = 300.0


def broker_from_supervisor(*, url: str = SUPERVISOR, token: str | None = None,
                           session: requests.Session | None = None) -> dict | None:
    """The broker's address and login, or None when there is no Supervisor, no MQTT service, or no answer."""
    token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        response = (session or requests).get(f"{url}/services/mqtt", headers={"Authorization": f"Bearer {token}"},
                                             timeout=10)
        body = response.json()
    except (requests.RequestException, ValueError):
        return None
    if body.get("result") != "ok":
        log.info("no MQTT broker from the Supervisor: %s", body.get("message") or body.get("result"))
        return None
    data = body.get("data") or {}
    if not data.get("host"):
        return None
    return {"host": data["host"], "port": int(data.get("port") or 1883), "username": data.get("username"),
            "password": data.get("password"), "ssl": bool(data.get("ssl")), "protocol": data.get("protocol") or "3.1.1"}


def paho_client(config: dict):
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion

    protocol = MQTTProtocolVersion.MQTTv31 if config.get("protocol") == "3.1" else MQTTProtocolVersion.MQTTv311
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="paperless-ngx-scanner", protocol=protocol)
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    if config.get("ssl"):
        client.tls_set()
    return client


class Publisher:
    def __init__(self, state: Callable[[], dict], *, broker: Callable[[], dict | None] = broker_from_supervisor,
                 client_factory: Callable[[dict], object] = paho_client, every: float = EVERY_SECONDS,
                 ask_again: float = ASK_AGAIN_SECONDS, monotonic: Callable[[], float] = time.monotonic):
        self.state = state
        self.broker = broker
        self.client_factory = client_factory
        self.every = every
        self.ask_again = ask_again
        self.monotonic = monotonic
        self.client = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._connected = threading.Event()
        self._fresh = False
        self._asked_at: float | None = None
        self._since: float | None = None
        self._said_no_broker = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mqtt-publisher", daemon=True)
        self._thread.start()

    def poke(self) -> None:
        """Something may have changed: publish soon. Never blocks."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        """Say offline, then disconnect. The last will says the same if the app dies instead."""
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        client = self.client
        if client is None:
            return
        try:
            if self._connected.is_set():
                info = client.publish(entities.AVAILABILITY_TOPIC, entities.OFFLINE, qos=1, retain=True)
                info.wait_for_publish(timeout)
            client.disconnect()
            client.loop_stop()
        except Exception:
            log.debug("MQTT stop", exc_info=True)

    # The publisher's own thread -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self._once()
            except Exception:
                log.exception("couldn't publish to Home Assistant; trying again later")
            self._wake.wait(self.every)
            self._wake.clear()

    def _once(self) -> None:
        now = self.monotonic()
        if self.client is not None and not self._connected.is_set() and self._since is not None \
                and now - self._since >= self.ask_again:
            # Not connected for a while: the broker may have a new login (the Mosquitto app makes new ones). Ask again.
            self._drop()
        if self.client is None:
            if self._asked_at is not None and now - self._asked_at < self.ask_again:
                return
            self._asked_at = now
            config = self.broker()
            if config is None:
                if not self._said_no_broker:
                    log.info("no MQTT broker; the app's numbers aren't published to Home Assistant. "
                             "Asking again every %d minutes", int(self.ask_again // 60))
                    self._said_no_broker = True
                return
            self._said_no_broker = False
            self._connect(config)
            return
        if not self._connected.is_set():
            return
        client = self.client
        if self._fresh:
            client.publish(entities.DISCOVERY_TOPIC, json.dumps(entities.discovery()), qos=1, retain=True)
            client.publish(entities.AVAILABILITY_TOPIC, entities.ONLINE, qos=1, retain=True)
            self._fresh = False
        client.publish(entities.STATE_TOPIC, json.dumps(self.state(), default=str), qos=1, retain=True)

    def _connect(self, config: dict) -> None:
        client = self.client_factory(config)
        if config.get("username"):
            client.username_pw_set(config["username"], config.get("password"))
        client.will_set(entities.AVAILABILITY_TOPIC, entities.OFFLINE, qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        self.client = client
        self._since = self.monotonic()
        client.connect_async(config["host"], int(config["port"]), keepalive=60)
        client.loop_start()
        log.info("publishing the app's numbers to Home Assistant through the MQTT broker at %s:%s",
                 config["host"], config["port"])

    def _drop(self) -> None:
        client, self.client = self.client, None
        self._connected.clear()
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            log.debug("MQTT drop", exc_info=True)

    # paho's network thread ----------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.warning("the MQTT broker refused the app: %s", reason_code)
            return
        self._connected.set()
        self._fresh = True              # discovery and "online" again: the broker may have lost them
        self._since = None
        self._wake.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected.clear()
        self._since = self.monotonic()
