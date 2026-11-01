/**
 * Line speed, measured by keeping the line full for a fixed time.
 *
 * Each direction runs for `durationMs` over `connections` parallel connections
 * to Cloudflare's speed-test endpoints. Bytes are put into `binMs` buckets by
 * the time they moved: downloads by when they arrived, uploads spread evenly
 * over each chunk's transfer once Cloudflare has confirmed it. The first
 * `warmupMs` is dropped while the connections speed up. Every remaining bucket
 * is one reading of the line's speed at that moment, and the app computes all
 * of its own numbers from those readings.
 */

export type Direction = "down" | "up";

export interface MethodSettings {
  /** How long each direction keeps the line full. */
  durationMs: number;
  /** Readings before this are dropped while connections speed up. */
  warmupMs: number;
  /** Width of one reading. */
  binMs: number;
  /** Parallel connections per direction. */
  connections: number;
  /** Size of each download request; downloads are cut off at the deadline. */
  downloadRequestBytes: number;
  /**
   * An upload chunk counts only once Cloudflare confirms it, and its bytes are
   * spread over its whole transfer, so a chunk longer than a dip averages the
   * dip away. Each chunk is sized from the previous one's speed to take about
   * this long, between the minimum and maximum sizes.
   */
  uploadChunkTargetMs: number;
  uploadFirstChunkBytes: number;
  uploadMinChunkBytes: number;
  uploadMaxChunkBytes: number;
  /** How long confirmed upload chunks may run past the deadline. */
  uploadGraceMs: number;
  /** Pause between the download and upload halves. */
  gapMs: number;
}

export const METHOD: MethodSettings = {
  durationMs: 10_000,
  warmupMs: 2_000,
  binMs: 250,
  connections: 2,
  downloadRequestBytes: 25_000_000,
  uploadChunkTargetMs: 500,
  uploadFirstChunkBytes: 262_144,
  uploadMinChunkBytes: 65_536,
  uploadMaxChunkBytes: 4_194_304,
  uploadGraceMs: 15_000,
  gapMs: 500,
};

/**
 * Timing of one confirmed upload chunk, on the transport's clock. Server time
 * is deliberately not taken off: Cloudflare's upload Server-Timing counts the
 * time spent receiving the body, so subtracting it erases the transfer itself
 * (0.4.0 did, and read uploads as instant bursts).
 */
export interface UploadTiming {
  /** The connection was ready and the first byte was written. */
  startMs: number;
  /** Cloudflare's response headers arrived. */
  endMs: number;
}

export interface StreamTransport {
  now(): number;
  /** Resolves when the body ends; `onBytes` sees every piece as it arrives. */
  download(
    bytes: number,
    onBytes: (count: number, atMs: number) => void,
    signal: AbortSignal,
  ): Promise<void>;
  /** Resolves once Cloudflare has received the whole chunk and answered. */
  upload(bytes: number, signal: AbortSignal): Promise<UploadTiming>;
}

/** A refusal from the server, with how long it asked us to wait. */
export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(`http_${status}`);
  }
}

/** Retry-After as delay seconds or an HTTP date. */
export function parseRetryAfter(
  header: string | undefined,
  nowMs: number,
): number | null {
  if (!header) return null;
  const trimmed = header.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const at = Date.parse(trimmed);
  return Number.isFinite(at)
    ? Math.max(0, Math.ceil((at - nowMs) / 1000))
    : null;
}

/** Linear interpolation between ranks. */
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

export interface DirectionResult {
  /** One reading per bin after the warm-up, in bits per second. */
  readingsBps: number[];
  /** Median reading: the speed the line held. */
  sustainedBps: number | null;
  /** 10th percentile: the slowest moments. */
  worstBps: number | null;
  /** 90th percentile: the fastest moments. */
  bestBps: number | null;
  /** Bytes moved, warm-up included. */
  bytes: number;
  requests: number;
}

export interface SpeedResult {
  download: DirectionResult;
  upload: DirectionResult;
  method: Pick<
    MethodSettings,
    "durationMs" | "warmupMs" | "binMs" | "connections"
  >;
}

export function summarize(
  readingsBps: number[],
  bytes: number,
  requests: number,
): DirectionResult {
  return {
    readingsBps,
    sustainedBps: percentile(readingsBps, 0.5),
    worstBps: percentile(readingsBps, 0.1),
    bestBps: percentile(readingsBps, 0.9),
    bytes,
    requests,
  };
}

/** Bins whose whole span lies in [warmup, coveredUntil] become readings. */
export function readingsFrom(
  binBytes: readonly number[],
  settings: MethodSettings,
  coveredUntilMs = settings.durationMs,
): number[] {
  const first = Math.ceil(settings.warmupMs / settings.binMs);
  const last = Math.floor(
    Math.min(coveredUntilMs, settings.durationMs) / settings.binMs,
  );
  const readings: number[] = [];
  for (let i = first; i < last; i++)
    readings.push(((binBytes[i] ?? 0) * 8) / (settings.binMs / 1000));
  return readings;
}

/** Add `bytes` spread evenly over [fromMs, toMs) into the bins. */
export function spread(
  binBytes: number[],
  bytes: number,
  fromMs: number,
  toMs: number,
  binMs: number,
): void {
  if (toMs <= fromMs) {
    const i = Math.max(0, Math.floor(fromMs / binMs));
    binBytes[i] = (binBytes[i] ?? 0) + bytes;
    return;
  }
  const rate = bytes / (toMs - fromMs);
  for (let i = Math.max(0, Math.floor(fromMs / binMs)); i * binMs < toMs; i++) {
    const overlap =
      Math.min(toMs, (i + 1) * binMs) - Math.max(fromMs, i * binMs);
    if (overlap > 0) binBytes[i] = (binBytes[i] ?? 0) + rate * overlap;
  }
}

/**
 * Size the next upload chunk to take about the target time at the last speed,
 * as a whole number of minimum-size chunks within the limits.
 */
export function nextChunkBytes(
  lastBytes: number,
  lastMs: number,
  settings: MethodSettings,
): number {
  if (lastMs <= 0) return settings.uploadMaxChunkBytes;
  const step = settings.uploadMinChunkBytes;
  const wanted = (lastBytes / lastMs) * settings.uploadChunkTargetMs;
  return Math.min(
    settings.uploadMaxChunkBytes,
    Math.max(step, Math.round(wanted / step) * step),
  );
}

const isAbort = (error: unknown) =>
  error instanceof Error &&
  (error.name === "AbortError" || error.message === "aborted");

async function downloadHalf(
  transport: StreamTransport,
  settings: MethodSettings,
  signal: AbortSignal | undefined,
): Promise<DirectionResult> {
  const binBytes: number[] = [];
  const t0 = transport.now();
  const deadline = new AbortController();
  const timer = setTimeout(() => deadline.abort(), settings.durationMs);
  const stop = signal
    ? AbortSignal.any([signal, deadline.signal])
    : deadline.signal;
  let bytes = 0;
  let requests = 0;
  const loop = async () => {
    while (!stop.aborted) {
      requests++;
      try {
        await transport.download(
          settings.downloadRequestBytes,
          (count, atMs) => {
            const offset = atMs - t0;
            bytes += count;
            if (offset < settings.durationMs) {
              const i = Math.floor(offset / settings.binMs);
              binBytes[i] = (binBytes[i] ?? 0) + count;
            }
          },
          stop,
        );
      } catch (error) {
        if (deadline.signal.aborted && !signal?.aborted && isAbort(error))
          return;
        throw error;
      }
    }
  };
  try {
    await Promise.all(Array.from({ length: settings.connections }, loop));
  } finally {
    clearTimeout(timer);
    deadline.abort();
  }
  signal?.throwIfAborted();
  return summarize(readingsFrom(binBytes, settings), bytes, requests);
}

async function uploadHalf(
  transport: StreamTransport,
  settings: MethodSettings,
  signal: AbortSignal | undefined,
): Promise<DirectionResult> {
  const binBytes: number[] = [];
  const t0 = transport.now();
  const grace = new AbortController();
  const timer = setTimeout(
    () => grace.abort(),
    settings.durationMs + settings.uploadGraceMs,
  );
  const stop = signal ? AbortSignal.any([signal, grace.signal]) : grace.signal;
  const coveredUntil = Array.from({ length: settings.connections }, () => 0);
  let bytes = 0;
  let requests = 0;
  const loop = async (_: unknown, connection: number) => {
    let chunk = settings.uploadFirstChunkBytes;
    while (transport.now() - t0 < settings.durationMs && !stop.aborted) {
      requests++;
      try {
        const timing = await transport.upload(chunk, stop);
        const from = timing.startMs - t0;
        const to = Math.max(from, timing.endMs - t0);
        spread(binBytes, chunk, from, to, settings.binMs);
        bytes += chunk;
        coveredUntil[connection] = to;
        chunk = nextChunkBytes(chunk, to - from, settings);
      } catch (error) {
        // A chunk cut off by the grace limit is unconfirmed: its span is left
        // out rather than guessed.
        if (grace.signal.aborted && !signal?.aborted && isAbort(error)) return;
        throw error;
      }
    }
  };
  try {
    await Promise.all(Array.from({ length: settings.connections }, loop));
  } finally {
    clearTimeout(timer);
    grace.abort();
  }
  signal?.throwIfAborted();
  return summarize(
    readingsFrom(binBytes, settings, Math.min(...coveredUntil)),
    bytes,
    requests,
  );
}

const pause = (ms: number, signal?: AbortSignal) =>
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
  transport: StreamTransport,
  settings: MethodSettings = METHOD,
  signal?: AbortSignal,
): Promise<SpeedResult> {
  const download = await downloadHalf(transport, settings, signal);
  await pause(settings.gapMs, signal);
  const upload = await uploadHalf(transport, settings, signal);
  return {
    download,
    upload,
    method: {
      durationMs: settings.durationMs,
      warmupMs: settings.warmupMs,
      binMs: settings.binMs,
      connections: settings.connections,
    },
  };
}
