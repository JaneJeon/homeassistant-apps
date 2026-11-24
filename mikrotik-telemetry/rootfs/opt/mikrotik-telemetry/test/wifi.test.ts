import assert from "node:assert/strict";
import { test } from "node:test";
import { parseWifiClients } from "../src/config.js";
import { emptyState, renderMetrics } from "../src/metrics.js";
import { WifiCounters } from "../src/wifi.js";

const alias = "test-client";
const index = "17";
const row = (receive: bigint, transmit: bigint, uptimeTicks: number, ifIndex = index) =>
  ({ client: alias, ifIndex, receive, transmit, uptimeTicks });

test("validate an optional, unique allowlist without leaking configured addresses in errors", () => {
  const genericMac = "02:00:00:00:00:01";
  assert.deepEqual(parseWifiClients(undefined), []);
  assert.deepEqual(parseWifiClients([{ name: alias, mac: genericMac }]),
    [{ name: alias, macIndex: "2.0.0.0.0.1" }]);
  assert.throws(() => parseWifiClients([{ name: alias, mac: genericMac }, { name: "other", mac: genericMac }]),
    { message: "Duplicate wifi_clients entry" });
  assert.throws(() => parseWifiClients([{ name: genericMac, mac: genericMac }]),
    { message: "Invalid option: wifi_clients" });
});

test("accumulate exact Counter64 deltas once per named client and interface", () => {
  const counters = new WifiCounters();
  const large = BigInt(Number.MAX_SAFE_INTEGER) + 50n;
  const first = row(large, 100n, 1000);
  const other = row(20n, 90n, 500, "19");
  counters.observe([first, other]);
  const next = row(large + 1n, 110n, 2000);
  assert.deepEqual(counters.observe([next, next, row(25n, 92n, 1500, "19")]), [
    { client: alias, ifIndex: index, receive: 1n, transmit: 10n },
    { client: alias, ifIndex: "19", receive: 5n, transmit: 2n },
  ]);
});

test("exclude absent and reset intervals, retaining prior totals on reappearance", () => {
  const counters = new WifiCounters();
  counters.observe([row(10n, 20n, 1000)]);
  counters.observe([row(13n, 27n, 2000)]);
  assert.deepEqual(counters.observe([]), []);
  const totals = { client: alias, ifIndex: index, receive: 3n, transmit: 7n };
  assert.deepEqual(counters.observe([row(1000n, 100n, 1500)]), [totals]);
  assert.deepEqual(counters.observe([row(1010n, 110n, 500)]), [totals]);
  assert.deepEqual(counters.observe([row(1n, 125n, 900)]), [totals]);
  assert.deepEqual(counters.observe([row(5n, 130n, 1200)]), [
    { ...totals, receive: 7n, transmit: 12n },
  ]);
});

test("publish directional Wi-Fi counters independently of WAN SNMP", () => {
  const state = emptyState();
  state.wifiTotals = [{ client: alias, ifIndex: index, receive: 16n, transmit: 81n }];
  const metrics = renderMetrics(state);
  assert.match(metrics, /mikrotik_wifi_client_receive_bytes_total\{client="test-client",if_index="17"\} 16/);
  assert.match(metrics, /mikrotik_wifi_client_transmit_bytes_total\{client="test-client",if_index="17"\} 81/);
  assert.doesNotMatch(metrics, /mikrotik_wan_receive_octets_total/);
});
