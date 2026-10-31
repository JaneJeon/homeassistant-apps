import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import type { DirectionResult, SpeedResult } from "./speed-measure.js";

export type Trigger = "schedule" | "manual";
export type ChildOutcome =
  | { ok: true; colo: string | null; result: SpeedResult }
  | {
      ok: false;
      colo: string | null;
      reason: string;
      retryAfterSeconds?: number | null;
    };
export type RunChild = (signal: AbortSignal) => Promise<ChildOutcome>;

/**
 * What became of one scheduled slot or requested test. Each has a fixed code
 * for `xb3_speedtest_last_attempt_outcome`, so a dashboard can draw one row.
 */
export type Attempt = "tested" | "rate_limited" | "over_budget" | "failed";
export const ATTEMPT_CODES: Record<Attempt, number> = {
  tested: 1,
  rate_limited: 2,
  over_budget: 3,
  failed: 4,
};

/** Why a test cannot start right now. */
export type Hold = "busy" | "rate_limited" | "over_budget";

export interface SpeedRunnerClock {
  wallMs(): number;
  sleep(ms: number, signal: AbortSignal): Promise<void>;
}

export interface SpeedRunnerOptions {
  /** 0 runs tests only on request. */
  intervalMinutes: number;
  /** Slots sit this many minutes past the interval boundary, away from :00 jobs. */
  offsetMinutes?: number;
  timeoutMs?: number;
  /** Tests that may start in any rolling hour, scheduled and requested together. */
  maxPerHour?: number;
  /** Wait after a refusal that names no delay; doubles on each refusal in a row. */
  backoffMinutes?: number;
  maxBackoffMinutes?: number;
  write: (record: object) => void;
  runChild?: RunChild;
  clock?: SpeedRunnerClock;
}

export interface LastSpeedResult {
  sequence: number;
  trigger: Trigger;
  startedSeconds: number;
  finishedSeconds: number;
  colo: string | null;
  result: SpeedResult;
}

const HOUR_MS = 3_600_000;
const CHILD = fileURLToPath(new URL("./speed-child.js", import.meta.url));

const nullableNumber = (value: unknown) =>
  value === null || (typeof value === "number" && Number.isFinite(value));

function isDirectionResult(value: unknown): value is DirectionResult {
  if (!value || typeof value !== "object") return false;
  const d = value as Record<string, unknown>;
  return (
    Array.isArray(d.readingsBps) &&
    d.readingsBps.every((v) => typeof v === "number" && Number.isFinite(v)) &&
    [d.sustainedBps, d.worstBps, d.bestBps].every(nullableNumber) &&
    typeof d.bytes === "number" &&
    typeof d.requests === "number"
  );
}

/** The child's line is trusted only in the shape the runner reads. */
export function isChildOutcome(value: unknown): value is ChildOutcome {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  if (v.colo !== null && typeof v.colo !== "string") return false;
  if (v.ok === false)
    return (
      typeof v.reason === "string" &&
      (v.retryAfterSeconds === undefined || nullableNumber(v.retryAfterSeconds))
    );
  if (v.ok !== true || !v.result || typeof v.result !== "object") return false;
  const r = v.result as Record<string, unknown>;
  return (
    isDirectionResult(r.download) &&
    isDirectionResult(r.upload) &&
    !!r.method &&
    typeof r.method === "object"
  );
}

/** Run speed-child.js and parse its single JSON line. */
export const runChildProcess: RunChild = (signal) =>
  new Promise((resolve) => {
    const child = spawn(process.execPath, [CHILD], {
      stdio: ["ignore", "pipe", "ignore"],
    });
    let output = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      if (output.length < 1_048_576) output += chunk;
    });
    const kill = () => child.kill("SIGKILL");
    signal.addEventListener("abort", kill, { once: true });
    child.on("error", () =>
      resolve({ ok: false, colo: null, reason: "spawn_failed" }),
    );
    child.on("close", () => {
      signal.removeEventListener("abort", kill);
      if (signal.aborted)
        return resolve({ ok: false, colo: null, reason: "timeout" });
      const line = output.trim().split("\n").at(-1) ?? "";
      try {
        const parsed: unknown = JSON.parse(line);
        if (isChildOutcome(parsed)) return resolve(parsed);
      } catch {
        // fall through
      }
      resolve({ ok: false, colo: null, reason: "invalid_child_output" });
    });
  });

const systemClock: SpeedRunnerClock = {
  wallMs: () => Date.now(),
  sleep: (ms, signal) =>
    new Promise((resolve, reject) => {
      if (signal.aborted) return reject(signal.reason);
      const timer = setTimeout(resolve, ms);
      signal.addEventListener(
        "abort",
        () => {
          clearTimeout(timer);
          reject(signal.reason);
        },
        { once: true },
      );
    }),
};

const iso = (ms: number) => new Date(ms).toISOString();
const mbps = (bps: number | null) =>
  bps === null ? null : Math.round(bps / 1e4) / 100;

/**
 * Schedules speed tests on wall-clock slots and runs requested ones, one at a
 * time, within an hourly budget and any wait Cloudflare asks for. Holds only
 * the latest result, the latest attempt and run counts.
 */
export class SpeedRunner {
  private sequence = 0;
  private readonly runChild: RunChild;
  private readonly clock: SpeedRunnerClock;
  private readonly timeoutMs: number;
  private readonly offsetMinutes: number;
  readonly maxPerHour: number;
  private readonly backoffMinutes: number;
  private readonly maxBackoffMinutes: number;
  running = false;
  startedSeconds = 0;
  lastFinishedSeconds = 0;
  last: LastSpeedResult | null = null;
  lastAttempt: {
    outcome: Attempt;
    atSeconds: number;
    trigger: Trigger;
  } | null = null;
  /** Wall time before which Cloudflare has asked not to be tested. */
  rateLimitedUntilMs = 0;
  private refusalsInRow = 0;
  /** Wall times of test starts in the last hour. */
  private starts: number[] = [];
  /** `${trigger}|${outcome}` → count. */
  readonly runs = new Map<string, number>();
  /** Skipped scheduled slots by reason. */
  readonly skipped = new Map<Hold, number>();
  private inFlight: Promise<void> | null = null;

  constructor(private readonly options: SpeedRunnerOptions) {
    this.runChild = options.runChild ?? runChildProcess;
    this.clock = options.clock ?? systemClock;
    this.timeoutMs = options.timeoutMs ?? 90_000;
    this.offsetMinutes = options.offsetMinutes ?? 7;
    this.maxPerHour = options.maxPerHour ?? 3;
    this.backoffMinutes = options.backoffMinutes ?? 60;
    this.maxBackoffMinutes = options.maxBackoffMinutes ?? 240;
  }

  private startsInLastHour(nowMs: number): number[] {
    this.starts = this.starts.filter((at) => nowMs - at < HOUR_MS);
    return this.starts;
  }

  /** Why a test cannot start now, and when it next could. */
  hold(nowMs = this.clock.wallMs()): { hold: Hold; untilMs: number } | null {
    if (this.running) return { hold: "busy", untilMs: nowMs };
    if (this.rateLimitedUntilMs > nowMs)
      return { hold: "rate_limited", untilMs: this.rateLimitedUntilMs };
    const starts = this.startsInLastHour(nowMs);
    if (starts.length >= this.maxPerHour)
      return { hold: "over_budget", untilMs: starts[0]! + HOUR_MS };
    return null;
  }

  /** Start a test now unless one is running, Cloudflare asked us to wait, or
   * the hour's budget is used. */
  trigger(trigger: Trigger, id?: string): "started" | Hold {
    const nowMs = this.clock.wallMs();
    const held = this.hold(nowMs);
    if (held) {
      this.options.write({
        event: "xb3_speedtest_rejected",
        trigger,
        ...(id ? { request_id: id } : {}),
        reason: held.hold,
        ...(held.hold === "busy"
          ? { running_sequence: this.sequence }
          : { next_allowed_at: iso(held.untilMs) }),
      });
      return held.hold;
    }
    this.starts.push(nowMs);
    this.inFlight = this.execute(trigger, id);
    return "started";
  }

  /** Wait for the test in progress, if any. */
  async settled(): Promise<void> {
    await this.inFlight;
  }

  nextSlotMs(nowMs: number): number {
    const interval = this.options.intervalMinutes * 60_000;
    const offset = (this.offsetMinutes % this.options.intervalMinutes) * 60_000;
    return (
      Math.floor((nowMs - offset) / interval) * interval + offset + interval
    );
  }

  /** One scheduled slot: run a test, or record why not. */
  slot(): void {
    const nowMs = this.clock.wallMs();
    const held = this.hold(nowMs);
    if (!held) {
      this.trigger("schedule");
      return;
    }
    this.skipped.set(held.hold, (this.skipped.get(held.hold) ?? 0) + 1);
    this.options.write({
      event: "xb3_speedtest_slot_skipped",
      reason: held.hold,
      ...(held.hold === "busy"
        ? { running_sequence: this.sequence }
        : { next_allowed_at: iso(held.untilMs) }),
    });
    // A slot that finds a test already running is covered by that test.
    if (held.hold !== "busy")
      this.lastAttempt = {
        outcome: held.hold,
        atSeconds: nowMs / 1000,
        trigger: "schedule",
      };
  }

  async run(stop: AbortSignal): Promise<void> {
    if (this.options.intervalMinutes <= 0) return;
    while (!stop.aborted) {
      const now = this.clock.wallMs();
      try {
        await this.clock.sleep(this.nextSlotMs(now) - now, stop);
      } catch {
        return;
      }
      this.slot();
      await this.settled().catch(() => undefined);
    }
  }

  /** Whatever happens inside a run, the next one is never blocked by it. */
  private async execute(trigger: Trigger, id?: string): Promise<void> {
    const sequence = ++this.sequence;
    this.running = true;
    try {
      await this.executeRun(sequence, trigger, id);
    } catch {
      this.count(trigger, "failed");
      this.lastFinishedSeconds = this.clock.wallMs() / 1000;
      this.lastAttempt = {
        outcome: "failed",
        atSeconds: this.lastFinishedSeconds,
        trigger,
      };
      try {
        this.options.write({
          event: "xb3_speedtest_failed",
          sequence,
          trigger,
          reason: "runner_failed",
        });
      } catch {
        // Logging is best-effort here; clearing `running` below is not.
      }
    } finally {
      this.running = false;
    }
  }

  private async executeRun(
    sequence: number,
    trigger: Trigger,
    id?: string,
  ): Promise<void> {
    this.startedSeconds = this.clock.wallMs() / 1000;
    const startedAt = iso(this.startedSeconds * 1000);
    const common = {
      sequence,
      trigger,
      ...(id ? { request_id: id } : {}),
      method: "fill_line_10s",
    };
    this.options.write({
      event: "xb3_speedtest_started",
      ...common,
      started_at: startedAt,
    });
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(), this.timeoutMs);
    let outcome: ChildOutcome;
    try {
      outcome = await this.runChild(timeout.signal);
    } catch {
      outcome = { ok: false, colo: null, reason: "runner_failed" };
    } finally {
      clearTimeout(timer);
    }
    const finishedMs = this.clock.wallMs();
    const finishedSeconds = finishedMs / 1000;
    const base = {
      ...common,
      started_at: startedAt,
      finished_at: iso(finishedMs),
      duration_seconds: finishedSeconds - this.startedSeconds,
      colo: outcome.colo,
    };
    if (
      outcome.ok &&
      outcome.result.download.sustainedBps !== null &&
      outcome.result.upload.sustainedBps !== null
    ) {
      const { download, upload, method } = outcome.result;
      this.last = {
        sequence,
        trigger,
        startedSeconds: this.startedSeconds,
        finishedSeconds,
        colo: outcome.colo,
        result: outcome.result,
      };
      this.refusalsInRow = 0;
      this.count(trigger, "success");
      this.lastAttempt = {
        outcome: "tested",
        atSeconds: finishedSeconds,
        trigger,
      };
      const round = (values: number[]) => values.map((v) => Math.round(v));
      this.options.write({
        event: "xb3_speedtest_result",
        ...base,
        download_sustained_bps: download.sustainedBps,
        download_worst_bps: download.worstBps,
        download_best_bps: download.bestBps,
        upload_sustained_bps: upload.sustainedBps,
        upload_worst_bps: upload.worstBps,
        upload_best_bps: upload.bestBps,
        download_bytes: download.bytes,
        upload_bytes: upload.bytes,
        download_requests: download.requests,
        upload_requests: upload.requests,
        window_ms: method.durationMs,
        warmup_ms: method.warmupMs,
        bin_ms: method.binMs,
        connections: method.connections,
        download_readings_bps: round(download.readingsBps),
        upload_readings_bps: round(upload.readingsBps),
      });
    } else if (!outcome.ok && outcome.reason === "http_429") {
      this.refusalsInRow++;
      const waitSeconds =
        outcome.retryAfterSeconds ??
        60 *
          Math.min(
            this.maxBackoffMinutes,
            this.backoffMinutes * 2 ** (this.refusalsInRow - 1),
          );
      this.rateLimitedUntilMs = Math.max(
        this.rateLimitedUntilMs,
        finishedMs + waitSeconds * 1000,
      );
      this.count(trigger, "rate_limited");
      this.lastAttempt = {
        outcome: "rate_limited",
        atSeconds: finishedSeconds,
        trigger,
      };
      this.options.write({
        event: "xb3_speedtest_failed",
        ...base,
        reason: "rate_limited",
        retry_after_seconds: outcome.retryAfterSeconds ?? null,
        refusals_in_row: this.refusalsInRow,
        rate_limited_until: iso(this.rateLimitedUntilMs),
      });
    } else {
      const reason = outcome.ok ? "no_readings" : outcome.reason;
      this.count(trigger, reason === "timeout" ? "timeout" : "failed");
      this.lastAttempt = {
        outcome: "failed",
        atSeconds: finishedSeconds,
        trigger,
      };
      this.options.write({ event: "xb3_speedtest_failed", ...base, reason });
    }
    this.lastFinishedSeconds = finishedSeconds;
  }

  private count(trigger: Trigger, outcome: string): void {
    const key = `${trigger}|${outcome}`;
    this.runs.set(key, (this.runs.get(key) ?? 0) + 1);
  }

  render(): string {
    const nowMs = this.clock.wallMs();
    const lines: string[] = [];
    const family = (name: string, help: string, type: "gauge" | "counter") =>
      lines.push(`# HELP ${name} ${help}`, `# TYPE ${name} ${type}`);
    const sample = (name: string, value: number | null, labels = "") => {
      if (value !== null && Number.isFinite(value))
        lines.push(`${name}${labels} ${value}`);
    };
    family(
      "xb3_speedtest_running",
      "A speed test is filling the line now; probes taken meanwhile include its load.",
      "gauge",
    );
    sample("xb3_speedtest_running", Number(this.running));
    family(
      "xb3_speedtest_last_started_timestamp_seconds",
      "Start of the latest speed test, successful or not.",
      "gauge",
    );
    sample("xb3_speedtest_last_started_timestamp_seconds", this.startedSeconds);
    family(
      "xb3_speedtest_last_finished_timestamp_seconds",
      "End of the latest finished speed test, successful or not.",
      "gauge",
    );
    sample(
      "xb3_speedtest_last_finished_timestamp_seconds",
      this.lastFinishedSeconds,
    );
    family(
      "xb3_speedtest_interval_seconds",
      "Configured schedule; 0 means tests run only on request.",
      "gauge",
    );
    sample("xb3_speedtest_interval_seconds", this.options.intervalMinutes * 60);
    family(
      "xb3_speedtest_budget_per_hour",
      "Tests that may start in any rolling hour, scheduled and requested together.",
      "gauge",
    );
    sample("xb3_speedtest_budget_per_hour", this.maxPerHour);
    family(
      "xb3_speedtest_tests_last_hour",
      "Tests started in the last hour.",
      "gauge",
    );
    sample(
      "xb3_speedtest_tests_last_hour",
      this.startsInLastHour(nowMs).length,
    );
    family(
      "xb3_speedtest_rate_limited_until_timestamp_seconds",
      "Cloudflare refused a test and no test starts before this time; 0 when not waiting.",
      "gauge",
    );
    sample(
      "xb3_speedtest_rate_limited_until_timestamp_seconds",
      this.rateLimitedUntilMs > nowMs ? this.rateLimitedUntilMs / 1000 : 0,
    );
    family(
      "xb3_speedtest_skipped_slots_total",
      "Scheduled slots without a test, by reason: busy (a requested test was running), rate_limited, over_budget.",
      "counter",
    );
    for (const [reason, value] of this.skipped)
      sample(
        "xb3_speedtest_skipped_slots_total",
        value,
        `{reason="${reason}"}`,
      );
    family(
      "xb3_speedtest_runs_total",
      "Finished speed tests by trigger and outcome: success, rate_limited, failed or timeout.",
      "counter",
    );
    for (const [key, value] of this.runs) {
      const [trigger, outcome] = key.split("|");
      sample(
        "xb3_speedtest_runs_total",
        value,
        `{trigger="${trigger}",outcome="${outcome}"}`,
      );
    }
    if (this.lastAttempt) {
      family(
        "xb3_speedtest_last_attempt_timestamp_seconds",
        "When the latest slot or requested test was decided: tested, refused, skipped or failed.",
        "gauge",
      );
      sample(
        "xb3_speedtest_last_attempt_timestamp_seconds",
        this.lastAttempt.atSeconds,
      );
      family(
        "xb3_speedtest_last_attempt_outcome",
        "Latest attempt: 1 tested, 2 rate limited by Cloudflare, 3 over the hourly budget, 4 failed.",
        "gauge",
      );
      sample(
        "xb3_speedtest_last_attempt_outcome",
        ATTEMPT_CODES[this.lastAttempt.outcome],
      );
    }
    const last = this.last;
    if (last) {
      family(
        "xb3_speedtest_last_success_timestamp_seconds",
        "End of the latest successful speed test; the result gauges below belong to it.",
        "gauge",
      );
      sample(
        "xb3_speedtest_last_success_timestamp_seconds",
        last.finishedSeconds,
      );
      family(
        "xb3_speedtest_result_sequence",
        "Sequence of the test behind the result gauges; joins xb3_speedtest_result logs.",
        "gauge",
      );
      sample("xb3_speedtest_result_sequence", last.sequence);
      family(
        "xb3_speedtest_bits_per_second",
        "Latest successful test, from quarter-second readings over 8 seconds of a full line: sustained is the median, worst the 10th percentile, best the 90th.",
        "gauge",
      );
      for (const [direction, d] of [
        ["download", last.result.download],
        ["upload", last.result.upload],
      ] as const)
        for (const [stat, value] of [
          ["sustained", d.sustainedBps],
          ["worst", d.worstBps],
          ["best", d.bestBps],
        ] as const)
          sample(
            "xb3_speedtest_bits_per_second",
            value,
            `{direction="${direction}",stat="${stat}"}`,
          );
      family(
        "xb3_speedtest_readings",
        "Readings behind the latest result; fewer than 32 means part of the window was not confirmed.",
        "gauge",
      );
      sample(
        "xb3_speedtest_readings",
        last.result.download.readingsBps.length,
        '{direction="download"}',
      );
      sample(
        "xb3_speedtest_readings",
        last.result.upload.readingsBps.length,
        '{direction="upload"}',
      );
    }
    return lines.join("\n") + "\n";
  }

  /** Read-only summary for GET /speedtest. */
  snapshot(): object {
    const nowMs = this.clock.wallMs();
    const held = this.hold(nowMs);
    const last = this.last;
    const direction = (d: DirectionResult) => ({
      sustained_mbps: mbps(d.sustainedBps),
      worst_mbps: mbps(d.worstBps),
      best_mbps: mbps(d.bestBps),
    });
    return {
      status: held ? (held.hold === "busy" ? "running" : held.hold) : "ready",
      running: this.running,
      next_allowed_at: held && held.hold !== "busy" ? iso(held.untilMs) : null,
      interval_minutes: this.options.intervalMinutes,
      budget_per_hour: this.maxPerHour,
      tests_last_hour: this.startsInLastHour(nowMs).length,
      last_finished_at: this.lastFinishedSeconds
        ? iso(this.lastFinishedSeconds * 1000)
        : null,
      last_attempt: this.lastAttempt
        ? {
            outcome: this.lastAttempt.outcome,
            at: iso(this.lastAttempt.atSeconds * 1000),
            trigger: this.lastAttempt.trigger,
          }
        : null,
      last_result: last
        ? {
            sequence: last.sequence,
            trigger: last.trigger,
            finished_at: iso(last.finishedSeconds * 1000),
            server: last.colo,
            download: direction(last.result.download),
            upload: direction(last.result.upload),
          }
        : null,
    };
  }
}
