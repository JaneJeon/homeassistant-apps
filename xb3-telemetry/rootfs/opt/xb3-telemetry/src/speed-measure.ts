/**
 * Cloudflare's speed-test method (the engine behind speed.cloudflare.com,
 * @cloudflare/speedtest 1.15.0), re-implemented with Node's own clock.
 *
 * The published package cannot run here unchanged: it reads `window.location`,
 * takes request timings from the browser's PerformanceResourceTiming entries,
 * and buffers every download body as a string. This module keeps its
 * measurement sequence, ramp-up stop rule, server-time subtraction and
 * percentile reductions, and receives timings from an injected transport.
 */

export type Direction = "down" | "up";
export type Phase = "idle" | "download" | "upload";

export interface RequestTiming {
  /** Request start to final response headers, in milliseconds. */
  ttfbMs: number;
  /** Final response headers to the last body byte, in milliseconds. */
  payloadMs: number;
  /** Body bytes received (download) or sent (upload). */
  bytes: number;
  /** Server processing time reported by Server-Timing, in milliseconds. */
  serverMs: number | undefined;
}

export interface SpeedTransport {
  request(
    dir: Direction,
    bytes: number,
    during: Phase,
    signal?: AbortSignal,
  ): Promise<RequestTiming>;
}

export type Step =
  | { type: "latency"; count: number }
  | {
      type: Direction;
      bytes: number;
      count: number;
      bypassMinDuration?: boolean;
    };

/**
 * Cloudflare's default sequence, capped at 25 MB per request. Its larger sets
 * (100 and 250 MB down, 50 MB up) run only when every 25 MB request finishes
 * within a second, which needs about 200 Mbps. On 2026-10-31 the download
 * endpoint answered 403 to a 100 MB request from a fast test host, so those
 * sets would fail rather than measure.
 */
export const CLOUDFLARE_STEPS: readonly Step[] = [
  { type: "latency", count: 1 },
  { type: "down", bytes: 1e5, count: 1, bypassMinDuration: true },
  { type: "latency", count: 20 },
  { type: "down", bytes: 1e5, count: 9 },
  { type: "down", bytes: 1e6, count: 8 },
  { type: "up", bytes: 1e5, count: 8 },
  { type: "up", bytes: 1e6, count: 6 },
  { type: "down", bytes: 1e7, count: 6 },
  { type: "up", bytes: 1e7, count: 4 },
  { type: "down", bytes: 2.5e7, count: 4 },
  { type: "up", bytes: 2.5e7, count: 4 },
];

export interface SpeedSettings {
  /** A direction stops ramping once a whole set's fastest request takes longer. */
  finishRequestMs: number;
  /** Shorter requests are too noisy to use for bandwidth. */
  minRequestMs: number;
  /** Side latency counts only from sets whose fastest request lasted this long. */
  loadedRequestMinMs: number;
  loadedThrottleMs: number;
  loadedMaxPoints: number;
  bandwidthPercentile: number;
  latencyPercentile: number;
}

export const CLOUDFLARE_SETTINGS: SpeedSettings = {
  finishRequestMs: 1000,
  minRequestMs: 10,
  loadedRequestMinMs: 250,
  loadedThrottleMs: 400,
  loadedMaxPoints: 20,
  bandwidthPercentile: 0.9,
  latencyPercentile: 0.5,
};

export interface BandwidthPoint {
  dir: Direction;
  bytes: number;
  bps: number;
  durationMs: number;
}

export interface SpeedResult {
  downloadBps: number | null;
  uploadBps: number | null;
  idleLatencyMs: number | null;
  idleJitterMs: number | null;
  downloadLoadedLatencyMs: number | null;
  uploadLoadedLatencyMs: number | null;
  downloadBytes: number;
  uploadBytes: number;
  points: BandwidthPoint[];
  idleLatencyPoints: number[];
}

/** Same reading of Server-Timing as Cloudflare's engine. */
export function parseServerTime(
  header: string | undefined,
): number | undefined {
  if (!header) return undefined;
  const single = header.match(
    /(?:^|,\s*)cfReq(?:uest)?Dur(?:ation)?;\s*dur=([0-9.]+)/i,
  );
  if (single && Number(single[1]) > 0.01) return Number(single[1]);
  let sum = 0;
  for (const match of header.matchAll(
    /(?:^|,\s*)cfSpeed[a-zA-Z]*;\s*dur=([0-9.]+)/gi,
  ))
    sum += Number(match[1]);
  return sum > 0.01 ? sum : undefined;
}

/** Linear interpolation between ranks, as in Cloudflare's engine. */
export function percentile(
  values: readonly number[],
  p: number,
): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = (sorted.length - 1) * p;
  const low = sorted[Math.floor(index)]!;
  const high = sorted[Math.ceil(index)]!;
  return low + (high - low) * (index % 1);
}

/** Mean absolute difference between consecutive latencies. */
export function jitter(values: readonly number[]): number | null {
  if (values.length < 2) return null;
  let sum = 0;
  for (let i = 1; i < values.length; i++)
    sum += Math.abs(values[i]! - values[i - 1]!);
  return sum / (values.length - 1);
}

/** Round-trip time with the server's own processing time taken out. */
export function pingOf(timing: RequestTiming): number {
  return Math.max(0, timing.ttfbMs - (timing.serverMs ?? 0));
}

export function pointOf(
  dir: Direction,
  bytes: number,
  timing: RequestTiming,
): BandwidthPoint {
  // Download: ping plus body transfer. Upload: request start to response
  // headers, with Cloudflare's 0.5% allowance for request overhead.
  const durationMs =
    dir === "down" ? pingOf(timing) + timing.payloadMs : timing.ttfbMs;
  const bits = dir === "down" ? 8 * timing.bytes : 8 * bytes * 1.005;
  return {
    dir,
    bytes,
    bps: durationMs > 0 ? bits / (durationMs / 1000) : 0,
    durationMs,
  };
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason);
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(signal.reason);
      },
      { once: true },
    );
  });

export async function measure(
  transport: SpeedTransport,
  steps: readonly Step[] = CLOUDFLARE_STEPS,
  settings: SpeedSettings = CLOUDFLARE_SETTINGS,
  signal?: AbortSignal,
): Promise<SpeedResult> {
  const finished: Record<Direction, boolean> = { down: false, up: false };
  const points: BandwidthPoint[] = [];
  const idleLatencyPoints: number[] = [];
  const loaded: Record<Direction, number[]> = { down: [], up: [] };
  const transferred: Record<Direction, number> = { down: 0, up: 0 };

  for (const step of steps) {
    signal?.throwIfAborted();
    if (step.type === "latency") {
      for (let i = 0; i < step.count; i++)
        idleLatencyPoints.push(
          pingOf(await transport.request("down", 0, "idle", signal)),
        );
      continue;
    }
    const dir = step.type;
    if (finished[dir]) continue;

    // Loaded latency runs on its own connection while the set transfers.
    const side: number[] = [];
    const setDone = new AbortController();
    const sideSignal = signal
      ? AbortSignal.any([signal, setDone.signal])
      : setDone.signal;
    const sideLoop = (async () => {
      const phase: Phase = dir === "down" ? "download" : "upload";
      while (!sideSignal.aborted) {
        side.push(
          pingOf(await transport.request("down", 0, phase, sideSignal)),
        );
        await sleep(settings.loadedThrottleMs, sideSignal);
      }
    })().catch(() => undefined);

    const durations: number[] = [];
    try {
      for (let i = 0; i < step.count; i++) {
        const timing = await transport.request(
          dir,
          step.bytes,
          dir === "down" ? "download" : "upload",
          signal,
        );
        transferred[dir] += timing.bytes;
        const point = pointOf(dir, step.bytes, timing);
        durations.push(point.durationMs);
        if (point.bps > 0 && point.durationMs >= settings.minRequestMs)
          points.push(point);
      }
    } finally {
      setDone.abort();
      await sideLoop;
    }
    const fastest = Math.min(...durations);
    if (fastest >= settings.loadedRequestMinMs) loaded[dir].push(...side);
    if (!step.bypassMinDuration && fastest > settings.finishRequestMs)
      finished[dir] = true;
  }

  const bandwidth = (dir: Direction) =>
    percentile(
      points.filter((p) => p.dir === dir).map((p) => p.bps),
      settings.bandwidthPercentile,
    );
  const loadedLatency = (dir: Direction) =>
    percentile(
      loaded[dir].slice(-settings.loadedMaxPoints),
      settings.latencyPercentile,
    );
  return {
    downloadBps: bandwidth("down"),
    uploadBps: bandwidth("up"),
    idleLatencyMs: percentile(idleLatencyPoints, settings.latencyPercentile),
    idleJitterMs: jitter(idleLatencyPoints),
    downloadLoadedLatencyMs: loadedLatency("down"),
    uploadLoadedLatencyMs: loadedLatency("up"),
    downloadBytes: transferred.down,
    uploadBytes: transferred.up,
    points,
    idleLatencyPoints,
  };
}
