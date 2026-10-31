import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { SpeedResult } from "../src/speed-measure.js";
import {
  isChildOutcome,
  SpeedRunner,
  type ChildOutcome,
  type SpeedRunnerClock,
} from "../src/speed-runner.js";

const result: SpeedResult = {
  downloadBps: 98.5e6,
  uploadBps: 20.9e6,
  idleLatencyMs: 14,
  idleJitterMs: 1.5,
  downloadLoadedLatencyMs: 55,
  uploadLoadedLatencyMs: 70,
  downloadBytes: 169e6,
  uploadBytes: 46.8e6,
  points: [{ dir: "down", bytes: 1e7, bps: 98.5e6, durationMs: 812.3 }],
  idleLatencyPoints: [14, 15],
};

class Clock implements SpeedRunnerClock {
  wall = Date.parse("2026-10-31T17:00:00Z");
  wallMs(): number {
    return this.wall;
  }
  sleep(): Promise<void> {
    return new Promise(() => undefined);
  }
}

function runner(
  outcome: (signal: AbortSignal) => Promise<ChildOutcome>,
  extra: { timeoutMs?: number; intervalMinutes?: number } = {},
) {
  const records: Record<string, unknown>[] = [];
  const clock = new Clock();
  const speed = new SpeedRunner({
    intervalMinutes: extra.intervalMinutes ?? 30,
    timeoutMs: extra.timeoutMs,
    write: (record) => records.push(record as Record<string, unknown>),
    runChild: outcome,
    clock,
  });
  return { speed, records, clock };
}

describe("SpeedRunner", () => {
  it("records a successful test in logs, metrics and the read-only summary", async () => {
    const { speed, records, clock } = runner(async () => {
      clock.wall += 35_000;
      return { ok: true, colo: "SJC", result };
    });
    assert.equal(speed.trigger("manual", "button-1"), "started");
    assert.equal(speed.running, true);
    await speed.settled();
    assert.equal(speed.running, false);
    assert.deepEqual(
      records.map((r) => r.event),
      ["xb3_speedtest_started", "xb3_speedtest_result"],
    );
    const done = records[1]!;
    assert.equal(done.request_id, "button-1");
    assert.equal(done.trigger, "manual");
    assert.equal(done.colo, "SJC");
    assert.equal(done.download_bps, 98.5e6);
    assert.equal(done.duration_seconds, 35);
    assert.deepEqual(done.bandwidth_points, [["down", 1e7, 98500000, 812.3]]);
    const text = speed.render();
    assert.match(text, /^xb3_speedtest_download_bits_per_second 98500000$/m);
    assert.match(text, /^xb3_speedtest_upload_bits_per_second 20900000$/m);
    assert.match(text, /^xb3_speedtest_idle_latency_seconds 0\.014$/m);
    assert.match(
      text,
      /^xb3_speedtest_loaded_latency_seconds\{direction="upload"\} 0\.07$/m,
    );
    assert.match(
      text,
      /^xb3_speedtest_runs_total\{trigger="manual",outcome="success"\} 1$/m,
    );
    assert.match(text, /^xb3_speedtest_running 0$/m);
    assert.match(text, /^xb3_speedtest_interval_seconds 1800$/m);
    assert.deepEqual(speed.snapshot(), {
      running: false,
      interval_minutes: 30,
      last_finished_at: "2026-10-31T17:00:35.000Z",
      last_result: {
        sequence: 1,
        trigger: "manual",
        finished_at: "2026-10-31T17:00:35.000Z",
        server: "SJC",
        download_mbps: 98.5,
        upload_mbps: 20.9,
        idle_latency_ms: 14,
        download_loaded_latency_ms: 55,
        upload_loaded_latency_ms: 70,
      },
    });
  });

  it("refuses a second test while one runs", async () => {
    let release!: () => void;
    let calls = 0;
    const { speed, records } = runner(() =>
      ++calls === 1
        ? new Promise((resolve) => {
            release = () => resolve({ ok: true, colo: null, result });
          })
        : Promise.resolve({ ok: true, colo: null, result }),
    );
    speed.trigger("schedule");
    assert.match(speed.render(), /^xb3_speedtest_running 1$/m);
    assert.equal(speed.trigger("manual", "again"), "busy");
    assert.equal(records.at(-1)!.event, "xb3_speedtest_rejected");
    assert.equal(records.at(-1)!.reason, "busy");
    release();
    await speed.settled();
    assert.equal(speed.trigger("manual", "later"), "started");
    await speed.settled();
  });

  it("keeps the previous result when a test fails", async () => {
    let fail = false;
    const { speed, records } = runner(async () =>
      fail
        ? { ok: false, colo: "SJC", reason: "http_429" }
        : { ok: true, colo: "SJC", result },
    );
    speed.trigger("schedule");
    await speed.settled();
    fail = true;
    speed.trigger("schedule");
    await speed.settled();
    assert.equal(records.at(-1)!.event, "xb3_speedtest_failed");
    assert.equal(records.at(-1)!.reason, "http_429");
    const text = speed.render();
    assert.match(text, /^xb3_speedtest_result_sequence 1$/m);
    assert.match(
      text,
      /^xb3_speedtest_runs_total\{trigger="schedule",outcome="failed"\} 1$/m,
    );
  });

  it("treats a result without bandwidth as a failure", async () => {
    const { speed, records } = runner(async () => ({
      ok: true,
      colo: null,
      result: { ...result, uploadBps: null },
    }));
    speed.trigger("manual", "x");
    await speed.settled();
    assert.equal(records.at(-1)!.reason, "no_usable_bandwidth");
    assert.equal(speed.last, null);
    assert.doesNotMatch(speed.render(), /download_bits_per_second/);
  });

  it("never stays stuck as running after an unexpected error", async () => {
    let broken = true;
    const { speed, records } = runner(async () =>
      broken
        ? ({
            ok: true,
            colo: null,
            result: { ...result, points: undefined },
          } as unknown as ChildOutcome)
        : { ok: true, colo: null, result },
    );
    speed.trigger("schedule");
    await speed.settled();
    assert.equal(speed.running, false);
    assert.equal(records.at(-1)!.event, "xb3_speedtest_failed");
    assert.equal(records.at(-1)!.reason, "runner_failed");
    broken = false;
    assert.equal(speed.trigger("manual", "after"), "started");
    await speed.settled();
    assert.equal(records.at(-1)!.event, "xb3_speedtest_result");
  });

  it("accepts child output only in the shape it reads", () => {
    assert.equal(isChildOutcome({ ok: true, colo: "SJC", result }), true);
    assert.equal(isChildOutcome({ ok: false, colo: null, reason: "x" }), true);
    assert.equal(isChildOutcome({ ok: true, colo: null }), false);
    assert.equal(
      isChildOutcome({
        ok: true,
        colo: null,
        result: { ...result, points: 1 },
      }),
      false,
    );
    assert.equal(
      isChildOutcome({
        ok: true,
        colo: null,
        result: { ...result, downloadBps: "fast" },
      }),
      false,
    );
    assert.equal(isChildOutcome({ ok: false, colo: 3, reason: "x" }), false);
    assert.equal(isChildOutcome(null), false);
  });

  it("kills a test that runs too long", async () => {
    const { speed, records } = runner(
      (signal) =>
        new Promise((resolve) =>
          signal.addEventListener("abort", () =>
            resolve({ ok: false, colo: null, reason: "timeout" }),
          ),
        ),
      { timeoutMs: 5 },
    );
    speed.trigger("schedule");
    await speed.settled();
    assert.equal(records.at(-1)!.reason, "timeout");
    assert.match(
      speed.render(),
      /^xb3_speedtest_runs_total\{trigger="schedule",outcome="timeout"\} 1$/m,
    );
  });

  it("schedules on wall-clock slots seven minutes past the interval", () => {
    const { speed } = runner(async () => ({ ok: true, colo: null, result }));
    const at = (iso: string) =>
      new Date(speed.nextSlotMs(Date.parse(iso))).toISOString();
    assert.equal(at("2026-10-31T17:00:00Z"), "2026-10-31T17:07:00.000Z");
    assert.equal(at("2026-10-31T17:07:00Z"), "2026-10-31T17:37:00.000Z");
    assert.equal(at("2026-10-31T17:20:00Z"), "2026-10-31T17:37:00.000Z");
    assert.equal(at("2026-10-31T17:50:00Z"), "2026-10-31T18:07:00.000Z");
    const hourly = runner(async () => ({ ok: true, colo: null, result }), {
      intervalMinutes: 60,
    }).speed;
    assert.equal(
      new Date(
        hourly.nextSlotMs(Date.parse("2026-10-31T17:10:00Z")),
      ).toISOString(),
      "2026-10-31T18:07:00.000Z",
    );
  });

  it("does not schedule when the interval is 0", async () => {
    const { speed, records } = runner(
      async () => ({ ok: true, colo: null, result }),
      {
        intervalMinutes: 0,
      },
    );
    await speed.run(new AbortController().signal);
    assert.equal(records.length, 0);
    assert.match(speed.render(), /^xb3_speedtest_interval_seconds 0$/m);
  });
});
