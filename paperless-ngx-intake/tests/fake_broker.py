"""A small MQTT 3.1.1 broker for tests: enough for a client that publishes and has a last will.

It speaks CONNECT/CONNACK, PUBLISH (QoS 0 and 1, retained), PINGREQ and DISCONNECT. When a
client goes away without DISCONNECT it publishes that client's will, as a real broker does.
It records the connections and the retained messages so a test can look at them.
"""

from __future__ import annotations

import socket
import struct
import threading
import time


def _read_exactly(conn: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def _remaining_length(conn: socket.socket) -> int:
    multiplier, value = 1, 0
    while True:
        byte = _read_exactly(conn, 1)[0]
        value += (byte & 127) * multiplier
        if not byte & 128:
            return value
        multiplier *= 128


def _string(data: bytes, at: int) -> tuple[bytes, int]:
    (length,) = struct.unpack_from("!H", data, at)
    return data[at + 2:at + 2 + length], at + 2 + length


class FakeBroker:
    def __init__(self, *, refuse: bool = False):
        self.refuse = refuse
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.connects: list[dict] = []
        self.retained: dict[str, bytes] = {}
        self.published: list[tuple[str, bytes, bool]] = []
        self.clean_disconnects = 0
        self._lock = threading.Lock()
        self._open: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.server.close()
        for conn in list(self._open):
            try:
                conn.close()
            except OSError:
                pass

    def wait_for(self, check, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if check(self):
                    return True
            time.sleep(0.02)
        return False

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self._open.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _publish(self, topic: str, payload: bytes, retain: bool) -> None:
        with self._lock:
            self.published.append((topic, payload, retain))
            if retain:
                self.retained[topic] = payload

    def _serve(self, conn: socket.socket) -> None:
        will = None
        try:
            while True:
                header = _read_exactly(conn, 1)[0]
                kind, flags = header >> 4, header & 0x0F
                body = _read_exactly(conn, _remaining_length(conn))
                if kind == 1:                                        # CONNECT
                    _, at = _string(body, 0)
                    level, connect_flags = body[at], body[at + 1]
                    at += 4
                    client_id, at = _string(body, at)
                    record = {"client_id": client_id.decode(), "level": level, "will": None,
                              "username": None, "password": None}
                    if connect_flags & 0x04:
                        topic, at = _string(body, at)
                        message, at = _string(body, at)
                        will = record["will"] = {"topic": topic.decode(), "payload": message,
                                                 "qos": (connect_flags >> 3) & 3, "retain": bool(connect_flags & 0x20)}
                    if connect_flags & 0x80:
                        username, at = _string(body, at)
                        record["username"] = username.decode()
                    if connect_flags & 0x40:
                        password, at = _string(body, at)
                        record["password"] = password.decode()
                    with self._lock:
                        self.connects.append(record)
                    conn.sendall(bytes([0x20, 0x02, 0x00, 0x05 if self.refuse else 0x00]))
                    if self.refuse:
                        conn.close()
                        return
                elif kind == 3:                                      # PUBLISH
                    qos, retain = (flags >> 1) & 3, bool(flags & 1)
                    topic, at = _string(body, 0)
                    if qos:
                        (packet_id,) = struct.unpack_from("!H", body, at)
                        at += 2
                        conn.sendall(bytes([0x40, 0x02]) + struct.pack("!H", packet_id))
                    self._publish(topic.decode(), body[at:], retain)
                elif kind == 12:                                     # PINGREQ
                    conn.sendall(bytes([0xD0, 0x00]))
                elif kind == 14:                                     # DISCONNECT: no will
                    with self._lock:
                        self.clean_disconnects += 1
                    will = None
                    conn.close()
                    return
        except (ConnectionError, OSError):
            if will is not None:                                     # gone without a word: the will goes out
                self._publish(will["topic"], will["payload"], will["retain"])
