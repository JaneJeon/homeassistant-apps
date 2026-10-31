import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  CLOUDFLARE_SETTINGS,
  jitter,
  measure,
  parseServerTime,
  percentile,
  pingOf,
  pointOf,
  type Direction,
  type Phase,
  type RequestTiming,
  type SpeedTransport,
} from "../src/speed-measure.js";

/** A line with fixed speeds and round trip; side latency rises under load. */
class SimulatedLine implements SpeedTransport {
  readonly requests: { dir: Direction; bytes: number; during: Phase }[] = [];
  constructor(
    private readonly downBps = 100e6,
    private readonly upBps = 20e6,
    private readonly rttMs = 15,
    private readonly loadedRttMs = 60,
    private readonly serverMs = 3,
  ) {}
  async request(
    dir: Direction,
    bytes: number,
    during: Phase,
    signal?: AbortSignal,
  ): Promise<RequestTiming> {
    signal?.throwIfAborted();
    this.requests.push({ dir, bytes, during });
    await new Promise((resolve) => setTimeout(resolve, 1));
    const rtt =
      bytes === 0 && during !== "idle" ? this.loadedRttMs : this.rttMs;
    if (dir === "down")
      return {
        ttfbMs: rtt + this.serverMs,
        payloadMs: bytes ? ((bytes * 8) / this.downBps) * 1000 : 0,
        bytes,
        serverMs: this.serverMs,
      };
    return {
      ttfbMs: rtt + ((bytes * 8) / this.upBps) * 1000,
      payloadMs: 0,
      bytes,
      serverMs: this.serverMs,
    };
  }
}

const fast = { ...CLOUDFLARE_SETTINGS, loadedThrottleMs: 0 };

describe("speed method helpers", () => {
  it("reads server time the way Cloudflare's engine does", () => {
    assert.equal(parseServerTime("cfRequestDuration;dur=12.5"), 12.5);
    assert.equal(
      parseServerTime("cfSpeedEdge;dur=3, cfSpeedWorker;dur=32"),
      35,
    );
    assert.equal(
      parseServerTime('cfL4;desc="?proto=TCP&rtt=9655", cfSpeedEdge;dur=3'),
      3,
    );
    assert.equal(parseServerTime("cfSpeedEdge;dur=0"), undefined);
    assert.equal(parseServerTime(undefined), undefined);
  });

  it("interpolates percentiles between ranks", () => {
    assert.equal(percentile([], 0.5), null);
    assert.equal(percentile([3, 1, 2], 0.5), 2);
    assert.equal(
      percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 0.9),
      91,
    );
  });

  it("computes jitter as the mean step between consecutive pings", () => {
    assert.equal(jitter([10]), null);
    assert.equal(jitter([10, 14, 12]), 3);
  });

  it("takes server time out of the round trip, never below zero", () => {
    assert.equal(
      pingOf({ ttfbMs: 20, payloadMs: 0, bytes: 0, serverMs: 5 }),
      15,
    );
    assert.equal(pingOf({ ttfbMs: 2, payloadMs: 0, bytes: 0, serverMs: 5 }), 0);
    assert.equal(
      pingOf({ ttfbMs: 20, payloadMs: 0, bytes: 0, serverMs: undefined }),
      20,
    );
  });

  it("times downloads as ping plus body, uploads as time to response", () => {
    const down = pointOf("down", 1e6, {
      ttfbMs: 20,
      payloadMs: 85,
      bytes: 1e6,
      serverMs: 5,
    });
    assert.equal(down.durationMs, 100);
    assert.equal(down.bps, 80e6);
    const up = pointOf("up", 1e6, {
      ttfbMs: 400,
      payloadMs: 0,
      bytes: 1e6,
      serverMs: 5,
    });
    assert.equal(up.durationMs, 400);
    assert.ok(Math.abs(up.bps - 20.1e6) < 1);
  });
});

describe("measure", () => {
  it("recovers a 100/20 Mbps line and stops ramping once requests pass a second", async () => {
    const line = new SimulatedLine();
    const result = await measure(line, undefined, fast);
    assert.ok(
      Math.abs(result.downloadBps! - 100e6) / 100e6 < 0.01,
      `${result.downloadBps}`,
    );
    assert.ok(
      Math.abs(result.uploadBps! - 20.1e6) / 20.1e6 < 0.01,
      `${result.uploadBps}`,
    );
    assert.equal(result.idleLatencyMs, 15);
    assert.equal(result.idleJitterMs, 0);
    // 25 MB downloads take 2 s at 100 Mbps, so the 100 MB set never runs.
    const bandwidth = line.requests.filter((r) => r.bytes > 0);
    assert.ok(bandwidth.some((r) => r.dir === "down" && r.bytes === 2.5e7));
    assert.ok(!bandwidth.some((r) => r.dir === "down" && r.bytes === 1e8));
    // 10 MB uploads take 4 s at 20 Mbps, so larger uploads never run.
    assert.ok(bandwidth.some((r) => r.dir === "up" && r.bytes === 1e7));
    assert.ok(!bandwidth.some((r) => r.dir === "up" && r.bytes > 1e7));
    assert.equal(
      result.downloadBytes,
      1e5 * 10 + 1e6 * 8 + 1e7 * 6 + 2.5e7 * 4,
    );
    assert.equal(result.uploadBytes, 1e5 * 8 + 1e6 * 6 + 1e7 * 4);
  });

  it("counts loaded latency only from sets that kept the line busy", async () => {
    const line = new SimulatedLine();
    const result = await measure(line, undefined, fast);
    assert.equal(result.downloadLoadedLatencyMs, 60);
    assert.equal(result.uploadLoadedLatencyMs, 60);
    const side = line.requests.filter(
      (r) => r.bytes === 0 && r.during !== "idle",
    );
    assert.ok(side.some((r) => r.during === "download"));
    assert.ok(side.some((r) => r.during === "upload"));
    // 100 kB downloads take about 23 ms here, far under the 250 ms bar, so a
    // run of only short requests has no loaded latency at all.
    const short = await measure(
      new SimulatedLine(),
      [{ type: "down", bytes: 1e5, count: 3 }],
      fast,
    );
    assert.equal(short.downloadLoadedLatencyMs, null);
    assert.equal(short.uploadBps, null);
  });

  it("drops requests too short to time", async () => {
    const result = await measure(
      new SimulatedLine(1e12),
      [{ type: "down", bytes: 1e5, count: 4 }],
      fast,
    );
    // A 15 ms ping plus a near-instant body is still above the 10 ms floor.
    assert.equal(result.points.length, 4);
    const tiny = await measure(
      new SimulatedLine(1e12, 20e6, 1, 1, 0),
      [{ type: "down", bytes: 1e5, count: 4 }],
      fast,
    );
    assert.equal(tiny.points.length, 0);
    assert.equal(tiny.downloadBps, null);
  });

  it("stops when aborted", async () => {
    const stop = new AbortController();
    stop.abort(new Error("stop"));
    await assert.rejects(
      measure(new SimulatedLine(), undefined, fast, stop.signal),
    );
  });
});
