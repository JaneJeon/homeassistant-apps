import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, describe, it } from "node:test";
import { measure, type MethodSettings } from "../src/speed-measure.js";
import { HttpsSpeedTransport } from "../src/speed-transport.js";

/** Bytes per second the stand-in line carries in each direction. */
const RATE = 1_000_000;

/**
 * A stand-in for Cloudflare's endpoints on a slow line. Like the real upload
 * endpoint, it answers with a Server-Timing that includes the time spent
 * receiving the body: 0.4.0 subtracted that and read uploads as instant.
 */
function standIn(): Server {
  const active = { down: 0, up: 0 };
  return createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    if (url.pathname === "/__down") {
      let left = Number(url.searchParams.get("bytes"));
      active.down++;
      res.writeHead(200, { "cf-ray": "test-LCL" });
      const tick = setInterval(() => {
        const n = Math.min(left, Math.round(RATE / 100 / active.down));
        left -= n;
        res.write(Buffer.alloc(n));
        if (left <= 0) {
          clearInterval(tick);
          active.down--;
          res.end();
        }
      }, 10);
      res.on("close", () => {
        if (left > 0) {
          clearInterval(tick);
          active.down--;
        }
      });
      return;
    }
    const started = performance.now();
    active.up++;
    let allowance = 0;
    req.pause();
    const tick = setInterval(() => {
      allowance += RATE / 100 / active.up;
      if (allowance > 0) req.resume();
    }, 10);
    req.on("data", (chunk: Buffer) => {
      allowance -= chunk.length;
      if (allowance <= 0) req.pause();
    });
    req.on("end", () => {
      clearInterval(tick);
      active.up--;
      const receiving = Math.round(performance.now() - started);
      res.writeHead(200, {
        "cf-ray": "test-LCL",
        "server-timing": `cfSpeedEdge;dur=1, cfSpeedWorker;dur=${receiving}`,
      });
      res.end("ok");
    });
  });
}

const SETTINGS: MethodSettings = {
  durationMs: 2_000,
  warmupMs: 500,
  binMs: 250,
  connections: 2,
  downloadRequestBytes: 10_000_000,
  uploadChunkTargetMs: 250,
  uploadFirstChunkBytes: 32_768,
  uploadMinChunkBytes: 16_384,
  uploadMaxChunkBytes: 1_048_576,
  uploadGraceMs: 3_000,
  gapMs: 100,
};

describe("HttpsSpeedTransport with measure", () => {
  let server: Server;
  let base: string;
  before(async () => {
    server = standIn();
    await new Promise<void>((resolve) => server.listen(0, resolve));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });
  after(() => {
    server.closeAllConnections();
    server.close();
  });

  it("reads both directions at the line's speed, server timing or not", async () => {
    const transport = new HttpsSpeedTransport(SETTINGS.connections, base);
    try {
      const result = await measure(transport, SETTINGS);
      const bps = RATE * 8;
      for (const [name, d] of Object.entries({
        download: result.download,
        upload: result.upload,
      })) {
        assert.ok(
          Math.abs(d.sustainedBps! - bps) / bps < 0.3,
          `${name} sustained ${d.sustainedBps}`,
        );
        // No reading may exceed the line by much: instant bursts mean the
        // transfer time was lost.
        assert.ok(
          Math.max(...d.readingsBps) < bps * 1.6,
          `${name} max ${Math.max(...d.readingsBps)}`,
        );
        assert.equal(d.readingsBps.length, 6, name);
      }
      assert.equal(transport.colo, "LCL");
    } finally {
      transport.close();
    }
  });
});
