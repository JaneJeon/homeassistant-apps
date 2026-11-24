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
  snmpUp: boolean;
  snmpLastSuccessSeconds: number;
  snmpLastDurationSeconds: number;
  snmpPollInProgress: boolean;
  sample: SnmpSample | null;
}

export function emptyState(): State {
  return { ping: { reply: 0, no_reply_by_deadline: 0, local_error: 0 }, snmpUp: false,
    snmpLastSuccessSeconds: 0, snmpLastDurationSeconds: 0, snmpPollInProgress: false, sample: null };
}

class TextMetrics {
  private families = new Map<string, { help: string; type: "counter" | "gauge"; samples: string[] }>();
  add(name: string, help: string, type: "counter" | "gauge", value: number | string, labels: Record<string, string> = {}): void {
    if (typeof value === "number" && !Number.isFinite(value)) return;
    const tags = Object.entries(labels).map(([key, label]) => `${key}=${JSON.stringify(label)}`).join(",");
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
  out.add("mikrotik_snmp_up", "1 when the most recent SNMP poll completed its interface lookup; stale samples are omitted after failures.", "gauge", Number(state.snmpUp));
  out.add("mikrotik_snmp_last_success_timestamp_seconds", "Unix time of the latest successful SNMP interface lookup, or 0.", "gauge", state.snmpLastSuccessSeconds);
  out.add("mikrotik_snmp_poll_duration_seconds", "Duration of the latest completed SNMP poll.", "gauge", state.snmpLastDurationSeconds);
  out.add("mikrotik_snmp_poll_in_progress", "1 while an independent SNMP poll is active.", "gauge", Number(state.snmpPollInProgress));
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
