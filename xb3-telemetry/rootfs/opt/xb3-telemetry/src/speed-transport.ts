import { Agent, request as httpsRequest } from "node:https";
import type { TLSSocket } from "node:tls";
import {
  parseServerTime,
  type Direction,
  type Phase,
  type RequestTiming,
  type SpeedTransport,
} from "./speed-measure.js";

const CHUNK = Buffer.alloc(64 * 1024, "0");
const REQUEST_TIMEOUT_MS = 60_000;

/**
 * Speed-test requests against Cloudflare's measurement endpoints.
 *
 * Bandwidth and idle latency share one keep-alive connection; loaded latency uses
 * a second, so it can run while a transfer is in progress. Timing starts once the
 * request's connection is established, matching the browser's `requestStart`.
 * Download bodies are counted and discarded; upload bodies repeat one 64 KiB
 * buffer, so memory stays flat at any test size.
 */
export class HttpsSpeedTransport implements SpeedTransport {
  private readonly main = new Agent({ keepAlive: true, maxSockets: 1 });
  private readonly side = new Agent({ keepAlive: true, maxSockets: 1 });
  /** Cloudflare site that answered the latest request, from the CF-RAY suffix. */
  colo: string | null = null;

  constructor(
    private readonly base = "https://speed.cloudflare.com",
    private readonly now = () => performance.now(),
  ) {}

  request(
    dir: Direction,
    bytes: number,
    during: Phase,
    signal?: AbortSignal,
  ): Promise<RequestTiming> {
    const sideLatency = bytes === 0 && during !== "idle";
    const url = new URL(dir === "down" ? "/__down" : "/__up", this.base);
    if (dir === "down") url.searchParams.set("bytes", String(bytes));
    if (bytes === 0) url.searchParams.set("during", during);
    return new Promise<RequestTiming>((resolve, reject) => {
      let start: number | undefined;
      let headersAt = 0;
      let settled = false;
      const fail = (error: Error) => {
        if (settled) return;
        settled = true;
        req.destroy();
        reject(error);
      };
      const req = httpsRequest(url, {
        method: dir === "down" ? "GET" : "POST",
        agent: sideLatency ? this.side : this.main,
        headers:
          dir === "up"
            ? {
                "Content-Type": "text/plain;charset=UTF-8",
                "Content-Length": String(bytes),
              }
            : {},
        signal,
        timeout: REQUEST_TIMEOUT_MS,
      });
      req.on("socket", (socket) => {
        const tls = socket as TLSSocket;
        // A reused keep-alive socket is ready now; a new one is ready after TLS.
        if (req.reusedSocket || (!tls.connecting && tls.encrypted)) {
          start = this.now();
          write();
        } else
          tls.once("secureConnect", () => {
            start = this.now();
            write();
          });
      });
      const write = () => {
        if (dir === "down") return void req.end();
        let remaining = bytes;
        const pump = () => {
          while (remaining > 0) {
            const size = Math.min(remaining, CHUNK.length);
            remaining -= size;
            const more = req.write(
              size === CHUNK.length ? CHUNK : CHUNK.subarray(0, size),
            );
            if (!more) return void req.once("drain", pump);
          }
          req.end();
        };
        pump();
      };
      req.on("timeout", () => fail(new Error("request_timeout")));
      req.on("error", (error) => fail(error));
      req.on("response", (res) => {
        headersAt = this.now();
        const ray = res.headers["cf-ray"];
        const site =
          typeof ray === "string" ? ray.split("-").at(-1) : undefined;
        if (site && /^[A-Z]{3}$/.test(site)) this.colo = site;
        const status = res.statusCode ?? 0;
        if (status < 200 || status >= 300) {
          res.resume();
          return fail(new Error(`http_${status}`));
        }
        const serverTiming = res.headers["server-timing"];
        const serverMs = parseServerTime(
          Array.isArray(serverTiming) ? serverTiming.join(", ") : serverTiming,
        );
        let received = 0;
        res.on("data", (chunk: Buffer) => (received += chunk.length));
        res.on("error", (error) => fail(error));
        res.on("end", () => {
          if (settled) return;
          settled = true;
          const end = this.now();
          resolve({
            ttfbMs: headersAt - (start ?? headersAt),
            payloadMs: end - headersAt,
            bytes: dir === "down" ? received : bytes,
            serverMs,
          });
        });
      });
    });
  }

  close(): void {
    this.main.destroy();
    this.side.destroy();
  }
}
