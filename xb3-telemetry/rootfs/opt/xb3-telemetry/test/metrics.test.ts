import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import {
  Collector,
  type Collection,
  type PageResult,
} from "../src/collector.js";
import { Metrics } from "../src/metrics.js";
import { GatewayError } from "../src/client.js";
import { parseHardware, parseLan, parseNetwork } from "../src/parser.js";
import type {
  HardwareSnapshot,
  LanSnapshot,
  NetworkSnapshot,
} from "../src/types.js";

const fixture = (name: string) =>
  readFileSync(
    fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)),
    "utf8",
  );
const partialNetworkHtml = fixture("network-partial.html");
const hardwareHtml = fixture("hardware.html");
const lanHtml = fixture("lan.html");

function page<T>(
  data: T | null,
  observedAtSeconds: number,
  error: string | null = null,
): PageResult<T> {
  return { data, durationSeconds: 0.02, observedAtSeconds, error };
}

function collection(
  network: PageResult<NetworkSnapshot>,
  hardware: PageResult<HardwareSnapshot>,
  lan: PageResult<LanSnapshot>,
): Collection {
  return { network, hardware, lan, durationSeconds: 0.1 };
}

function metricLine(output: string, prefix: string): string | undefined {
  return output.split("\n").find((line) => line.startsWith(prefix));
}

function render(metrics: Metrics, value: Collection): string {
  metrics.observe(value);
  return metrics.render(value);
}

function assertMetricFamiliesAreWellFormed(
  output: string,
): Map<string, string> {
  const families = new Map<
    string,
    { helpCount: number; typeCount: number; type?: string; samples: number }
  >();
  const closed = new Set<string>();
  let current: string | null = null;
  for (const line of output.split("\n").filter(Boolean)) {
    const help = line.match(/^# HELP (\S+) (.+)$/);
    if (help) {
      if (current !== null) closed.add(current);
      const name = help[1];
      assert.ok(name);
      assert.equal(
        closed.has(name),
        false,
        `${name} must not resume after another family starts`,
      );
      assert.equal(
        families.has(name),
        false,
        `${name} must have one HELP definition`,
      );
      families.set(name, { helpCount: 1, typeCount: 0, samples: 0 });
      current = name;
      continue;
    }

    const type = line.match(/^# TYPE (\S+) (gauge|counter)$/);
    if (type) {
      const name = type[1];
      assert.ok(name);
      assert.equal(
        current,
        name,
        `${name} TYPE must follow its HELP before samples`,
      );
      const family = families.get(name);
      assert.ok(family);
      assert.equal(
        family.typeCount,
        0,
        `${name} must have one TYPE definition`,
      );
      family.typeCount = 1;
      family.type = type[2];
      continue;
    }

    const sample = line.match(
      /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+\S+$/,
    );
    assert.ok(sample, `unexpected exposition line: ${line}`);
    assert.equal(
      current,
      sample[1],
      `${sample[1]} samples must be contiguous within their family`,
    );
    const family = families.get(sample[1]);
    assert.ok(family);
    assert.equal(family.typeCount, 1, `${sample[1]} TYPE must precede samples`);
    family.samples += 1;
  }

  for (const [name, family] of families) {
    assert.equal(family.helpCount, 1);
    assert.equal(family.typeCount, 1);
    assert.ok(family.samples > 0, `${name} must contain at least one sample`);
  }
  return new Map(
    [...families].map(([name, family]) => [name, family.type ?? ""]),
  );
}

describe("Metrics", () => {
  it("emits valid partial RF and counter zeros while omitting unavailable metadata", () => {
    const network = parseNetwork(partialNetworkHtml);
    const hardware = parseHardware(hardwareHtml);
    const lan = parseLan(lanHtml);
    const metrics = new Metrics();
    const rendered = render(
      metrics,
      collection(page(network, 1000), page(hardware, 1000), page(lan, 1000)),
    );

    assert.match(
      rendered,
      /xb3_channel_frequency_hertz\{direction="downstream",slot="1"\} 675000000/,
    );
    assert.match(
      rendered,
      /xb3_collection_success\{collector="downstream"\} 1/,
    );
    assert.match(
      rendered,
      /xb3_downstream_codewords_total\{slot="25",kind="unerrored"\} 0/,
    );
    assert.match(rendered, /# TYPE xb3_downstream_codewords_total counter/);
    assert.doesNotMatch(rendered, /^xb3_uptime_seconds(?:\{| )/m);
    assert.match(rendered, /xb3_collection_success\{collector="uptime"\} 0/);
  });

  it("renders one contiguous typed family per metric name without credential or hardware identifiers", () => {
    const network = parseNetwork(partialNetworkHtml);
    const hardware = parseHardware(hardwareHtml);
    const lan = parseLan(lanHtml);
    const rendered = render(
      new Metrics(),
      collection(page(network, 1000), page(hardware, 1000), page(lan, 1000)),
    );
    const types = assertMetricFamiliesAreWellFormed(rendered);
    assert.equal(types.get("xb3_downstream_codewords_total"), "counter");
    assert.equal(types.get("xb3_exporter_cpu_seconds_total"), "counter");
    assert.equal(types.get("xb3_collection_success"), "gauge");
    assert.equal(types.get("xb3_channel_frequency_hertz"), "gauge");
    assert.doesNotMatch(
      rendered,
      /\b(?:password|serial(?:_number)?|mac(?:_address)?)\b/i,
    );
  });

  it("advances last-success only when an observation is explicitly committed", () => {
    const network = parseNetwork(partialNetworkHtml);
    const hardware = parseHardware(hardwareHtml);
    const lan = parseLan(lanHtml);
    const metrics = new Metrics();
    const observed = collection(
      page(network, 1_000),
      page(hardware, 1_000),
      page(lan, 1_000),
    );

    const beforeObservation = metrics.render(observed);
    assert.match(
      beforeObservation,
      /xb3_collection_last_success_timestamp_seconds\{collector="downstream"\} 0/,
    );
    metrics.observe(observed);
    const firstRendered = metrics.render(observed);
    assert.match(
      firstRendered,
      /xb3_collection_last_success_timestamp_seconds\{collector="downstream"\} 1000/,
    );

    const uncommittedNewer = collection(
      page(network, 2_000),
      page(hardware, 2_000),
      page(lan, 2_000),
    );
    const preview = metrics.render(uncommittedNewer);
    const repeated = metrics.render(observed);
    assert.match(
      preview,
      /xb3_collection_last_success_timestamp_seconds\{collector="downstream"\} 1000/,
    );
    assert.match(
      repeated,
      /xb3_collection_last_success_timestamp_seconds\{collector="downstream"\} 1000/,
    );

    metrics.observe(uncommittedNewer);
    const committed = metrics.render(uncommittedNewer);
    assert.match(
      committed,
      /xb3_collection_last_success_timestamp_seconds\{collector="downstream"\} 2000/,
    );
  });

  it("does not re-emit stale RF after a failed page and freezes that group’s last-success time", () => {
    const metrics = new Metrics();
    const network = parseNetwork(partialNetworkHtml);
    const hardware = parseHardware(hardwareHtml);
    const lan = parseLan(lanHtml);
    const first = render(
      metrics,
      collection(page(network, 1000), page(hardware, 1000), page(lan, 1000)),
    );
    assert.match(
      first,
      /xb3_collection_last_success_timestamp_seconds\{collector="downstream"\} 1000/,
    );

    const second = render(
      metrics,
      collection(
        page(null, 1100, "auth_failed"),
        page(hardware, 1100),
        page(lan, 1100),
      ),
    );
    assert.equal(metricLine(second, "xb3_channel_frequency_hertz"), undefined);
    assert.equal(metricLine(second, "xb3_channel_power_dbmv"), undefined);
    assert.equal(metricLine(second, "xb3_locked_channels"), undefined);
    assert.equal(
      metricLine(second, "xb3_downstream_codewords_total"),
      undefined,
    );
    assert.match(second, /xb3_collection_success\{collector="downstream"\} 0/);
    assert.match(
      second,
      /xb3_collection_last_success_timestamp_seconds\{collector="downstream"\} 1000/,
    );
  });
});

describe("Collector", () => {
  it("coalesces concurrent scrapes and retains a valid network page when hardware fails", async () => {
    const calls = { network: 0, hardware: 0, lan: 0 };
    let releaseNetwork!: () => void;
    const networkGate = new Promise<void>((resolve) => {
      releaseNetwork = resolve;
    });
    const fakeClient = {
      async network() {
        calls.network += 1;
        await networkGate;
        return partialNetworkHtml;
      },
      async hardware() {
        calls.hardware += 1;
        throw new Error("hardware request failed");
      },
      async lan() {
        calls.lan += 1;
        return lanHtml;
      },
    };
    const collector = new Collector(fakeClient);
    const first = collector.collect();
    const second = collector.collect();
    assert.strictEqual(first, second);
    assert.deepEqual(calls, { network: 1, hardware: 0, lan: 0 });
    releaseNetwork();

    const [a, b] = await Promise.all([first, second]);
    assert.strictEqual(a, b);
    assert.deepEqual(calls, { network: 1, hardware: 1, lan: 1 });
    assert.equal(a.network.data?.downstream?.length, 32);
    assert.equal(a.network.error, null);
    assert.equal(a.hardware.data, null);
    assert.equal(a.hardware.error, "parse_failed");
    assert.deepEqual(a.lan.data?.ports, [
      { port: 1, active: true, speedMbps: 1000 },
      { port: 2, active: true, speedMbps: 1000 },
      { port: 3, active: false, speedMbps: null },
      { port: 4, active: false, speedMbps: null },
    ]);
    assert.equal(a.lan.error, null);
  });

  it("skips later pages after an ambiguous network failure, then resumes next collection", async () => {
    for (const failure of ["timeout", "transport_failed"] as const) {
      const calls = { network: 0, hardware: 0, lan: 0 };
      const fakeClient = {
        async network() {
          calls.network += 1;
          if (calls.network === 1) throw new GatewayError(failure);
          return partialNetworkHtml;
        },
        async hardware() {
          calls.hardware += 1;
          return hardwareHtml;
        },
        async lan() {
          calls.lan += 1;
          return lanHtml;
        },
      };
      const collector = new Collector(fakeClient);
      const first = await collector.collect();
      assert.deepEqual(calls, { network: 1, hardware: 0, lan: 0 });
      assert.equal(first.network.error, failure);
      assert.equal(first.network.data, null);
      assert.equal(first.hardware.data, null);
      assert.equal(first.hardware.durationSeconds, 0);
      assert.equal(first.hardware.error, "skipped_after_ambiguous_failure");
      assert.equal(first.lan.data, null);
      assert.equal(first.lan.durationSeconds, 0);
      assert.equal(first.lan.error, "skipped_after_ambiguous_failure");

      const next = await collector.collect();
      assert.deepEqual(calls, { network: 2, hardware: 1, lan: 1 });
      assert.equal(next.network.error, null);
      assert.equal(next.network.data?.downstream?.length, 32);
      assert.equal(next.hardware.error, null);
      assert.equal(next.hardware.data?.flashMb?.total, 256);
      assert.equal(next.lan.error, null);
      assert.equal(next.lan.data?.ports?.length, 4);
    }
  });

  it("preserves a good network page but skips LAN after ambiguous hardware failure", async () => {
    for (const failure of ["timeout", "transport_failed"] as const) {
      const calls = { network: 0, hardware: 0, lan: 0 };
      const fakeClient = {
        async network() {
          calls.network += 1;
          return partialNetworkHtml;
        },
        async hardware() {
          calls.hardware += 1;
          if (calls.hardware === 1) throw new GatewayError(failure);
          return hardwareHtml;
        },
        async lan() {
          calls.lan += 1;
          return lanHtml;
        },
      };
      const collector = new Collector(fakeClient);
      const first = await collector.collect();
      assert.deepEqual(calls, { network: 1, hardware: 1, lan: 0 });
      assert.equal(first.network.error, null);
      assert.equal(first.network.data?.downstream?.length, 32);
      assert.equal(first.hardware.error, failure);
      assert.equal(first.hardware.data, null);
      assert.equal(first.lan.data, null);
      assert.equal(first.lan.durationSeconds, 0);
      assert.equal(first.lan.error, "skipped_after_ambiguous_failure");

      const next = await collector.collect();
      assert.deepEqual(calls, { network: 2, hardware: 2, lan: 1 });
      assert.equal(next.network.error, null);
      assert.equal(next.hardware.error, null);
      assert.equal(next.lan.error, null);
      assert.equal(next.lan.data?.ports?.length, 4);
    }
  });
});
