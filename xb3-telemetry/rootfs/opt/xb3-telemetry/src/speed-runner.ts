import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import type { SpeedResult } from "./speed-measure.js";

export type Trigger = "schedule" | "manual";
export type ChildOutcome =
  | { ok: true; colo: string | null; result: SpeedResult }
  | { ok: false; colo: string | null; reason: string };
export type RunChild = (signal: AbortSignal) => Promise<ChildOutcome>;

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

const CHILD = fileURLToPath(new URL("./speed-child.js", import.meta.url));

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
        const parsed = JSON.parse(line) as ChildOutcome;
        if (typeof parsed?.ok === "boolean") return resolve(parsed);
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

/**
 * Schedules speed tests on wall-clock slots and runs requested ones, one at a
 * time. Holds only the latest result and run counts.
 */
export class SpeedRunner {
  private sequence = 0;
  private readonly runChild: RunChild;
  private readonly clock: SpeedRunnerClock;
  private readonly timeoutMs: number;
  private readonly offsetMinutes: number;
  running = false;
  startedSeconds = 0;
  lastFinishedSeconds = 0;
  skippedSlots = 0;
  last: LastSpeedResult | null = null;
  /** `${trigger}|${outcome}` → count. */
  readonly runs = new Map<string, number>();
  private inFlight: Promise<void> | null = null;

  constructor(private readonly options: SpeedRunnerOptions) {
    this.runChild = options.runChild ?? runChildProcess;
    this.clock = options.clock ?? systemClock;
    this.timeoutMs = options.timeoutMs ?? 150_000;
    this.offsetMinutes = options.offsetMinutes ?? 7;
  }

  /** Start a test now unless one is running. */
  trigger(trigger: Trigger, id?: string): "started" | "busy" {
    if (this.running) {
      this.options.write({
        event: "xb3_speedtest_rejected",
        trigger,
        ...(id ? { request_id: id } : {}),
        reason: "busy",
        running_sequence: this.sequence,
      });
      return "busy";
    }
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

  async run(stop: AbortSignal): Promise<void> {
    if (this.options.intervalMinutes <= 0) return;
    while (!stop.aborted) {
      const now = this.clock.wallMs();
      try {
        await this.clock.sleep(this.nextSlotMs(now) - now, stop);
      } catch {
        return;
      }
      if (this.running) {
        this.skippedSlots++;
        this.options.write({
          event: "xb3_speedtest_slot_skipped",
          reason: "busy",
          running_sequence: this.sequence,
        });
        continue;
      }
      this.trigger("schedule");
      await this.settled();
    }
  }

  private async execute(trigger: Trigger, id?: string): Promise<void> {
    const sequence = ++this.sequence;
    this.running = true;
    this.startedSeconds = this.clock.wallMs() / 1000;
    const startedAt = new Date(this.startedSeconds * 1000).toISOString();
    const common = {
      sequence,
      trigger,
      ...(id ? { request_id: id } : {}),
      method: "cloudflare_engine_1.15.0",
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
    const finishedSeconds = this.clock.wallMs() / 1000;
    const base = {
      ...common,
      started_at: startedAt,
      finished_at: new Date(finishedSeconds * 1000).toISOString(),
      duration_seconds: finishedSeconds - this.startedSeconds,
      colo: outcome.colo,
    };
    if (
      outcome.ok &&
      outcome.result.downloadBps !== null &&
      outcome.result.uploadBps !== null
    ) {
      const r = outcome.result;
      this.last = {
        sequence,
        trigger,
        startedSeconds: this.startedSeconds,
        finishedSeconds,
        colo: outcome.colo,
        result: r,
      };
      this.count(trigger, "success");
      this.options.write({
        event: "xb3_speedtest_result",
        ...base,
        download_bps: r.downloadBps,
        upload_bps: r.uploadBps,
        idle_latency_ms: r.idleLatencyMs,
        idle_jitter_ms: r.idleJitterMs,
        download_loaded_latency_ms: r.downloadLoadedLatencyMs,
        upload_loaded_latency_ms: r.uploadLoadedLatencyMs,
        download_bytes: r.downloadBytes,
        upload_bytes: r.uploadBytes,
        idle_latency_points_ms: r.idleLatencyPoints,
        bandwidth_points: r.points.map((p) => [
          p.dir,
          p.bytes,
          Math.round(p.bps),
          Math.round(p.durationMs * 10) / 10,
        ]),
      });
    } else {
      const reason = outcome.ok ? "no_usable_bandwidth" : outcome.reason;
      this.count(trigger, reason === "timeout" ? "timeout" : "failed");
      this.options.write({ event: "xb3_speedtest_failed", ...base, reason });
    }
    this.lastFinishedSeconds = finishedSeconds;
    this.running = false;
  }

  private count(trigger: Trigger, outcome: string): void {
    const key = `${trigger}|${outcome}`;
    this.runs.set(key, (this.runs.get(key) ?? 0) + 1);
  }

  render(): string {
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
      "xb3_speedtest_skipped_slots",
      "Scheduled slots skipped because a test was already running.",
      "gauge",
    );
    sample("xb3_speedtest_skipped_slots", this.skippedSlots);
    family(
      "xb3_speedtest_runs_total",
      "Finished speed tests by trigger and outcome.",
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
    const last = this.last;
    if (last) {
      const r = last.result;
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
        "xb3_speedtest_download_bits_per_second",
        "Download speed of the latest successful test (90th percentile of request speeds).",
        "gauge",
      );
      sample("xb3_speedtest_download_bits_per_second", r.downloadBps);
      family(
        "xb3_speedtest_upload_bits_per_second",
        "Upload speed of the latest successful test (90th percentile of request speeds).",
        "gauge",
      );
      sample("xb3_speedtest_upload_bits_per_second", r.uploadBps);
      family(
        "xb3_speedtest_idle_latency_seconds",
        "Median round trip to the test server before loading the line.",
        "gauge",
      );
      sample(
        "xb3_speedtest_idle_latency_seconds",
        r.idleLatencyMs === null ? null : r.idleLatencyMs / 1000,
      );
      family(
        "xb3_speedtest_idle_jitter_seconds",
        "Mean change between consecutive idle round trips.",
        "gauge",
      );
      sample(
        "xb3_speedtest_idle_jitter_seconds",
        r.idleJitterMs === null ? null : r.idleJitterMs / 1000,
      );
      family(
        "xb3_speedtest_loaded_latency_seconds",
        "Median round trip while a download or upload fills the line.",
        "gauge",
      );
      sample(
        "xb3_speedtest_loaded_latency_seconds",
        r.downloadLoadedLatencyMs === null
          ? null
          : r.downloadLoadedLatencyMs / 1000,
        '{direction="download"}',
      );
      sample(
        "xb3_speedtest_loaded_latency_seconds",
        r.uploadLoadedLatencyMs === null
          ? null
          : r.uploadLoadedLatencyMs / 1000,
        '{direction="upload"}',
      );
    }
    return lines.join("\n") + "\n";
  }

  /** Read-only summary for GET /speedtest. */
  snapshot(): object {
    const last = this.last;
    const mbps = (bps: number | null) =>
      bps === null ? null : Math.round(bps / 1e4) / 100;
    return {
      running: this.running,
      interval_minutes: this.options.intervalMinutes,
      last_finished_at: this.lastFinishedSeconds
        ? new Date(this.lastFinishedSeconds * 1000).toISOString()
        : null,
      last_result: last
        ? {
            sequence: last.sequence,
            trigger: last.trigger,
            finished_at: new Date(last.finishedSeconds * 1000).toISOString(),
            server: last.colo,
            download_mbps: mbps(last.result.downloadBps),
            upload_mbps: mbps(last.result.uploadBps),
            idle_latency_ms: last.result.idleLatencyMs,
            download_loaded_latency_ms: last.result.downloadLoadedLatencyMs,
            upload_loaded_latency_ms: last.result.uploadLoadedLatencyMs,
          }
        : null,
    };
  }
}
