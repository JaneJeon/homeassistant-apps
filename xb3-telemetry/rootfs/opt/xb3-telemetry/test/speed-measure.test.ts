import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  HttpError,
  measure,
  METHOD,
  nextChunkBytes,
  parseRetryAfter,
  parseServerTime,
  percentile,
  readingsFrom,
  spread,
  summarize,
  type MethodSettings,
  type StreamTransport,
  type UploadTiming,
} from "../src/speed-measure.js";

/** Small, fast settings: 400 ms per direction, 50 ms readings after 100 ms. */
const FAST: MethodSettings = {
  durationMs: 400,
  warmupMs: 100,
  binMs: 50,
  connections: 2,
  downloadRequestBytes: 1_000_000,
  uploadChunkTargetMs: 20,
  uploadFirstChunkBytes: 1_000,
  uploadMinChunkBytes: 500,
  uploadMaxChunkBytes: 200_000,
  uploadGraceMs: 1_000,
  gapMs: 10,
};

const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      },
      { once: true },
    );
  });

/**
 * A line with a fixed speed in bytes per millisecond, shared by all
 * connections. `rateAt` can slow it down for part of a test.
 */
class SimulatedLine implements StreamTransport {
  downloads = 0;
  uploads = 0;
  private readonly t0 = performance.now();
  constructor(
    private readonly rateAt: (dir: "down" | "up", ms: number) => number = () =>
      100,
    private readonly failWith?: Error,
  ) {}
  now(): number {
    return performance.now();
  }
  async download(
    bytes: number,
    onBytes: (count: number, atMs: number) => void,
    signal: AbortSignal,
  ): Promise<void> {
    this.downloads++;
    if (this.failWith) throw this.failWith;
    let left = bytes;
    while (left > 0) {
      await wait(5, signal);
      // Two connections share the line.
      const count = Math.min(
        left,
        (this.rateAt("down", this.now() - this.t0) * 5) / 2,
      );
      left -= count;
      onBytes(count, this.now());
    }
  }
  async upload(bytes: number, signal: AbortSignal): Promise<UploadTiming> {
    this.uploads++;
    if (this.failWith) throw this.failWith;
    const startMs = this.now();
    let left = bytes;
    while (left > 0) {
      await wait(5, signal);
      left -= (this.rateAt("up", this.now() - this.t0) * 5) / 2;
    }
    return { startMs, endMs: this.now(), serverMs: undefined };
  }
}

describe("speed method helpers", () => {
  it("reads Retry-After as seconds or a date", () => {
    const now = Date.parse("2026-10-31T20:00:00Z");
    assert.equal(parseRetryAfter("120", now), 120);
    assert.equal(parseRetryAfter("Sat, 31 Oct 2026 20:30:00 GMT", now), 1800);
    assert.equal(parseRetryAfter("soon", now), null);
    assert.equal(parseRetryAfter(undefined, now), null);
  });

  it("reads server time the way Cloudflare's engine does", () => {
    assert.equal(parseServerTime("cfRequestDuration;dur=12.5"), 12.5);
    assert.equal(
      parseServerTime("cfSpeedEdge;dur=3, cfSpeedWorker;dur=32"),
      35,
    );
    assert.equal(parseServerTime(undefined), undefined);
  });

  it("interpolates percentiles between ranks", () => {
    assert.equal(percentile([], 0.5), null);
    assert.equal(percentile([3, 1, 2], 0.5), 2);
    assert.equal(
      percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 0.1),
      19,
    );
  });

  it("spreads a confirmed chunk evenly over the bins it spans", () => {
    const bins: number[] = [];
    spread(bins, 1000, 100, 600, 250);
    assert.deepEqual(
      bins.map((b) => Math.round(b)),
      [300, 500, 200],
    );
    const instant: number[] = [];
    spread(instant, 10, 260, 260, 250);
    assert.equal(instant.length, 2);
    assert.equal(instant[0], undefined);
    assert.equal(instant[1], 10);
  });

  it("drops the warm-up and anything not covered", () => {
    const settings = { ...FAST, durationMs: 1500, warmupMs: 500, binMs: 250 };
    const bins = [1, 1, 10, 20, 30, 40];
    // 10 bytes in 250 ms is 320 bits per second.
    assert.deepEqual(readingsFrom(bins, settings), [320, 640, 960, 1280]);
    assert.deepEqual(readingsFrom(bins, settings, 1200), [320, 640]);
  });

  it("sizes upload chunks to take about half a second at the last speed", () => {
    // 24 Mbit/s over two connections: 1.5 MB/s each, so 11 × 64 KiB.
    assert.equal(nextChunkBytes(1_500_000, 1000, METHOD), 720_896);
    // 2.5 Mbit/s each: 2 × 64 KiB, so a slow line still gets short chunks.
    assert.equal(nextChunkBytes(312_500, 1000, METHOD), 131_072);
    // Never below the minimum or above the maximum.
    assert.equal(nextChunkBytes(10_000, 1000, METHOD), 65_536);
    assert.equal(nextChunkBytes(1e9, 1000, METHOD), 4_194_304);
    assert.equal(nextChunkBytes(1000, 0, METHOD), 4_194_304);
  });

  it("calls the median sustained and the tails worst and best", () => {
    const readings = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100];
    const result = summarize(readings, 5, 2);
    assert.equal(result.sustainedBps, 55);
    assert.equal(result.worstBps, 19);
    assert.equal(result.bestBps, 91);
  });
});

describe("measure", () => {
  it("reads a steady line as steady, in both directions", async () => {
    const line = new SimulatedLine();
    const result = await measure(line, FAST);
    // 100 bytes per ms is 800,000 bits per second.
    assert.equal(result.download.readingsBps.length, 6);
    assert.ok(result.upload.readingsBps.length >= 5);
    for (const d of [result.download, result.upload]) {
      assert.ok(
        Math.abs(d.sustainedBps! - 800_000) / 800_000 < 0.25,
        `${d.sustainedBps}`,
      );
    }
    assert.equal(result.method.binMs, 50);
  });

  it("shows a dip in the worst moments, not in the best", async () => {
    // Download drops to a fifth between 200 and 330 ms into the test.
    const line = new SimulatedLine((dir, ms) =>
      dir === "down" && ms >= 200 && ms < 330 ? 20 : 100,
    );
    const { download } = await measure(line, FAST);
    assert.ok(download.worstBps! < 400_000, `${download.worstBps}`);
    assert.ok(download.bestBps! > 600_000, `${download.bestBps}`);
  });

  it("shows an upload dip too, because chunks stay short", async () => {
    // Upload drops to a fifth for 130 ms of its 400 ms.
    let uploadFrom: number | null = null;
    const slow = new SimulatedLine((dir, ms) => {
      if (dir === "down") return 100;
      uploadFrom ??= ms;
      const into = ms - uploadFrom;
      return into >= 200 && into < 330 ? 20 : 100;
    });
    const { upload } = await measure(slow, FAST);
    assert.ok(upload.worstBps! < 450_000, `${upload.worstBps}`);
    assert.ok(upload.bestBps! > 600_000, `${upload.bestBps}`);
  });

  it("keeps few requests: the line is filled, not sampled", async () => {
    const line = new SimulatedLine();
    const result = await measure(line, FAST);
    // 1 MB requests at 100 bytes/ms never finish in 400 ms: one per connection.
    assert.equal(result.download.requests, 2);
    assert.equal(line.downloads, 2);
  });

  it("passes on a refusal with the wait the server asked for", async () => {
    const line = new SimulatedLine(undefined, new HttpError(429, 600));
    await assert.rejects(measure(line, FAST), (error: unknown) => {
      assert.ok(error instanceof HttpError);
      assert.equal(error.status, 429);
      assert.equal(error.retryAfterSeconds, 600);
      return true;
    });
  });

  it("stops when aborted", async () => {
    const stop = new AbortController();
    setTimeout(() => stop.abort(new Error("stop")), 50);
    await assert.rejects(measure(new SimulatedLine(), FAST, stop.signal));
  });
});
