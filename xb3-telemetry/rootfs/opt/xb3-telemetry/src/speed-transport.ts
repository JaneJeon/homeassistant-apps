import { Agent, request as httpsRequest } from "node:https";
import type { ClientRequest, IncomingMessage } from "node:http";
import type { TLSSocket } from "node:tls";
import {
  HttpError,
  parseRetryAfter,
  parseServerTime,
  type StreamTransport,
  type UploadTiming,
} from "./speed-measure.js";

const CHUNK = Buffer.alloc(64 * 1024, "0");
const REQUEST_TIMEOUT_MS = 60_000;

/**
 * Requests against Cloudflare's speed-test endpoints (`/__down`, `/__up`).
 *
 * Connections are kept alive and reused, up to `connections` at a time.
 * Download bodies are counted as they arrive and discarded; upload bodies
 * repeat one 64 KiB buffer, so memory stays flat at any size.
 */
export class HttpsSpeedTransport implements StreamTransport {
  private readonly agent: Agent;
  /** Cloudflare site that answered the latest request, from the CF-RAY suffix. */
  colo: string | null = null;

  constructor(
    connections: number,
    private readonly base = "https://speed.cloudflare.com",
  ) {
    this.agent = new Agent({ keepAlive: true, maxSockets: connections });
  }

  now(): number {
    return performance.now();
  }

  private open(
    method: "GET" | "POST",
    url: URL,
    headers: Record<string, string>,
    signal: AbortSignal,
    onReady: (req: ClientRequest) => void,
    onResponse: (res: IncomingMessage, settle: Settle) => void,
  ): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      let settled = false;
      const settle: Settle = {
        ok: () => {
          if (settled) return;
          settled = true;
          resolve();
        },
        fail: (error: Error) => {
          if (settled) return;
          settled = true;
          req.destroy();
          reject(error);
        },
      };
      const req = httpsRequest(url, {
        method,
        agent: this.agent,
        headers,
        signal,
        timeout: REQUEST_TIMEOUT_MS,
      });
      req.on("socket", (socket) => {
        const tls = socket as TLSSocket;
        // A reused keep-alive socket is ready now; a new one is ready after TLS.
        if (req.reusedSocket || (!tls.connecting && tls.encrypted))
          onReady(req);
        else tls.once("secureConnect", () => onReady(req));
      });
      req.on("timeout", () => settle.fail(new Error("request_timeout")));
      req.on("error", (error) => settle.fail(error));
      req.on("response", (res) => {
        const ray = res.headers["cf-ray"];
        const site =
          typeof ray === "string" ? ray.split("-").at(-1) : undefined;
        if (site && /^[A-Z]{3}$/.test(site)) this.colo = site;
        const status = res.statusCode ?? 0;
        if (status < 200 || status >= 300) {
          res.resume();
          const retryAfter = res.headers["retry-after"];
          return settle.fail(
            new HttpError(
              status,
              parseRetryAfter(
                Array.isArray(retryAfter) ? retryAfter[0] : retryAfter,
                Date.now(),
              ),
            ),
          );
        }
        res.on("error", (error) => settle.fail(error));
        onResponse(res, settle);
      });
    });
  }

  download(
    bytes: number,
    onBytes: (count: number, atMs: number) => void,
    signal: AbortSignal,
  ): Promise<void> {
    const url = new URL("/__down", this.base);
    url.searchParams.set("bytes", String(bytes));
    return this.open(
      "GET",
      url,
      {},
      signal,
      (req) => req.end(),
      (res, settle) => {
        res.on("data", (chunk: Buffer) => onBytes(chunk.length, this.now()));
        res.on("end", () => settle.ok());
      },
    );
  }

  async upload(bytes: number, signal: AbortSignal): Promise<UploadTiming> {
    let startMs = 0;
    let endMs = 0;
    let serverMs: number | undefined;
    await this.open(
      "POST",
      new URL("/__up", this.base),
      {
        "Content-Type": "text/plain;charset=UTF-8",
        "Content-Length": String(bytes),
      },
      signal,
      (req) => {
        startMs = this.now();
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
      },
      (res, settle) => {
        endMs = this.now();
        const timing = res.headers["server-timing"];
        serverMs = parseServerTime(
          Array.isArray(timing) ? timing.join(", ") : timing,
        );
        res.resume();
        res.on("end", () => settle.ok());
      },
    );
    return { startMs, endMs, serverMs };
  }

  close(): void {
    this.agent.destroy();
  }
}

interface Settle {
  ok(): void;
  fail(error: Error): void;
}
