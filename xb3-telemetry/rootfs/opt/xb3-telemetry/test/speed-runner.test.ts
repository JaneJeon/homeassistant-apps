import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { DirectionResult, SpeedResult } from "../src/speed-measure.js";
import {
  isChildOutcome,
  SpeedRunner,
  type ChildOutcome,
  type SpeedRunnerClock,
} from "../src/speed-runner.js";

const direction = (sustained: number, worst: number, best: number) =>
  ({
    readingsBps: [worst, sustained, best],
    sustainedBps: sustained,
    worstBps: worst,
    bestBps: best,
    bytes: 1e8,
    requests: 6,
  }) satisfies DirectionResult;

const result: SpeedResult = {
  download: direction(116e6, 98e6, 117.5e6),
  upload: direction(23.5e6, 5.2e6, 24e6),
  method: { durationMs: 10_000, warmupMs: 2_000, binMs: 250, connections: 2 },
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

const ok = async (): Promise<ChildOutcome> => ({
  ok: true,
  colo: "SJC",
  result,
});

describe("SpeedRunner", () => {
  it("records a successful test in logs, metrics and the read-only summary", async () => {
    const { speed, records, clock } = runner(async () => {
      clock.wall += 25_000;
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
    assert.equal(done.method, "fill_line_10s");
    assert.equal(done.upload_worst_bps, 5.2e6);
    assert.deepEqual(done.upload_readings_bps, [5200000, 23500000, 24000000]);
    assert.equal(done.window_ms, 10_000);
    const text = speed.render();
    assert.match(
      text,
      /^xb3_speedtest_bits_per_second\{direction="download",stat="sustained"\} 116000000$/m,
    );
    assert.match(
      text,
      /^xb3_speedtest_bits_per_second\{direction="upload",stat="worst"\} 5200000$/m,
    );
    assert.match(text, /^xb3_speedtest_readings\{direction="upload"\} 3$/m);
    assert.match(text, /^xb3_speedtest_last_attempt_outcome 1$/m);
    assert.match(text, /^xb3_speedtest_tests_last_hour 1$/m);
    assert.match(
      text,
      /^xb3_speedtest_runs_total\{trigger="manual",outcome="success"\} 1$/m,
    );
    assert.deepEqual(speed.snapshot(), {
      status: "ready",
      running: false,
      next_allowed_at: null,
      interval_minutes: 30,
      budget_per_hour: 3,
      tests_last_hour: 1,
      last_finished_at: "2026-10-31T17:00:25.000Z",
      last_attempt: {
        outcome: "tested",
        at: "2026-10-31T17:00:25.000Z",
        trigger: "manual",
      },
      last_result: {
        sequence: 1,
        trigger: "manual",
        finished_at: "2026-10-31T17:00:25.000Z",
        server: "SJC",
        download: { sustained_mbps: 116, worst_mbps: 98, best_mbps: 117.5 },
        upload: { sustained_mbps: 23.5, worst_mbps: 5.2, best_mbps: 24 },
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
        : ok(),
    );
    speed.trigger("schedule");
    assert.match(speed.render(), /^xb3_speedtest_running 1$/m);
    assert.equal((speed.snapshot() as { status: string }).status, "running");
    assert.equal(speed.trigger("manual", "again"), "busy");
    assert.equal(records.at(-1)!.event, "xb3_speedtest_rejected");
    assert.equal(records.at(-1)!.reason, "busy");
    release();
    await speed.settled();
    assert.equal(speed.trigger("manual", "later"), "started");
    await speed.settled();
  });

  it("allows three tests in any hour and says when the next one may run", async () => {
    const { speed, records, clock } = runner(ok);
    for (let i = 0; i < 3; i++) {
      assert.equal(speed.trigger("manual"), "started");
      await speed.settled();
      clock.wall += 5 * 60_000;
    }
    assert.equal(speed.trigger("manual", "fourth"), "over_budget");
    const rejected = records.at(-1)!;
    assert.equal(rejected.reason, "over_budget");
    assert.equal(rejected.next_allowed_at, "2026-10-31T18:00:00.000Z");
    const snap = speed.snapshot() as Record<string, unknown>;
    assert.equal(snap.status, "over_budget");
    assert.equal(snap.next_allowed_at, "2026-10-31T18:00:00.000Z");
    // A scheduled slot over budget is recorded as such, not silently dropped.
    speed.slot();
    assert.equal(records.at(-1)!.event, "xb3_speedtest_slot_skipped");
    assert.equal(records.at(-1)!.reason, "over_budget");
    assert.match(speed.render(), /^xb3_speedtest_last_attempt_outcome 3$/m);
    assert.match(
      speed.render(),
      /^xb3_speedtest_skipped_slots_total\{reason="over_budget"\} 1$/m,
    );
    clock.wall = Date.parse("2026-10-31T18:00:00Z");
    assert.equal(speed.trigger("manual"), "started");
    await speed.settled();
  });

  it("waits as long as Cloudflare asks after a refusal", async () => {
    let refuse = true;
    const { speed, records, clock } = runner(async () =>
      refuse
        ? {
            ok: false,
            colo: "SJC",
            reason: "http_429",
            retryAfterSeconds: 1800,
          }
        : { ok: true, colo: "SJC", result },
    );
    speed.trigger("schedule");
    await speed.settled();
    const failed = records.at(-1)!;
    assert.equal(failed.reason, "rate_limited");
    assert.equal(failed.retry_after_seconds, 1800);
    assert.equal(failed.rate_limited_until, "2026-10-31T17:30:00.000Z");
    assert.match(speed.render(), /^xb3_speedtest_last_attempt_outcome 2$/m);
    assert.match(
      speed.render(),
      /^xb3_speedtest_rate_limited_until_timestamp_seconds 1793467800$/m,
    );
    assert.equal(speed.trigger("manual"), "rate_limited");
    speed.slot();
    assert.equal(records.at(-1)!.reason, "rate_limited");
    assert.match(
      speed.render(),
      /^xb3_speedtest_runs_total\{trigger="schedule",outcome="rate_limited"\} 1$/m,
    );
    clock.wall = Date.parse("2026-10-31T17:30:00Z");
    refuse = false;
    assert.equal(speed.trigger("schedule"), "started");
    await speed.settled();
    assert.match(
      speed.render(),
      /^xb3_speedtest_rate_limited_until_timestamp_seconds 0$/m,
    );
  });

  it("backs off longer each time a refusal names no wait", async () => {
    const { speed, records, clock } = runner(async () => ({
      ok: false,
      colo: null,
      reason: "http_429",
    }));
    speed.trigger("schedule");
    await speed.settled();
    assert.equal(
      records.at(-1)!.rate_limited_until,
      "2026-10-31T18:00:00.000Z",
    );
    clock.wall = Date.parse("2026-10-31T18:00:00Z");
    speed.trigger("schedule");
    await speed.settled();
    assert.equal(
      records.at(-1)!.rate_limited_until,
      "2026-10-31T20:00:00.000Z",
    );
    assert.equal(records.at(-1)!.refusals_in_row, 2);
  });

  it("keeps the previous result when a test fails", async () => {
    let fail = false;
    const { speed, records, clock } = runner(async () =>
      fail
        ? { ok: false, colo: "SJC", reason: "ECONNRESET" }
        : { ok: true, colo: "SJC", result },
    );
    speed.trigger("schedule");
    await speed.settled();
    fail = true;
    clock.wall += 30 * 60_000;
    speed.trigger("schedule");
    await speed.settled();
    assert.equal(records.at(-1)!.event, "xb3_speedtest_failed");
    assert.equal(records.at(-1)!.reason, "ECONNRESET");
    const text = speed.render();
    assert.match(text, /^xb3_speedtest_result_sequence 1$/m);
    assert.match(text, /^xb3_speedtest_last_attempt_outcome 4$/m);
    assert.match(
      text,
      /^xb3_speedtest_runs_total\{trigger="schedule",outcome="failed"\} 1$/m,
    );
  });

  it("treats a test without readings as a failure", async () => {
    const empty = { ...result.upload, readingsBps: [], sustainedBps: null };
    const { speed, records } = runner(async () => ({
      ok: true,
      colo: null,
      result: {
        ...result,
        upload: { ...empty, worstBps: null, bestBps: null },
      },
    }));
    speed.trigger("manual", "x");
    await speed.settled();
    assert.equal(records.at(-1)!.reason, "no_readings");
    assert.equal(speed.last, null);
    assert.doesNotMatch(speed.render(), /bits_per_second/);
  });

  it("never stays stuck as running after an unexpected error", async () => {
    let broken = true;
    const { speed, records, clock } = runner(async () =>
      broken
        ? ({
            ok: true,
            colo: null,
            result: { ...result, download: undefined },
          } as unknown as ChildOutcome)
        : { ok: true, colo: null, result },
    );
    speed.trigger("schedule");
    await speed.settled();
    assert.equal(speed.running, false);
    assert.equal(records.at(-1)!.event, "xb3_speedtest_failed");
    assert.equal(records.at(-1)!.reason, "runner_failed");
    broken = false;
    clock.wall += 60_000;
    assert.equal(speed.trigger("manual", "after"), "started");
    await speed.settled();
    assert.equal(records.at(-1)!.event, "xb3_speedtest_result");
  });

  it("accepts child output only in the shape it reads", () => {
    assert.equal(isChildOutcome({ ok: true, colo: "SJC", result }), true);
    assert.equal(isChildOutcome({ ok: false, colo: null, reason: "x" }), true);
    assert.equal(
      isChildOutcome({
        ok: false,
        colo: null,
        reason: "http_429",
        retryAfterSeconds: 60,
      }),
      true,
    );
    assert.equal(
      isChildOutcome({
        ok: false,
        colo: null,
        reason: "http_429",
        retryAfterSeconds: "soon",
      }),
      false,
    );
    assert.equal(isChildOutcome({ ok: true, colo: null }), false);
    assert.equal(
      isChildOutcome({
        ok: true,
        colo: null,
        result: {
          ...result,
          upload: { ...result.upload, readingsBps: [1, "x"] },
        },
      }),
      false,
    );
    assert.equal(
      isChildOutcome({
        ok: true,
        colo: null,
        result: {
          ...result,
          download: { ...result.download, worstBps: "slow" },
        },
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
    const { speed } = runner(ok);
    const at = (iso: string) =>
      new Date(speed.nextSlotMs(Date.parse(iso))).toISOString();
    assert.equal(at("2026-10-31T17:00:00Z"), "2026-10-31T17:07:00.000Z");
    assert.equal(at("2026-10-31T17:07:00Z"), "2026-10-31T17:37:00.000Z");
    assert.equal(at("2026-10-31T17:50:00Z"), "2026-10-31T18:07:00.000Z");
  });

  it("does not schedule when the interval is 0", async () => {
    const { speed, records } = runner(ok, { intervalMinutes: 0 });
    await speed.run(new AbortController().signal);
    assert.equal(records.length, 0);
    assert.match(speed.render(), /^xb3_speedtest_interval_seconds 0$/m);
  });
});
