import { setTimeout as delay } from "node:timers/promises";
import { pingProbe, type ProbeDriver } from "./probe-driver.js";

export const PROBE_TARGETS = [
  { name: "gateway", address: "10.0.0.1" },
  { name: "google", address: "8.8.8.8" },
  { name: "cloudflare", address: "1.1.1.1" },
] as const;
export type ProbeOutcome =
  | "reply"
  | "no_reply_by_deadline"
  | "local_error"
  | "skipped_local";
export interface ProbeClock {
  monotonicMs(): number;
  wallMs(): number;
  sleepUntil(deadline: number, stop: AbortSignal): Promise<void>;
}
const realClock: ProbeClock = {
  monotonicMs: () => performance.now(),
  wallMs: () => Date.now(),
  sleepUntil: async (deadline, stop) => {
    await delay(Math.max(0, deadline - performance.now()), undefined, {
      signal: stop,
    });
  },
};
export interface ProbeState {
  scheduled: number;
  launched: number;
  completed: Record<ProbeOutcome, number>;
  inFlight: number;
  lastCompletedSeconds: number;
  lastLaunchedSeconds: number;
  lagSeconds: number;
  maxLagSeconds: number;
  maxLaunchGapSeconds: number;
  rttCount: number;
  rttSumSeconds: number;
  rttBuckets: number[];
}
const buckets = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1];
export interface ProbeOptions {
  startupId: string;
  startupSeconds: number;
  mode?: "live" | "validation";
  validationId?: string;
  intervalMs?: number;
  maxTicks?: number;
  driver?: ProbeDriver;
  clock?: ProbeClock;
  write?: (record: object) => void;
}

/** Schedule independently of replies; process timestamps are not kernel packet timestamps. */
export class ProbeRecorder {
  readonly mode: "live" | "validation";
  readonly states = new Map<string, ProbeState>();
  private readonly clock: ProbeClock;
  private readonly driver: ProbeDriver;
  private readonly interval: number;
  private readonly pending = new Set<Promise<void>>();
  private readonly previousLaunch = new Map<string, number>();
  private originMono = 0;
  private originWall = 0;
  private running = false;
  get isRunning(): boolean {
    return this.running;
  }
  constructor(private readonly options: ProbeOptions) {
    this.mode = options.mode ?? "live";
    this.interval = options.intervalMs ?? 1000;
    if (!Number.isFinite(this.interval) || this.interval < 250)
      throw new Error("Invalid probe interval");
    this.clock = options.clock ?? realClock;
    this.driver = options.driver ?? pingProbe;
    for (const target of PROBE_TARGETS)
      this.states.set(target.name, {
        scheduled: 0,
        launched: 0,
        completed: {
          reply: 0,
          no_reply_by_deadline: 0,
          local_error: 0,
          skipped_local: 0,
        },
        inFlight: 0,
        lastCompletedSeconds: 0,
        lastLaunchedSeconds: 0,
        lagSeconds: 0,
        maxLagSeconds: 0,
        maxLaunchGapSeconds: 0,
        rttCount: 0,
        rttSumSeconds: 0,
        rttBuckets: buckets.map(() => 0),
      });
  }
  private emit(record: object): void {
    (this.options.write ?? ((x) => console.log(JSON.stringify(x))))({
      startup_id: this.options.startupId,
      startup_seconds: this.options.startupSeconds,
      mode: this.mode,
      synthetic: this.mode === "validation",
      ...(this.options.validationId
        ? { validation_id: this.options.validationId }
        : {}),
      ...record,
    });
  }
  private base(
    target: (typeof PROBE_TARGETS)[number],
    sequence: number,
    due: number,
  ): Record<string, unknown> {
    return {
      event: "xb3_probe_outcome",
      target: target.name,
      address: target.address,
      sequence,
      scheduled_at: new Date(
        this.originWall + due - this.originMono,
      ).toISOString(),
      interval_ms: this.interval,
      utility_timeout_ms: 1000,
    };
  }
  private skipped(
    target: (typeof PROBE_TARGETS)[number],
    sequence: number,
    due: number,
    reason: string,
  ): void {
    const state = this.states.get(target.name)!;
    state.scheduled++;
    state.completed.skipped_local++;
    state.lastCompletedSeconds = this.clock.wallMs() / 1000;
    this.emit({
      ...this.base(target, sequence, due),
      launched_at: null,
      completed_at: new Date(this.clock.wallMs()).toISOString(),
      outcome: "skipped_local",
      reason,
      rtt_ms: null,
    });
  }
  private launch(
    target: (typeof PROBE_TARGETS)[number],
    sequence: number,
    due: number,
    stop: AbortSignal,
  ): void {
    const state = this.states.get(target.name)!;
    if (state.inFlight >= 2) {
      this.skipped(target, sequence, due, "inflight_limit");
      return;
    }
    const launchedMono = this.clock.monotonicMs();
    const launchedWall = this.clock.wallMs();
    const lag = Math.max(0, launchedMono - due);
    state.scheduled++;
    state.launched++;
    state.inFlight++;
    state.lastLaunchedSeconds = launchedWall / 1000;
    state.lagSeconds = lag / 1000;
    state.maxLagSeconds = Math.max(state.maxLagSeconds, lag / 1000);
    const previous = this.previousLaunch.get(target.name);
    if (previous !== undefined)
      state.maxLaunchGapSeconds = Math.max(
        state.maxLaunchGapSeconds,
        (launchedMono - previous) / 1000,
      );
    this.previousLaunch.set(target.name, launchedMono);
    const task = (async () => {
      let result;
      try {
        result = await this.driver(target.address, stop);
      } catch {
        result = {
          outcome: "local_error" as const,
          rttMs: null,
          reason: "driver_exception",
        };
      }
      const completedMono = this.clock.monotonicMs();
      const completedWall = this.clock.wallMs();
      state.inFlight--;
      state.completed[result.outcome]++;
      state.lastCompletedSeconds = completedWall / 1000;
      if (result.outcome === "reply" && result.rttMs !== null) {
        state.rttCount++;
        state.rttSumSeconds += result.rttMs / 1000;
        buckets.forEach((bound, i) => {
          if (result.rttMs! / 1000 <= bound)
            state.rttBuckets[i] = (state.rttBuckets[i] ?? 0) + 1;
        });
      }
      this.emit({
        ...this.base(target, sequence, due),
        launched_at: new Date(launchedWall).toISOString(),
        completed_at: new Date(completedWall).toISOString(),
        launched_monotonic_ms: launchedMono,
        completed_monotonic_ms: completedMono,
        scheduler_lag_ms: lag,
        process_duration_ms: completedMono - launchedMono,
        wall_offset_ms:
          launchedWall - (this.originWall + launchedMono - this.originMono),
        completion_wall_offset_ms:
          completedWall - (this.originWall + completedMono - this.originMono),
        outcome: result.outcome,
        reason: result.reason ?? null,
        rtt_ms: result.rttMs,
      });
    })();
    this.pending.add(task);
    void task.finally(() => this.pending.delete(task));
  }
  async run(stop: AbortSignal): Promise<void> {
    if (this.running) throw new Error("Probe recorder already running");
    this.running = true;
    this.originMono = this.clock.monotonicMs();
    this.originWall = this.clock.wallMs();
    let tick = 0;
    try {
      while (
        !stop.aborted &&
        (this.options.maxTicks === undefined || tick < this.options.maxTicks)
      ) {
        let due = this.originMono + tick * this.interval;
        await this.clock.sleepUntil(due, stop);
        if (stop.aborted) break;
        const missed = Math.floor(
          Math.max(0, this.clock.monotonicMs() - due) / this.interval,
        );
        if (missed) {
          // Bound work after suspension. A range explicitly accounts for longer local gaps.
          const remaining =
            this.options.maxTicks === undefined
              ? missed
              : Math.min(missed, this.options.maxTicks - tick);
          if (remaining > 120) {
            for (const target of PROBE_TARGETS) {
              const state = this.states.get(target.name)!;
              state.scheduled += remaining;
              state.completed.skipped_local += remaining;
            }
            this.emit({
              event: "xb3_probe_schedule_gap",
              first_sequence: tick + 1,
              last_sequence: tick + remaining,
              completed_at: new Date(this.clock.wallMs()).toISOString(),
              reason: "scheduler_late",
              skipped_per_target: remaining,
            });
          } else
            for (let n = 0; n < remaining; n++)
              for (const target of PROBE_TARGETS)
                this.skipped(
                  target,
                  tick + n + 1,
                  due + n * this.interval,
                  "scheduler_late",
                );
          tick += remaining;
          due = this.originMono + tick * this.interval;
        }
        if (
          this.options.maxTicks !== undefined &&
          tick >= this.options.maxTicks
        )
          break;
        for (const target of PROBE_TARGETS)
          this.launch(target, tick + 1, due, stop);
        tick++;
      }
    } catch (error) {
      if (!stop.aborted) throw error;
    } finally {
      await Promise.allSettled(this.pending);
      this.running = false;
    }
  }
  render(): string {
    const lines: string[] = [];
    const family = (
      name: string,
      help: string,
      type: string,
      values: string[],
    ) => {
      lines.push(`# HELP ${name} ${help}`, `# TYPE ${name} ${type}`, ...values);
    };
    const labels = (target: string, extra = "") =>
      `{mode="${this.mode}",target="${target}"${extra}}`;
    const gauges: [string, string, (s: ProbeState) => number][] = [
      ["inflight", "Currently running probe subprocesses.", (s) => s.inFlight],
      [
        "last_completed_timestamp_seconds",
        "Last outcome completion time, including local failures.",
        (s) => s.lastCompletedSeconds,
      ],
      [
        "last_launched_timestamp_seconds",
        "Last subprocess launch time, not kernel transmit time.",
        (s) => s.lastLaunchedSeconds,
      ],
      [
        "scheduler_lag_seconds",
        "Most recent subprocess launch scheduling delay.",
        (s) => s.lagSeconds,
      ],
      [
        "scheduler_lag_max_seconds",
        "Maximum launch scheduling delay since recorder startup.",
        (s) => s.maxLagSeconds,
      ],
      [
        "launch_gap_max_seconds",
        "Maximum actual subprocess launch interval since startup.",
        (s) => s.maxLaunchGapSeconds,
      ],
    ];
    family(
      "xb3_probe_scheduled_total",
      "Scheduled slots, including explicitly skipped local slots.",
      "counter",
      [...this.states].map(
        ([t, s]) => `xb3_probe_scheduled_total${labels(t)} ${s.scheduled}`,
      ),
    );
    family(
      "xb3_probe_launched_total",
      "One-shot ping subprocesses launched. Not verified packet transmissions.",
      "counter",
      [...this.states].map(
        ([t, s]) => `xb3_probe_launched_total${labels(t)} ${s.launched}`,
      ),
    );
    family(
      "xb3_probe_outcomes_total",
      "Probe results. Local errors and skipped slots are distinct from no reply by the utility deadline.",
      "counter",
      [...this.states].flatMap(([t, s]) =>
        Object.entries(s.completed).map(
          ([o, n]) =>
            `xb3_probe_outcomes_total${labels(t, `,outcome="${o}"`)} ${n}`,
        ),
      ),
    );
    for (const [suffix, help, value] of gauges)
      family(
        `xb3_probe_${suffix}`,
        help,
        "gauge",
        [...this.states].map(
          ([t, s]) => `xb3_probe_${suffix}${labels(t)} ${value(s)}`,
        ),
      );
    family(
      "xb3_probe_rtt_seconds",
      "Utility-reported RTT for recognized successful replies only.",
      "histogram",
      [...this.states].flatMap(([t, s]) => [
        ...buckets.map(
          (b, i) =>
            `xb3_probe_rtt_seconds_bucket${labels(t, `,le="${b}"`)} ${s.rttBuckets[i]}`,
        ),
        `xb3_probe_rtt_seconds_bucket${labels(t, `,le="+Inf"`)} ${s.rttCount}`,
        `xb3_probe_rtt_seconds_sum${labels(t)} ${s.rttSumSeconds}`,
        `xb3_probe_rtt_seconds_count${labels(t)} ${s.rttCount}`,
      ]),
    );
    return lines.join("\n") + "\n";
  }
}

/** Keep both bounded modes within a single contiguous Prometheus family. */
export function renderProbeMetrics(recorders: ProbeRecorder[]): string {
  const families = new Map<
    string,
    { help: string; type: string; samples: string[] }
  >();
  for (const recorder of recorders) {
    let current: { help: string; type: string; samples: string[] } | undefined;
    for (const line of recorder.render().split("\n")) {
      if (!line) continue;
      if (line.startsWith("# HELP ")) {
        const name = line.split(" ")[2]!;
        current = families.get(name);
        if (!current) {
          current = { help: line, type: "", samples: [] };
          families.set(name, current);
        }
        if (current.help !== line) throw new Error("Inconsistent probe family");
      } else if (line.startsWith("# TYPE ")) {
        if (!current || (current.type && current.type !== line))
          throw new Error("Inconsistent probe type");
        current.type = line;
      } else {
        if (!current) throw new Error("Probe sample without family");
        current.samples.push(line);
      }
    }
  }
  return (
    [...families.values()]
      .flatMap((f) => [f.help, f.type, ...f.samples])
      .join("\n") + "\n"
  );
}

/** An explicitly synthetic transport; it never sends network traffic. */
export function validationDriver(): ProbeDriver {
  const counts = new Map<string, number>();
  return async (address, stop) => {
    const sequence = (counts.get(address) ?? 0) + 1;
    counts.set(address, sequence);
    const external = address !== PROBE_TARGETS[0].address;
    const loss = external && sequence >= 5 && sequence <= 7;
    await delay(loss ? 1000 : 20, undefined, { signal: stop });
    if (external && sequence === 9)
      return {
        outcome: "local_error",
        rttMs: null,
        reason: "validation_injected",
      };
    return {
      outcome: loss ? "no_reply_by_deadline" : "reply",
      rttMs: loss ? null : 1,
      reason: "validation_injected",
    };
  };
}
