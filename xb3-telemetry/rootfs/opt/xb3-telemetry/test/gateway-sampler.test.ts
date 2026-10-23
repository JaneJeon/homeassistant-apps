import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { Collector, type Collection } from "../src/collector.js";
import { GatewayError } from "../src/client.js";
import { LogCollector } from "../src/logs.js";
import { Metrics } from "../src/metrics.js";
import { PortalGate, type PortalGateClock } from "../src/portal-gate.js";
import {
  GatewaySampler,
  type GatewaySamplerState,
  type SamplerClock,
} from "../src/gateway-sampler.js";

const fixture = (name: string) =>
  readFileSync(
    fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)),
    "utf8",
  );
const networkHtml = fixture("network-partial.html");
const hardwareHtml = fixture("hardware.html");
const lanHtml = fixture("lan.html");

function at<T>(items: readonly T[], index: number): T {
  const item = items[index];
  assert.ok(item);
  return item;
}

class ManualClock implements SamplerClock {
  monotonicMs = 0;
  readonly wallStartSeconds: number;

  constructor(wallStartSeconds = 1_000) {
    this.wallStartSeconds = wallStartSeconds;
  }

  nowMonotonicMs(): number {
    return this.monotonicMs;
  }

  nowWallSeconds(): number {
    return this.wallStartSeconds + this.monotonicMs / 1000;
  }

  async sleepUntil(deadlineMs: number, stop: AbortSignal): Promise<void> {
    if (!stop.aborted)
      this.monotonicMs = Math.max(this.monotonicMs, deadlineMs);
  }

  advanceBy(milliseconds: number): void {
    this.monotonicMs += milliseconds;
  }
}

class ControlledSamplerClock implements SamplerClock {
  monotonicMs = 0;
  private waitCount = 0;
  private pending: Array<{
    deadline: number;
    resolve: () => void;
    stop: AbortSignal;
    abort: () => void;
  }> = [];
  nowMonotonicMs(): number {
    return this.monotonicMs;
  }
  nowWallSeconds(): number {
    return 1_000 + this.monotonicMs / 1000;
  }
  sleepUntil(deadlineMs: number, stop: AbortSignal): Promise<void> {
    this.waitCount++;
    return new Promise<void>((resolve) => {
      const item = {
        deadline: deadlineMs,
        resolve,
        stop,
        abort: () => {
          this.remove(item);
          resolve();
        },
      };
      if (stop.aborted) {
        resolve();
        return;
      }
      stop.addEventListener("abort", item.abort, { once: true });
      this.pending.push(item);
    });
  }
  advanceNext(): void {
    const item = this.pending.shift();
    assert.ok(item, "expected a sampler sleep to be pending");
    this.monotonicMs = Math.max(this.monotonicMs, item.deadline);
    item.stop.removeEventListener("abort", item.abort);
    item.resolve();
  }
  async waitForSleeps(count: number): Promise<void> {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (this.waitCount >= count) return;
      await new Promise<void>((resolve) => setImmediate(resolve));
    }
    assert.fail(`sampler did not reach sleep ${count}`);
  }
  private remove(item: (typeof this.pending)[number]): void {
    const index = this.pending.indexOf(item);
    if (index >= 0) this.pending.splice(index, 1);
    item.stop.removeEventListener("abort", item.abort);
  }
}

class ManualGateClock implements PortalGateClock {
  monotonic = 0;
  wall = 1_800_000_000_000;
  private pending: Array<{
    resolve: () => void;
    reject: (error: Error) => void;
    stop?: AbortSignal;
    abort?: () => void;
  }> = [];
  monotonicMs(): number {
    return this.monotonic;
  }
  wallMs(): number {
    return this.wall;
  }
  sleep(_ms: number, stop?: AbortSignal): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const item = {
        resolve,
        reject,
        stop,
        abort: undefined as (() => void) | undefined,
      };
      item.abort = () => {
        this.remove(item);
        reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      };
      if (stop?.aborted) {
        item.abort();
        return;
      }
      stop?.addEventListener("abort", item.abort, { once: true });
      this.pending.push(item);
    });
  }
  advance(ms: number): void {
    this.monotonic += ms;
    this.wall += ms;
    const pending = [...this.pending];
    for (const item of pending) {
      this.remove(item);
      item.resolve();
    }
  }
  private remove(item: (typeof this.pending)[number]): void {
    const index = this.pending.indexOf(item);
    if (index >= 0) this.pending.splice(index, 1);
    if (item.stop && item.abort)
      item.stop.removeEventListener("abort", item.abort);
  }
}

function sampler(
  collector: Collector,
  clock: ManualClock,
  extras: Partial<ConstructorParameters<typeof GatewaySampler>[1]> = {},
) {
  const events: Record<string, unknown>[] = [];
  const observations: Array<{ collection: Collection; sequence: number }> = [];
  const states: GatewaySamplerState[] = [];
  let result: GatewaySampler;
  result = new GatewaySampler(collector, {
    startupId: "test-startup",
    startupSeconds: 1_000,
    networkIntervalMs: 20_000,
    auxiliaryIntervalMs: 60_000,
    clock,
    write: (event) => events.push(event),
    onObservation: (collection, sequence) => {
      observations.push({ collection, sequence });
      states.push(result.state);
    },
    ...extras,
  });
  return { sampler: result, events, observations, states };
}

describe("GatewaySampler", () => {
  it("schedules network every 20s, auxiliary pages every 60s, and carries cache across not-scheduled results", async () => {
    const clock = new ManualClock();
    const calls = { network: 0, hardware: 0, lan: 0 };
    const times: { network: number[]; hardware: number[]; lan: number[] } = {
      network: [],
      hardware: [],
      lan: [],
    };
    const fakeClient = {
      async network() {
        calls.network += 1;
        times.network.push(clock.monotonicMs);
        if (calls.network === 4) stop.abort();
        return networkHtml;
      },
      async hardware() {
        calls.hardware += 1;
        times.hardware.push(clock.monotonicMs);
        return calls.hardware === 1
          ? hardwareHtml
          : hardwareHtml.replace("450 MHz", "451 MHz");
      },
      async lan() {
        calls.lan += 1;
        times.lan.push(clock.monotonicMs);
        return lanHtml;
      },
    };
    const stop = new AbortController();
    const built = sampler(new Collector(fakeClient), clock);
    await built.sampler.run(stop.signal);

    assert.deepEqual(times.network, [0, 20_000, 40_000, 60_000]);
    assert.deepEqual(times.hardware, [0, 60_000]);
    assert.deepEqual(times.lan, [0, 60_000]);
    assert.equal(built.sampler.state.sequence, 4);
    assert.equal(built.sampler.state.skippedTicks, 0);
    assert.deepEqual(at(built.states, 1).pageSequences, {
      network: 2,
      hardware: 1,
      lan: 1,
    });
    assert.deepEqual(at(built.states, 2).pageSequences, {
      network: 3,
      hardware: 1,
      lan: 1,
    });
    assert.equal(at(built.states, 1).completedSequence, 2);
    assert.equal(
      at(built.observations, 0).collection.hardware.data?.cpuMhz,
      450,
    );
    assert.equal(
      at(built.observations, 1).collection.hardware.data?.cpuMhz,
      450,
    );
    assert.equal(
      at(built.observations, 2).collection.hardware.data?.cpuMhz,
      450,
    );
    assert.equal(
      at(built.observations, 3).collection.hardware.data?.cpuMhz,
      451,
    );

    const snapshots = built.events.filter(
      (event) => event.event === "xb3_gateway_snapshot",
    );
    assert.equal(snapshots.length, 4);
    const networkOnly = at(snapshots, 1);
    assert.equal(
      (networkOnly.hardware as Record<string, unknown>).error,
      "not_scheduled",
    );
    assert.equal((networkOnly.hardware as Record<string, unknown>).data, null);
    const first = at(snapshots, 0);
    assert.equal(first.startup_id, "test-startup");
    assert.equal(first.collection_sequence, 1);
    assert.equal(first.scheduled_at, 1_000);
    assert.equal(
      typeof (first.network as Record<string, unknown>).started_at_seconds,
      "number",
    );
    const networkData = (first.network as Record<string, unknown>)
      .data as Record<string, unknown>;
    assert.equal(
      at(networkData.codewords as Array<Record<string, unknown>>, 1)
        .uncorrectable,
      "151504",
    );
    assert.doesNotMatch(
      JSON.stringify(first),
      /password|serial|mac_address|ip_address/i,
    );
  });

  it("replaces an auxiliary cache after a real failure and keeps it null on later not-scheduled ticks", async () => {
    const clock = new ManualClock();
    const calls = { network: 0, hardware: 0, lan: 0 };
    const fakeClient = {
      async network() {
        calls.network += 1;
        if (calls.network === 7) stop.abort();
        return networkHtml;
      },
      async hardware() {
        calls.hardware += 1;
        if (calls.hardware === 2) throw new GatewayError("http_failed");
        return hardwareHtml;
      },
      async lan() {
        calls.lan += 1;
        return lanHtml;
      },
    };
    const stop = new AbortController();
    const built = sampler(new Collector(fakeClient), clock);
    await built.sampler.run(stop.signal);

    assert.deepEqual(calls, { network: 7, hardware: 3, lan: 3 });
    assert.equal(
      at(built.observations, 0).collection.hardware.data?.cpuMhz,
      450,
    );
    assert.equal(at(built.observations, 3).collection.hardware.data, null);
    assert.equal(
      at(built.observations, 3).collection.hardware.error,
      "http_failed",
    );
    assert.equal(at(built.observations, 4).collection.hardware.data, null);
    assert.equal(
      at(built.observations, 4).collection.hardware.error,
      "http_failed",
    );
    assert.equal(at(built.observations, 5).collection.hardware.data, null);
    assert.equal(
      at(built.observations, 6).collection.hardware.data?.cpuMhz,
      450,
    );

    const snapshots = built.events.filter(
      (event) => event.event === "xb3_gateway_snapshot",
    );
    assert.equal(
      (at(snapshots, 4).hardware as Record<string, unknown>).error,
      "not_scheduled",
    );
    assert.equal(
      (at(snapshots, 4).hardware as Record<string, unknown>).data,
      null,
    );
  });

  it("skips overdue slots instead of catching up in bursts", async () => {
    const clock = new ManualClock();
    const callsAt: number[] = [];
    let networkCalls = 0;
    const fakeClient = {
      async network() {
        networkCalls += 1;
        callsAt.push(clock.monotonicMs);
        if (networkCalls === 1) clock.advanceBy(55_000);
        if (networkCalls === 2) stop.abort();
        return networkHtml;
      },
      async hardware() {
        return hardwareHtml;
      },
      async lan() {
        return lanHtml;
      },
    };
    const stop = new AbortController();
    const built = sampler(new Collector(fakeClient), clock);
    await built.sampler.run(stop.signal);

    assert.deepEqual(callsAt, [0, 60_000]);
    assert.equal(built.sampler.state.skippedTicks, 2);
    const skipped = built.events.filter(
      (event) => event.event === "xb3_gateway_schedule_skipped",
    );
    assert.deepEqual(
      skipped.map((event) => event.scheduled_at),
      [1_020, 1_040],
    );
    assert.ok(skipped.every((event) => event.reason === "missed_deadline"));
  });

  it("enforces post-timeout cooldown without issuing early requests, then resumes on a scheduled tick", async () => {
    const clock = new ManualClock();
    const calls = { network: 0, hardware: 0, lan: 0 };
    const callsAt: number[] = [];
    const fakeClient = {
      async network() {
        calls.network += 1;
        callsAt.push(clock.monotonicMs);
        if (calls.network === 1) {
          clock.advanceBy(12_000);
          throw new GatewayError("timeout");
        }
        stop.abort();
        return networkHtml;
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
    const stop = new AbortController();
    const built = sampler(new Collector(fakeClient), clock);
    await built.sampler.run(stop.signal);

    assert.deepEqual(callsAt, [0, 80_000]);
    assert.deepEqual(calls, { network: 2, hardware: 0, lan: 0 });
    assert.equal(built.sampler.state.cooldownUntilSeconds, 1_072);
    assert.equal(built.sampler.state.skippedTicks, 3);
    const cooldown = built.events.find(
      (event) => event.event === "xb3_gateway_collection_cooldown",
    );
    assert.ok(cooldown);
    assert.deepEqual(cooldown.failed_pages, [
      { page: "network", reason: "timeout" },
    ]);
    const skipped = built.events.filter(
      (event) => event.event === "xb3_gateway_schedule_skipped",
    );
    assert.deepEqual(
      skipped.map((event) => event.scheduled_at),
      [1_020, 1_040, 1_060],
    );
    assert.ok(skipped.every((event) => event.reason === "cooldown"));
  });

  it("keeps the previous completed sequence and auxiliary page versions visible during a new attempt", async () => {
    const clock = new ManualClock();
    const stop = new AbortController();
    let networkCalls = 0;
    let announceSecondStart!: () => void;
    let releaseSecondNetwork!: () => void;
    const secondStarted = new Promise<void>((resolve) => {
      announceSecondStart = resolve;
    });
    const secondGate = new Promise<void>((resolve) => {
      releaseSecondNetwork = resolve;
    });
    const fakeClient = {
      async network() {
        networkCalls += 1;
        if (networkCalls === 2) {
          announceSecondStart();
          await secondGate;
          stop.abort();
        }
        return networkHtml;
      },
      async hardware() {
        return hardwareHtml;
      },
      async lan() {
        return lanHtml;
      },
    };
    const built = sampler(new Collector(fakeClient), clock);
    const running = built.sampler.run(stop.signal);
    await secondStarted;

    const during = built.sampler.state;
    assert.equal(during.sequence, 2);
    assert.equal(during.completedSequence, 1);
    assert.equal(during.inProgress, true);
    assert.deepEqual(during.pageSequences, {
      network: 1,
      hardware: 1,
      lan: 1,
    });
    assert.equal(during.collection?.network.data?.downstream?.length, 32);
    assert.equal(during.collection?.hardware.data?.cpuMhz, 450);

    releaseSecondNetwork();
    await running;
    const completed = built.sampler.state;
    assert.equal(completed.sequence, 2);
    assert.equal(completed.completedSequence, 2);
    assert.equal(completed.inProgress, false);
    assert.deepEqual(completed.pageSequences, {
      network: 2,
      hardware: 1,
      lan: 1,
    });
  });

  it("skips a busy portal tick without changing the cached observation, then resumes next tick", async () => {
    const clock = new ControlledSamplerClock();
    const access = new PortalGate();
    const stop = new AbortController();
    let networkCalls = 0;
    const fakeClient = {
      async network() {
        networkCalls++;
        if (networkCalls === 2) stop.abort();
        return networkHtml;
      },
      async hardware() {
        return hardwareHtml;
      },
      async lan() {
        return lanHtml;
      },
    };
    const built = sampler(new Collector(fakeClient), clock, { access });
    const running = built.sampler.run(stop.signal);
    await clock.waitForSleeps(1);
    clock.advanceNext();
    await clock.waitForSleeps(2);
    const previousObservedAt =
      built.sampler.state.collection?.network.observedAtSeconds;
    assert.ok(previousObservedAt);
    assert.equal(networkCalls, 1);

    const busyLease = access.tryAcquireMetrics();
    assert.ok(busyLease);
    clock.advanceNext();
    await clock.waitForSleeps(3);
    assert.equal(built.sampler.state.skippedTicks, 1);
    assert.equal(
      built.sampler.state.collection?.network.observedAtSeconds,
      previousObservedAt,
    );
    assert.equal(built.sampler.state.pageSequences.network, 1);
    assert.equal(networkCalls, 1);

    busyLease.release();
    clock.advanceNext();
    await running;
    assert.equal(networkCalls, 2);
    assert.equal(built.sampler.state.sequence, 2);
    assert.equal(built.sampler.state.completedSequence, 2);
  });

  it("shares a transport-timeout cooldown with a waiting log request", async () => {
    const samplerClock = new ControlledSamplerClock();
    const gateClock = new ManualGateClock();
    const access = new PortalGate({ clock: gateClock });
    const stop = new AbortController();
    let logCollector: LogCollector;
    let logPromise: Promise<void> | undefined;
    const logCalls: string[] = [];
    logCollector = new LogCollector(
      {
        async logs(_kind: string, timeFrame: string) {
          logCalls.push(timeFrame);
          return "[]";
        },
      },
      new Metrics(),
      () => {},
      access,
    );
    let networkCalls = 0;
    const fakeClient = {
      async network() {
        networkCalls++;
        logPromise = logCollector.once(["firewall"], 5000);
        assert.equal(access.state.owner, "metrics");
        assert.equal(access.state.logsWaiting, true);
        throw new GatewayError("transport_failed");
      },
      async hardware() {
        return hardwareHtml;
      },
      async lan() {
        return lanHtml;
      },
    };
    const built = sampler(new Collector(fakeClient), samplerClock, { access });
    const running = built.sampler.run(stop.signal);
    await samplerClock.waitForSleeps(1);
    samplerClock.advanceNext();
    await samplerClock.waitForSleeps(2);

    assert.equal(networkCalls, 1);
    assert.equal(access.state.owner, null);
    assert.equal(access.state.cooldownRemainingSeconds, 60);
    assert.equal(access.tryAcquireMetrics(), null);
    assert.deepEqual(logCalls, []);

    gateClock.advance(1000);
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.deepEqual(logCalls, []);
    assert.equal(access.state.cooldownRemainingSeconds, 59);

    gateClock.advance(59_000);
    await logPromise;
    assert.deepEqual(logCalls, ["Today", "Yesterday"]);
    stop.abort();
    await running;
  });
});
