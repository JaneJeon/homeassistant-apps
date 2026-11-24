import assert from "node:assert/strict";
import test from "node:test";
import { parseRouterosSyslog } from "../src/routeros-logs.js";

test("decodes RouterOS BSD syslog fields while recording receipt time", () => {
  const record = parseRouterosSyslog(
    Buffer.from("<30>Nov 24 09:14:15 router.lan firewall,info accepted connection"),
    "192.0.2.1", new Date("2026-11-24T17:14:16.000Z"),
  );
  assert.deepEqual(record, {
    message: "firewall,info accepted connection", timestamp: "2026-11-24T17:14:16.000Z",
    source: "routeros", router: "router.lan", router_ip: "192.0.2.1", routeros_timestamp: "Nov 24 09:14:15",
    routeros_hostname: "router.lan", syslog_priority: 30, syslog_facility: 3, syslog_severity: 6,
  });
});

test("rejects malformed input and strips unsafe controls without corrupting valid message text", () => {
  assert.equal(parseRouterosSyslog(Buffer.from("not syslog"), "192.0.2.1"), null);
  const record = parseRouterosSyslog(Buffer.from("<13>Nov  4 01:02:03 r1 allow\u0000 this\n"), "192.0.2.1", new Date(0));
  assert.equal(record?.message, "allow this");
  assert.equal(record?.router, "r1");
});
