import type { WifiAssociation, WifiInterface, WifiTotals } from "./wifi.js";

export type PingOutcome = "reply" | "no_reply_by_deadline" | "local_error";

export interface SnmpSample {
  wanReceiveOctets?: string;
  wanTransmitOctets?: string;
  cpuLoadPercent: Map<number, number>;
  memoryUsedBytes?: number;
  memoryTotalBytes?: number;
  dhcpLeases?: number;
  uptimeSeconds?: number;
}

export interface State {
  ping: Record<PingOutcome, number>;
  pingReplySeconds: number;
  timedReplies: number;
  snmpUp: boolean;
  snmpLastSuccessSeconds: number;
  snmpLastDurationSeconds: number;
  snmpPollInProgress: boolean;
  sample: SnmpSample | null;
  wifiTotals: WifiTotals[];
  /** Associations read by the latest Wi-Fi poll; empty after a failure, so link gauges disappear. */
  wifiStations: WifiAssociation[];
  /** Rows of the latest successful interface walk; empty after a failure. */
  wifiInterfaces: WifiInterface[];
  routerosLogs: {
    accepted: number;
    forwarded: number;
    dropped: Record<"invalid_syslog" | "queue_full" | "unauthorized_source" | "forward_failed", number>;
    queued: number;
  };
}

export function emptyState(): State {
  return { ping: { reply: 0, no_reply_by_deadline: 0, local_error: 0 }, pingReplySeconds: 0, timedReplies: 0, snmpUp: false,
    snmpLastSuccessSeconds: 0, snmpLastDurationSeconds: 0, snmpPollInProgress: false, sample: null, wifiTotals: [], wifiStations: [], wifiInterfaces: [],
    routerosLogs: { accepted: 0, forwarded: 0, dropped: { invalid_syslog: 0, queue_full: 0, unauthorized_source: 0, forward_failed: 0 }, queued: 0 } };
}

/** Prometheus text exposition escaping: backslash, double quote and line feed only. */
export function escapeLabel(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n");
}

class TextMetrics {
  private families = new Map<string, { help: string; type: "counter" | "gauge"; samples: string[] }>();
  add(name: string, help: string, type: "counter" | "gauge", value: number | string, labels: Record<string, string> = {}): void {
    if (typeof value === "number" && !Number.isFinite(value)) return;
    const tags = Object.entries(labels).map(([key, label]) => `${key}="${escapeLabel(label)}"`).join(",");
    let family = this.families.get(name);
    if (!family) { family = { help, type, samples: [] }; this.families.set(name, family); }
    if (family.help !== help || family.type !== type) throw new Error("Inconsistent metric definition");
    family.samples.push(`${name}${tags ? `{${tags}}` : ""} ${value}`);
  }
  text(): string { return [...this.families.entries()].flatMap(([name, family]) => [`# HELP ${name} ${family.help}`, `# TYPE ${name} ${family.type}`, ...family.samples]).join("\n") + "\n"; }
}

export function renderMetrics(state: State): string {
  const out = new TextMetrics();
  for (const outcome of ["reply", "no_reply_by_deadline", "local_error"] as const)
    out.add("mikrotik_ping_outcomes_total", "Completed one-second ping process outcomes since startup.", "counter", state.ping[outcome], { outcome });
  out.add("mikrotik_ping_reply_seconds_total", "Sum of measured ICMP reply round-trip times in seconds.", "counter", state.pingReplySeconds);
  out.add("mikrotik_ping_timed_replies_total", "ICMP replies with a measured round-trip time.", "counter", state.timedReplies);
  out.add("mikrotik_snmp_up", "1 when the most recent SNMP poll completed its interface lookup; stale samples are omitted after failures.", "gauge", Number(state.snmpUp));
  out.add("mikrotik_snmp_last_success_timestamp_seconds", "Unix time of the latest successful SNMP interface lookup, or 0.", "gauge", state.snmpLastSuccessSeconds);
  out.add("mikrotik_snmp_poll_duration_seconds", "Duration of the latest completed SNMP poll.", "gauge", state.snmpLastDurationSeconds);
  out.add("mikrotik_snmp_poll_in_progress", "1 while an independent SNMP poll is active.", "gauge", Number(state.snmpPollInProgress));
  out.add("mikrotik_routeros_logs_accepted_total", "Valid RouterOS syslog datagrams accepted from the configured router since startup.", "counter", state.routerosLogs.accepted);
  out.add("mikrotik_routeros_logs_forwarded_total", "RouterOS log records accepted by VictoriaLogs since startup.", "counter", state.routerosLogs.forwarded);
  for (const [reason, count] of Object.entries(state.routerosLogs.dropped))
    out.add("mikrotik_routeros_logs_dropped_total", "RouterOS syslog records discarded since startup.", "counter", count, { reason });
  out.add("mikrotik_routeros_logs_queue_length", "RouterOS log records waiting in the bounded forwarding queue.", "gauge", state.routerosLogs.queued);
  for (const { client, ifIndex, receive, transmit } of state.wifiTotals) {
    const labels = { client, if_index: ifIndex };
    out.add("mikrotik_wifi_client_receive_bytes_total", "Observed Wi-Fi bytes from station to AP.", "counter", receive.toString(), labels);
    out.add("mikrotik_wifi_client_transmit_bytes_total", "Observed Wi-Fi bytes from AP to station.", "counter", transmit.toString(), labels);
  }
  // RouterOS types the association uptime as TimeTicks but counts whole seconds in it: on the live hAP ax3
  // (RouterOS 7.24.7) the value rose by 2430 over 2424 seconds, and matched the uptime the RouterOS API reports.
  for (const { client, ifIndex, uptimeTicks, signal, band, txRate, rxRate } of state.wifiStations) {
    const labels = { client, if_index: ifIndex };
    out.add("mikrotik_wifi_client_uptime_seconds", "Time the station has been associated, from mtxrWifiRegistrationUptime.", "gauge", uptimeTicks, labels);
    if (signal !== undefined) out.add("mikrotik_wifi_client_signal_dbm", "Station signal strength from mtxrWifiRegistrationSignal.", "gauge", signal, labels);
    if (txRate !== undefined) out.add("mikrotik_wifi_client_tx_rate_bits_per_second", "AP to station link rate from mtxrWifiRegistrationTxRate, as reported (unit not stated by the MIB).", "gauge", txRate, labels);
    if (rxRate !== undefined) out.add("mikrotik_wifi_client_rx_rate_bits_per_second", "Station to AP link rate from mtxrWifiRegistrationRxRate, as reported (unit not stated by the MIB).", "gauge", rxRate, labels);
    if (band !== undefined) out.add("mikrotik_wifi_client_band_info", "Wireless band of the association from mtxrWifiRegistrationBand.", "gauge", 1, { ...labels, band });
  }
  for (const { index, name, ssid, channel, frequencyMhz } of state.wifiInterfaces) {
    out.add("mikrotik_wifi_interface_info", "Router Wi-Fi interface from mtxrWifiInterfaces.", "gauge", 1, { interface: name, ssid, channel, index });
    if (frequencyMhz !== undefined) out.add("mikrotik_wifi_interface_frequency_mhz", "Router Wi-Fi interface frequency from mtxrWifiInterfacesFreq.", "gauge", frequencyMhz, { interface: name });
  }
  if (!state.snmpUp || !state.sample) return out.text();
  const sample = state.sample;
  if (sample.wanReceiveOctets) out.add("mikrotik_wan_receive_octets_total", "WAN interface received octets from ifHCInOctets.", "counter", sample.wanReceiveOctets);
  if (sample.wanTransmitOctets) out.add("mikrotik_wan_transmit_octets_total", "WAN interface transmitted octets from ifHCOutOctets.", "counter", sample.wanTransmitOctets);
  for (const [core, load] of sample.cpuLoadPercent)
    out.add("mikrotik_cpu_load_percent", "Processor load reported by HOST-RESOURCES-MIB.", "gauge", load, { core: String(core) });
  if (sample.memoryUsedBytes !== undefined) out.add("mikrotik_memory_bytes", "Router memory reported by HOST-RESOURCES-MIB.", "gauge", sample.memoryUsedBytes, { kind: "used" });
  if (sample.memoryTotalBytes !== undefined) out.add("mikrotik_memory_bytes", "Router memory reported by HOST-RESOURCES-MIB.", "gauge", sample.memoryTotalBytes, { kind: "total" });
  if (sample.dhcpLeases !== undefined) out.add("mikrotik_dhcp_leases", "DHCP lease count reported by MikroTik enterprise MIB.", "gauge", sample.dhcpLeases);
  if (sample.uptimeSeconds !== undefined) out.add("mikrotik_uptime_seconds", "Router uptime in seconds from sysUpTime.", "gauge", sample.uptimeSeconds);
  return out.text();
}
