import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  ProbeRecorder,
  PROBE_TARGETS,
  renderProbeMetrics,
  type ProbeClock,
} from "../src/probes.js";

class ManualClock implements ProbeClock {
  mono = 10_000;
  wall = 1_800_000_000_000;
  sleepCount = 0;
  onSleep?: (deadline: number, clock: ManualClock) => void;
  monotonicMs(): number {
    return this.mono;
  }
  wallMs(): number {
    return this.wall;
  }
  async sleepUntil(deadline: number, stop: AbortSignal): Promise<void> {
    if (stop.aborted) return;
    this.sleepCount++;
    this.mono = Math.max(this.mono, deadline);
    this.onSleep?.(deadline, this);
    await Promise.resolve();
  }
}

const settle = () => new Promise<void>((resolve) => setImmediate(resolve));
const result = (
  outcome: "reply" | "no_reply_by_deadline" | "local_error",
  rttMs: number | null,
) => ({ outcome, rttMs });

describe("ProbeRecorder", () => {
  it("keeps scheduling with unanswered probes, caps each target at two in flight, and records skips locally", async () => {
    const clock = new ManualClock();
    const records: Record<string, unknown>[] = [];
    const pending: (() => void)[] = [];
    const driver = (_address: string, stop?: AbortSignal) =>
      new Promise<ReturnType<typeof result>>((resolve) => {
        pending.push(() => resolve(result("reply", 12)));
        stop?.addEventListener(
          "abort",
          () => resolve(result("local_error", null)),
          { once: true },
        );
      });
    const recorder = new ProbeRecorder({
      startupId: "test-startup",
      startupSeconds: 1,
      intervalMs: 1000,
      maxTicks: 3,
      clock,
      driver,
      write: (record) => records.push(record as Record<string, unknown>),
    });
    const controller = new AbortController();
    const running = recorder.run(controller.signal);
    await settle();
    const state = recorder.states.get(PROBE_TARGETS[0].name)!;
    assert.equal(state.scheduled, 3);
    assert.equal(state.launched, 2);
    assert.equal(state.inFlight, 2);
    assert.equal(state.completed.skipped_local, 1);
    assert.equal(state.completed.no_reply_by_deadline, 0);
    assert.equal(
      records.filter((r) => r.outcome === "skipped_local").length,
      3,
    );
    controller.abort();
    await running;
    assert.equal(recorder.isRunning, false);
    assert.equal(state.inFlight, 0);
    assert.equal(state.completed.local_error, 2);
    assert.equal(pending.length, 6);
  });

  it("marks missed scheduler slots and launches only the next actual sequence", async () => {
    const clock = new ManualClock();
    clock.onSleep = (deadline, current) => {
      if (current.sleepCount === 1) current.mono = deadline + 2500;
    };
    const records: Record<string, unknown>[] = [];
    const recorder = new ProbeRecorder({
      startupId: "private-startup",
      startupSeconds: 123,
      intervalMs: 1000,
      maxTicks: 4,
      clock,
      driver: async () => result("reply", 2),
      write: (record) => records.push(record as Record<string, unknown>),
    });
    await recorder.run(new AbortController().signal);
    const targetRecords = records.filter(
      (r) =>
        r.target === PROBE_TARGETS[0].name && r.event === "xb3_probe_outcome",
    );
    assert.deepEqual(
      targetRecords.map((r) => [r.sequence, r.outcome]),
      [
        [1, "skipped_local"],
        [2, "skipped_local"],
        [3, "reply"],
        [4, "reply"],
      ],
    );
    assert.equal(
      targetRecords[2]?.scheduled_at,
      new Date(1_800_000_000_000 + 2000).toISOString(),
    );
    const exposition = recorder.render();
    assert.doesNotMatch(
      exposition,
      /private-startup|startup_seconds|validation_id|scheduled_at/,
    );
    assert.match(exposition, /mode="live",target="gateway"/);
  });

  it("reduces a finite injected outcome pattern and adds RTT only for replies", async () => {
    const clock = new ManualClock();
    const perAddress = new Map<string, number>();
    const driver = async (address: string) => {
      const n = (perAddress.get(address) ?? 0) + 1;
      perAddress.set(address, n);
      if (address === PROBE_TARGETS[0].address) return result("reply", 4);
      if (n === 1) return result("no_reply_by_deadline", null);
      if (n === 2) return result("local_error", null);
      return result("reply", 8);
    };
    const recorder = new ProbeRecorder({
      startupId: "synthetic",
      startupSeconds: 1,
      mode: "validation",
      validationId: "pattern-1",
      intervalMs: 1000,
      maxTicks: 2,
      clock,
      driver,
      write: () => {},
    });
    await recorder.run(new AbortController().signal);
    const gateway = recorder.states.get(PROBE_TARGETS[0].name)!;
    const external = recorder.states.get(PROBE_TARGETS[1].name)!;
    assert.deepEqual(gateway.completed, {
      reply: 2,
      no_reply_by_deadline: 0,
      local_error: 0,
      skipped_local: 0,
    });
    assert.equal(gateway.rttCount, 2);
    assert.equal(gateway.rttSumSeconds, 0.008);
    assert.deepEqual(external.completed, {
      reply: 0,
      no_reply_by_deadline: 1,
      local_error: 1,
      skipped_local: 0,
    });
    assert.equal(external.rttCount, 0);
  });

  it("converts a thrown driver failure into a local error without RTT", async () => {
    const recorder = new ProbeRecorder({
      startupId: "throws",
      startupSeconds: 1,
      intervalMs: 1000,
      maxTicks: 1,
      clock: new ManualClock(),
      driver: async () => {
        throw new Error("failure");
      },
      write: () => {},
    });
    await recorder.run(new AbortController().signal);
    for (const state of recorder.states.values()) {
      assert.equal(state.completed.local_error, 1);
      assert.equal(state.completed.no_reply_by_deadline, 0);
      assert.equal(state.rttCount, 0);
    }
  });

  it("combines live and validation samples into one contiguous family definition", async () => {
    const make = (mode: "live" | "validation") =>
      new ProbeRecorder({
        startupId: mode,
        startupSeconds: 1,
        mode,
        validationId: mode === "validation" ? "v1" : undefined,
        intervalMs: 1000,
        maxTicks: 1,
        clock: new ManualClock(),
        driver: async () => result("reply", 3),
        write: () => {},
      });
    const live = make("live");
    const validation = make("validation");
    await Promise.all([
      live.run(new AbortController().signal),
      validation.run(new AbortController().signal),
    ]);
    const exposition = renderProbeMetrics([live, validation]);
    for (const metric of [
      "xb3_probe_scheduled_total",
      "xb3_probe_outcomes_total",
      "xb3_probe_rtt_seconds",
    ]) {
      const lines = exposition.split("\n");
      const help = lines.indexOf(
        lines.find(
          (line) =>
            line ===
            `# HELP ${metric} ${metric === "xb3_probe_scheduled_total" ? "Scheduled slots, including explicitly skipped local slots." : metric === "xb3_probe_outcomes_total" ? "Probe results. Local errors and skipped slots are distinct from no reply by the utility deadline." : "Utility-reported RTT for recognized successful replies only."}`,
        ),
      );
      assert.ok(help >= 0, `missing ${metric} HELP`);
      assert.equal(
        lines[help + 1],
        `# TYPE ${metric} ${metric === "xb3_probe_rtt_seconds" ? "histogram" : "counter"}`,
      );
      const nextHelp = lines.findIndex(
        (line, index) => index > help && line.startsWith("# HELP "),
      );
      const familyEnd = nextHelp < 0 ? lines.length : nextHelp;
      const familyText = lines.slice(help, familyEnd).join("\n");
      assert.match(familyText, /mode="live"/);
      assert.match(familyText, /mode="validation"/);
      if (metric === "xb3_probe_rtt_seconds") {
        assert.match(familyText, /xb3_probe_rtt_seconds_bucket.*le="\+Inf"/);
        assert.match(familyText, /xb3_probe_rtt_seconds_sum/);
        assert.match(familyText, /xb3_probe_rtt_seconds_count/);
      }
    }
  });

  it("renders without mutating outcomes and derives wall offset and lateness from separate clocks", async () => {
    const clock = new ManualClock();
    clock.onSleep = (_deadline, current) => {
      current.wall += 20_000;
    };
    const records: Record<string, unknown>[] = [];
    const recorder = new ProbeRecorder({
      startupId: "clock-jump",
      startupSeconds: 1,
      intervalMs: 1000,
      maxTicks: 2,
      clock,
      driver: async () => result("reply", 10),
      write: (record) => records.push(record as Record<string, unknown>),
    });
    await recorder.run(new AbortController().signal);
    const state = recorder.states.get(PROBE_TARGETS[0].name)!;
    const before = structuredClone(state);
    const first = recorder.render();
    const second = recorder.render();
    assert.equal(second, first);
    assert.deepEqual(state, before);
    const outcomes = records.filter(
      (record) =>
        record.event === "xb3_probe_outcome" &&
        record.target === PROBE_TARGETS[0].name,
    );
    assert.equal(outcomes[0]?.wall_offset_ms, 20_000);
    assert.equal(outcomes[0]?.scheduler_lag_ms, 0);
    assert.equal(state.maxLagSeconds, 0);
  });
});
