import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { PortalGate, type PortalGateClock } from "../src/portal-gate.js";

class ManualClock implements PortalGateClock {
  mono = 5000;
  wall = 1_800_000_000_000;
  readonly requests: number[] = [];
  private sleepers: {
    resolve: () => void;
    reject: (reason: Error) => void;
    stop?: AbortSignal;
    onAbort?: () => void;
  }[] = [];
  monotonicMs(): number {
    return this.mono;
  }
  wallMs(): number {
    return this.wall;
  }
  sleep(ms: number, stop?: AbortSignal): Promise<void> {
    this.requests.push(ms);
    return new Promise<void>((resolve, reject) => {
      const sleeper = {
        resolve,
        reject,
        stop,
        onAbort: undefined as (() => void) | undefined,
      };
      sleeper.onAbort = () => {
        this.remove(sleeper);
        reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      };
      if (stop?.aborted) {
        sleeper.onAbort();
        return;
      }
      stop?.addEventListener("abort", sleeper.onAbort, { once: true });
      this.sleepers.push(sleeper);
    });
  }
  wakeAll(): void {
    const sleepers = [...this.sleepers];
    for (const sleeper of sleepers) {
      this.remove(sleeper);
      sleeper.resolve();
    }
  }
  advance(ms: number): void {
    this.mono += ms;
    this.wall += ms;
    this.wakeAll();
  }
  private remove(sleeper: (typeof this.sleepers)[number]): void {
    const index = this.sleepers.indexOf(sleeper);
    if (index >= 0) this.sleepers.splice(index, 1);
    if (sleeper.stop && sleeper.onAbort)
      sleeper.stop.removeEventListener("abort", sleeper.onAbort);
  }
}

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

describe("PortalGate", () => {
  it("serializes leases, yields metrics to FIFO log waiters, and releases idempotently", async () => {
    const clock = new ManualClock();
    const gate = new PortalGate({ clock });
    const metrics = gate.tryAcquireMetrics();
    assert.ok(metrics);
    assert.equal(metrics.owner, "metrics");
    assert.equal(gate.tryAcquireMetrics(), null);

    const firstLogsPromise = gate.acquireLogs();
    const secondLogsPromise = gate.acquireLogs();
    assert.equal(gate.state.logsWaiting, true);
    assert.equal(gate.tryAcquireMetrics(), null);
    assert.deepEqual(clock.requests, [1000, 1000]);

    metrics.release();
    metrics.release();
    clock.wakeAll();
    await flush();
    const firstLogs = await firstLogsPromise;
    assert.ok(firstLogs);
    assert.equal(firstLogs.owner, "logs");
    assert.equal(gate.state.owner, "logs");
    assert.equal(gate.tryAcquireMetrics(), null);

    firstLogs.release();
    clock.wakeAll();
    await flush();
    const secondLogs = await secondLogsPromise;
    assert.ok(secondLogs);
    assert.equal(secondLogs.owner, "logs");
    secondLogs.release();
    assert.equal(gate.state.owner, null);
    assert.equal(gate.state.logsWaiting, false);
    assert.ok(gate.tryAcquireMetrics());
  });

  it("keeps a shared monotonic cooldown after lease release and wakes logs at expiry", async () => {
    const clock = new ManualClock();
    const gate = new PortalGate({ clock });
    const metrics = gate.tryAcquireMetrics();
    assert.ok(metrics);
    gate.cooldown(5000);
    metrics.release();
    assert.equal(gate.state.owner, null);
    assert.equal(gate.state.cooldownRemainingSeconds, 5);
    assert.equal(gate.state.cooldownUntilSeconds, (clock.wall + 5000) / 1000);
    assert.equal(gate.tryAcquireMetrics(), null);

    const logsPromise = gate.acquireLogs();
    assert.equal(gate.state.logsWaiting, true);
    assert.ok(clock.requests.every((ms) => ms > 0 && ms <= 1000));
    clock.advance(5000);
    await flush();
    const logs = await logsPromise;
    assert.ok(logs);
    assert.equal(logs.owner, "logs");
    assert.equal(gate.state.cooldownRemainingSeconds, 0);
    logs.release();
    assert.ok(gate.tryAcquireMetrics());
  });

  it("uses a 60 second default cooldown and allows a newer cooldown to extend it", () => {
    const clock = new ManualClock();
    const gate = new PortalGate({ clock });
    gate.cooldown();
    assert.equal(gate.state.cooldownRemainingSeconds, 60);
    clock.advance(10_000);
    gate.cooldown(100_000);
    assert.equal(gate.state.cooldownRemainingSeconds, 100);
    gate.cooldown(1);
    assert.equal(gate.state.cooldownRemainingSeconds, 100);
  });

  it("removes an aborted log waiter so it no longer blocks metrics", async () => {
    const clock = new ManualClock();
    const gate = new PortalGate({ clock });
    const metrics = gate.tryAcquireMetrics();
    assert.ok(metrics);
    const controller = new AbortController();
    const logsPromise = gate.acquireLogs(controller.signal);
    assert.equal(gate.state.logsWaiting, true);
    controller.abort();
    assert.equal(await logsPromise, null);
    assert.equal(gate.state.logsWaiting, false);
    metrics.release();
    const nextMetrics = gate.tryAcquireMetrics();
    assert.ok(nextMetrics);
    nextMetrics.release();
  });

  it("returns null for an already-aborted logs request and ignores invalid cooldown durations", async () => {
    const gate = new PortalGate({ clock: new ManualClock() });
    const controller = new AbortController();
    controller.abort();
    assert.equal(await gate.acquireLogs(controller.signal), null);
    assert.throws(() => gate.cooldown(Number.NaN), /finite non-negative/);
    assert.throws(() => gate.cooldown(-1), /finite non-negative/);
  });
});
