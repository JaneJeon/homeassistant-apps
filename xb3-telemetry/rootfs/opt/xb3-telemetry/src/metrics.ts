import type { Collection } from "./collector.js";
import type { Channel } from "./types.js";

type Labels = Record<string, string | number>;
class TextMetrics {
  private families = new Map<
    string,
    { help: string; type: "gauge" | "counter"; samples: string[] }
  >();
  add(
    name: string,
    help: string,
    type: "gauge" | "counter",
    value: number | string,
    labels: Labels = {},
  ): void {
    if (typeof value === "number" && !Number.isFinite(value)) return;
    if (typeof value === "string" && !/^\d+$/.test(value))
      throw new Error("Invalid metric number");
    let family = this.families.get(name);
    if (!family) {
      family = { help, type, samples: [] };
      this.families.set(name, family);
    } else if (family.help !== help || family.type !== type)
      throw new Error("Inconsistent metric definition");
    const attributes = Object.entries(labels)
      .map(([key, val]) => `${key}=${JSON.stringify(String(val))}`)
      .join(",");
    family.samples.push(
      `${name}${attributes ? `{${attributes}}` : ""} ${value}`,
    );
  }
  text(): string {
    return (
      [...this.families.entries()]
        .flatMap(([name, family]) => [
          `# HELP ${name} ${family.help}`,
          `# TYPE ${name} ${family.type}`,
          ...family.samples,
        ])
        .join("\n") + "\n"
    );
  }
}

export interface LogQueryState {
  kind: string;
  timeFrame: string;
  inProgress: boolean;
  success: boolean | null;
  durationSeconds: number;
  lastSuccessSeconds: number;
  recordsReturned: number | null;
}

/** The only retained state is collection-health timestamps, not measurements. */
export class Metrics {
  private lastSuccess = new Map<string, number>();
  readonly logs = new Map<string, LogQueryState>();

  /** Commit acquisition status once, independently of HTTP rendering. */
  observe(collection: Collection): void {
    const n = collection.network.data;
    const h = collection.hardware.data;
    const groups: [string, boolean, number][] = [
      [
        "downstream",
        n?.downstream != null,
        collection.network.observedAtSeconds,
      ],
      ["upstream", n?.upstream != null, collection.network.observedAtSeconds],
      ["codewords", n?.codewords != null, collection.network.observedAtSeconds],
      [
        "uptime",
        n?.uptimeSeconds != null,
        collection.network.observedAtSeconds,
      ],
      [
        "internet_ui_status",
        n?.internetActive != null,
        collection.network.observedAtSeconds,
      ],
      [
        "gateway_clock",
        n?.gatewayTimeRaw != null,
        collection.network.observedAtSeconds,
      ],
      [
        "modem_info",
        Boolean(n?.modem.model && n.modem.firmware && n.modem.hardwareVersion),
        collection.network.observedAtSeconds,
      ],
      ["dram", h?.dramReported != null, collection.hardware.observedAtSeconds],
      ["flash", h?.flashMb != null, collection.hardware.observedAtSeconds],
      [
        "lan_ports",
        collection.lan.data?.ports != null,
        collection.lan.observedAtSeconds,
      ],
    ];
    for (const [group, available, at] of groups)
      if (available) this.lastSuccess.set(group, at);
  }

  render(collection: Collection): string {
    const out = new TextMetrics();
    const gauge = (
      name: string,
      help: string,
      value: number,
      labels: Labels = {},
    ) => out.add(name, help, "gauge", value, labels);
    const quality = (
      collector: string,
      available: boolean,
      observedAt: number,
      reason?: string,
    ) => {
      gauge(
        "xb3_collection_success",
        "1 when the named group was usable at its latest completed observation. Check its original last-success time and page age.",
        Number(available),
        { collector },
      );
      gauge(
        "xb3_collection_last_success_timestamp_seconds",
        "Collector UTC time of the last usable group observation, or 0 if none.",
        this.lastSuccess.get(collector) ?? 0,
        { collector },
      );
      if (!available && reason)
        gauge(
          "xb3_collection_error",
          "Failure reason for the current observation. Missing values are not zero readings.",
          1,
          { collector, reason },
        );
    };
    for (const [page, result] of Object.entries({
      network: collection.network,
      hardware: collection.hardware,
      lan: collection.lan,
    })) {
      gauge(
        "xb3_page_fetch_success",
        "1 when the authenticated page was fetched and recognized.",
        Number(result.data !== null),
        { page },
      );
      gauge(
        "xb3_page_fetch_duration_seconds",
        "HTTP and parsing duration including any bounded authentication recovery.",
        result.durationSeconds,
        { page },
      );
      if (result.data !== null) {
        gauge(
          "xb3_page_observation_timestamp_seconds",
          "Original response-completion time, not backend acquisition time or Prometheus scrape time.",
          result.observedAtSeconds,
          { page },
        );
        gauge(
          "xb3_page_observation_age_seconds",
          "Age of the cached usable response. Backend acquisition age is unknown.",
          Math.max(
            0,
            result.observedAtMonotonicMs !== undefined
              ? (performance.now() - result.observedAtMonotonicMs) / 1000
              : Date.now() / 1000 - result.observedAtSeconds,
          ),
          { page },
        );
      }
      if (result.error)
        gauge(
          "xb3_page_fetch_error",
          "Bounded failure category for the current page fetch.",
          1,
          { page, reason: result.error },
        );
    }
    gauge(
      "xb3_scrape_duration_seconds",
      "Duration of the latest background passive collection attempt, not this HTTP request.",
      collection.durationSeconds,
    );

    const network = collection.network.data;
    const netAt = collection.network.observedAtSeconds;
    const rf = (direction: string, channels: Channel[] | null | undefined) => {
      quality(
        direction,
        channels != null,
        netAt,
        network?.groupErrors[direction] ??
          collection.network.error ??
          undefined,
      );
      if (!channels) return;
      gauge(
        "xb3_locked_channels",
        "Count of locked channels in the current usable table.",
        channels.filter((c) => c.locked).length,
        { direction },
      );
      for (const channel of channels) {
        const labels = { direction, slot: channel.slot };
        gauge(
          "xb3_channel_locked",
          "Gateway-reported lock state. Slot is a visible UI position, not a DOCSIS channel ID.",
          Number(channel.locked),
          labels,
        );
        if (channel.frequencyMHz !== null)
          gauge(
            "xb3_channel_frequency_hertz",
            "RF frequency at the current UI slot.",
            channel.frequencyMHz * 1e6,
            labels,
          );
        if (channel.powerDbmv !== null)
          gauge(
            "xb3_channel_power_dbmv",
            "Gateway-reported downstream receive or upstream transmit power.",
            channel.powerDbmv,
            labels,
          );
        if (channel.snrDb !== null)
          gauge(
            "xb3_downstream_snr_db",
            "Downstream signal-to-noise reading reported by the gateway.",
            channel.snrDb,
            { slot: channel.slot },
          );
        if (channel.modulation)
          gauge(
            "xb3_channel_modulation_info",
            "Modulation string reported at the current RF UI slot.",
            1,
            { ...labels, modulation: channel.modulation },
          );
      }
    };
    rf("downstream", network?.downstream);
    rf("upstream", network?.upstream);
    quality(
      "codewords",
      network?.codewords != null,
      netAt,
      network?.groupErrors.codewords ?? collection.network.error ?? undefined,
    );
    for (const slot of network?.codewords ?? []) {
      for (const kind of [
        "unerrored",
        "correctable",
        "uncorrectable",
      ] as const) {
        out.add(
          "xb3_downstream_codewords_total",
          "Raw gateway cumulative counts by codeword UI slot. Decreases and slot changes require care when interpreting rates.",
          "counter",
          slot[kind],
          { slot: slot.slot, kind },
        );
      }
    }
    quality(
      "uptime",
      network?.uptimeSeconds != null,
      netAt,
      network?.groupErrors.uptime ?? collection.network.error ?? undefined,
    );
    if (network?.uptimeSeconds != null)
      gauge(
        "xb3_uptime_seconds",
        "Positive gateway-reported uptime. Missing or rendered default zero is omitted; clock availability is independent.",
        network.uptimeSeconds,
      );
    quality(
      "internet_ui_status",
      network?.internetActive != null,
      netAt,
      network?.groupErrors.internet_ui_status ??
        collection.network.error ??
        undefined,
    );
    quality(
      "gateway_clock",
      network?.gatewayTimeRaw != null,
      netAt,
      network?.groupErrors.clock ?? collection.network.error ?? undefined,
    );
    if (network?.internetActive != null)
      gauge(
        "xb3_internet_ui_active",
        "UI displays Active (1) or Inactive (0). Missing backend data can produce Inactive; bridge-static mode can force Active. Not a reachability test.",
        Number(network.internetActive),
      );
    const modem = network?.modem;
    const infoValid = Boolean(
      modem?.model && modem.firmware && modem.hardwareVersion,
    );
    quality(
      "modem_info",
      infoValid,
      netAt,
      network?.groupErrors.modem ?? collection.network.error ?? undefined,
    );
    if (infoValid)
      gauge(
        "xb3_info",
        "Current nonempty gateway model and software metadata.",
        1,
        {
          model: modem!.model!,
          software_version: modem!.firmware!,
          hardware_revision: modem!.hardwareVersion!,
        },
      );

    const hardware = collection.hardware.data;
    const hardwareAt = collection.hardware.observedAtSeconds;
    quality(
      "dram",
      hardware?.dramReported != null,
      hardwareAt,
      hardware?.groupErrors.dram ?? collection.hardware.error ?? undefined,
    );
    if (hardware?.dramReported)
      for (const quantity of ["total", "used", "available"] as const) {
        gauge(
          "xb3_dram_reported_value",
          "Raw DRAM UI numbers. The displayed unit is unverified; available maps to the upstream Free field, not established MemAvailable.",
          hardware.dramReported[quantity],
          {
            quantity,
            reported_unit: hardware.dramReported.unit,
            unit_verified: "false",
          },
        );
      }
    quality(
      "flash",
      hardware?.flashMb != null,
      hardwareAt,
      hardware?.groupErrors.flash ?? collection.hardware.error ?? undefined,
    );
    if (hardware?.flashMb)
      for (const quantity of ["total", "used", "available"] as const) {
        gauge(
          "xb3_flash_reported_megabytes",
          "Flash values as reported by the UI. Blank operands and zero total are rejected.",
          hardware.flashMb[quantity],
          { quantity },
        );
      }
    const lan = collection.lan.data;
    quality(
      "lan_ports",
      lan?.ports != null,
      collection.lan.observedAtSeconds,
      lan?.groupErrors.lan ?? collection.lan.error ?? undefined,
    );
    for (const port of lan?.ports ?? []) {
      gauge(
        "xb3_lan_link_active",
        "Gateway-reported Ethernet port state. The HA port has not been independently mapped.",
        Number(port.active),
        { port: port.port },
      );
      if (port.speedMbps !== null)
        gauge(
          "xb3_lan_link_speed_bits_per_second",
          "Current Ethernet link rate. Not Applicable is omitted, not zero.",
          port.speedMbps * 1e6,
          { port: port.port },
        );
    }
    for (const state of this.logs.values()) {
      const labels = { kind: state.kind, time_frame: state.timeFrame };
      gauge(
        "xb3_log_query_in_progress",
        "A log request is running in a separate authenticated session.",
        Number(state.inProgress),
        labels,
      );
      if (state.success !== null)
        gauge(
          "xb3_log_query_success",
          "Last completed log query returned a valid array, including a valid empty array.",
          Number(state.success),
          labels,
        );
      gauge(
        "xb3_log_query_duration_seconds",
        "Last completed log request and parsing duration.",
        state.durationSeconds,
        labels,
      );
      gauge(
        "xb3_log_query_last_success_timestamp_seconds",
        "Collector UTC time of the last valid log query.",
        state.lastSuccessSeconds,
        labels,
      );
      if (state.recordsReturned !== null && state.success)
        gauge(
          "xb3_log_records_returned",
          "Rows in the last query snapshot. This is not a count of unique new events.",
          state.recordsReturned,
          labels,
        );
    }
    const cpu = process.cpuUsage();
    out.add(
      "xb3_exporter_cpu_seconds_total",
      "Exporter process CPU time, not gateway CPU utilization.",
      "counter",
      (cpu.user + cpu.system) / 1e6,
    );
    gauge(
      "xb3_exporter_resident_memory_bytes",
      "Exporter process resident memory, not gateway memory.",
      process.memoryUsage().rss,
    );
    return out.text();
  }
}
