import assert from "node:assert/strict";
import { test } from "node:test";
import { parseWifiClients } from "../src/config.js";
import * as snmp from "net-snmp";
import { emptyState, escapeLabel, renderMetrics } from "../src/metrics.js";
import { displayString, wifiInterfacesFromWalk } from "../src/snmp.js";
import { recordWifiPoll, WifiCounters } from "../src/wifi.js";

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

const linked = (client: string, ifIndex = index) => ({
  ...row(100n, 200n, 123456, ifIndex), client, signal: -61, band: "5ghz-ax", txRate: 864, rxRate: 720,
});
const gaugeNames = ["signal_dbm", "tx_rate_bits_per_second", "rx_rate_bits_per_second", "band_info", "uptime_seconds"];

test("publish link gauges for an associated client with the counter labels", () => {
  const state = emptyState();
  recordWifiPoll(state, new WifiCounters(), [linked(alias)]);
  const metrics = renderMetrics(state);
  assert.match(metrics, /mikrotik_wifi_client_signal_dbm\{client="test-client",if_index="17"\} -61\n/);
  assert.match(metrics, /mikrotik_wifi_client_tx_rate_bits_per_second\{client="test-client",if_index="17"\} 864\n/);
  assert.match(metrics, /mikrotik_wifi_client_rx_rate_bits_per_second\{client="test-client",if_index="17"\} 720\n/);
  assert.match(metrics, /mikrotik_wifi_client_band_info\{client="test-client",if_index="17",band="5ghz-ax"\} 1\n/);
  assert.match(metrics, /mikrotik_wifi_client_uptime_seconds\{client="test-client",if_index="17"\} 123456\n/);
  assert.match(metrics, /mikrotik_wifi_client_receive_bytes_total\{client="test-client",if_index="17"\} 0\n/);
});

test("omit link gauges for a client that is not associated, and for fields the router did not return", () => {
  const state = emptyState();
  const counters = new WifiCounters();
  recordWifiPoll(state, counters, [linked(alias), linked("other")]);
  recordWifiPoll(state, counters, [{ ...row(1n, 2n, 50), client: "other" }]);
  const metrics = renderMetrics(state);
  for (const name of gaugeNames) assert.doesNotMatch(metrics, new RegExp(`mikrotik_wifi_client_${name}\\{client="test-client"`));
  assert.match(metrics, /mikrotik_wifi_client_uptime_seconds\{client="other",if_index="17"\} 50\n/);
  for (const name of gaugeNames.slice(0, 4)) assert.doesNotMatch(metrics, new RegExp(`mikrotik_wifi_client_${name}`));
});

test("drop every link gauge after a failed poll while counters keep their existing behaviour", () => {
  const state = emptyState();
  const counters = new WifiCounters();
  recordWifiPoll(state, counters, [linked(alias)]);
  recordWifiPoll(state, counters, undefined);
  assert.deepEqual(state.wifiStations, []);
  const metrics = renderMetrics(state);
  for (const name of gaugeNames) assert.doesNotMatch(metrics, new RegExp(`mikrotik_wifi_client_${name}`));
});

const text = (oid: string, value: string) => ({ oid: `1.3.6.1.4.1.14988.1.1.21.5.1.${oid}`, type: snmp.ObjectType.OctetString, value: Buffer.from(value) });

test("render router Wi-Fi interfaces from the interface table walk", () => {
  const state = emptyState();
  state.wifiInterfaces = wifiInterfacesFromWalk([
    text("2.1", "wifi1"), text("3.1", "home"), text("4.1", "5180"), text("5.1", "5180/ax/Ceee"),
    text("2.2", "wifi2"), text("3.2", "home"), text("4.2", "auto"),
    text("3.9", "no name, skipped"), { oid: "1.3.6.1.4.1.14988.1.1.21.4.1.4.1", type: snmp.ObjectType.TimeTicks, value: 5 },
  ]);
  assert.deepEqual(state.wifiInterfaces.map(({ index, frequencyMhz }) => [index, frequencyMhz]), [["1", 5180], ["2", undefined]]);
  assert.deepEqual(state.wifiInterfaces[1]?.raw, { col2: 'OctetString:"wifi2"', col3: 'OctetString:"home"', col4: 'OctetString:"auto"', col5: "missing" });
  const metrics = renderMetrics(state);
  assert.match(metrics, /mikrotik_wifi_interface_info\{interface="wifi1",ssid="home",channel="5180\/ax\/Ceee",index="1"\} 1\n/);
  assert.match(metrics, /mikrotik_wifi_interface_info\{interface="wifi2",ssid="home",channel="",index="2"\} 1\n/);
  assert.match(metrics, /mikrotik_wifi_interface_frequency_mhz\{interface="wifi1"\} 5180\n/);
  assert.doesNotMatch(metrics, /mikrotik_wifi_interface_frequency_mhz\{interface="wifi2"\}/);
});

test("escape odd SSID and channel text in labels", () => {
  const state = emptyState();
  state.wifiInterfaces = wifiInterfacesFromWalk([
    text("2.3", "wifi3"), text("3.3", 'café "5G" \\ net\n\u0007'), text("5.3", "36\\ \"x\""),
  ]);
  const metrics = renderMetrics(state);
  assert.ok(metrics.includes('mikrotik_wifi_interface_info{interface="wifi3",ssid="café \\"5G\\" \\\\ net",channel="36\\\\ \\"x\\"",index="3"} 1\n'));
  assert.equal(escapeLabel('a"b\\c\nd'), 'a\\"b\\\\c\\nd');
  assert.equal(displayString({ type: snmp.ObjectType.Integer, value: 4 }), undefined);
});
