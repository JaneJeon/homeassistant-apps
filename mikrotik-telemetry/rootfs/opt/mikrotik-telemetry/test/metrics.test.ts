import assert from "node:assert/strict";
import test from "node:test";
import { emptyState, renderMetrics } from "../src/metrics.js";
import { classifyPing } from "../src/ping.js";
import { decodeCounter64 } from "../src/snmp.js";

test("renders full unsigned Counter64 values without number coercion", () => {
  const state = emptyState();
  state.snmpUp = true;
  state.sample = { wanReceiveOctets: "18446744073709551615", wanTransmitOctets: "9007199254740993", cpuLoadPercent: new Map([[1, 17]]) };
  const text = renderMetrics(state);
  assert.match(text, /mikrotik_wan_receive_octets_total 18446744073709551615/);
  assert.match(text, /mikrotik_wan_transmit_octets_total 9007199254740993/);
  assert.equal((text.match(/# HELP mikrotik_cpu_load_percent/g) ?? []).length, 1);
});

test("omits stale SNMP data after a failed poll", () => {
  const state = emptyState();
  state.sample = { wanReceiveOctets: "1", cpuLoadPercent: new Map() };
  assert.doesNotMatch(renderMetrics(state), /wan_receive/);
});

test("decodes a network-order Counter64 buffer", () => {
  assert.equal(decodeCounter64(Buffer.from("ffffffffffffffff", "hex")), "18446744073709551615");
  assert.equal(decodeCounter64(Buffer.from("01ef", "hex")), "495");
  assert.equal(decodeCounter64(Buffer.from("00ffffffffffffffff", "hex")), "18446744073709551615");
  assert.equal(decodeCounter64(Buffer.alloc(10)), undefined);
});

test("classifies successful, deadline, and local ping outcomes", () => {
  assert.equal(classifyPing(0, "1 packets transmitted, 1 packets received, 0% packet loss"), "reply");
  assert.equal(classifyPing(1, "1 packets transmitted, 0 packets received, 100% packet loss"), "no_reply_by_deadline");
  assert.equal(classifyPing(1, "ping: permission denied"), "local_error");
  assert.equal(classifyPing(null, ""), "local_error");
});
