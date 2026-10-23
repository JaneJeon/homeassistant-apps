import { Worker } from "node:worker_threads";
import type {
  HardwareSnapshot,
  LanSnapshot,
  NetworkSnapshot,
} from "./types.js";

export type ParserKind = "network" | "hardware" | "lan";
export type ParserWorkerErrorCode =
  | "busy"
  | "closed"
  | "timeout"
  | "worker_error"
  | "worker_exit"
  | "worker_protocol"
  | "worker_start_failed"
  | "parse_failed"
  | "invalid_request"
  | "response_too_large";

export class ParserWorkerError extends Error {
  constructor(readonly code: ParserWorkerErrorCode) {
    super(code);
    this.name = "ParserWorkerError";
  }
}

export interface ParserWorkerLike {
  postMessage(message: unknown): void;
  onMessage(listener: (message: unknown) => void): void;
  onError(listener: (error: unknown) => void): void;
  onExit(listener: (code: number) => void): void;
  terminate(): void;
}

export type ParserWorkerFactory = () => ParserWorkerLike;

export interface WorkerParsersOptions {
  timeoutMs?: number;
  workerFactory?: ParserWorkerFactory;
}

interface ParseRequest {
  id: number;
  kind: ParserKind;
  html: string;
}

interface PendingRequest {
  id: number;
  worker: ParserWorkerLike;
  timer: ReturnType<typeof setTimeout>;
  resolve(value: unknown): void;
  reject(error: ParserWorkerError): void;
}

class NodeParserWorker implements ParserWorkerLike {
  constructor(private readonly worker: Worker) {}

  postMessage(message: unknown): void {
    this.worker.postMessage(message);
  }

  onMessage(listener: (message: unknown) => void): void {
    this.worker.on("message", listener);
  }

  onError(listener: (error: unknown) => void): void {
    this.worker.on("error", listener);
  }

  onExit(listener: (code: number) => void): void {
    this.worker.on("exit", listener);
  }

  terminate(): void {
    void this.worker.terminate().catch(() => undefined);
  }
}

const defaultWorkerFactory: ParserWorkerFactory = () =>
  new NodeParserWorker(
    new Worker(new URL("./parser-worker.js", import.meta.url)),
  );

export class WorkerParsers {
  private readonly timeoutMs: number;
  private readonly workerFactory: ParserWorkerFactory;
  private worker?: ParserWorkerLike;
  private pending?: PendingRequest;
  private nextId = 1;
  private closed = false;

  constructor(options: WorkerParsersOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? 5_000;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs <= 0)
      throw new Error("timeoutMs must be a positive finite number");
    this.workerFactory = options.workerFactory ?? defaultWorkerFactory;
  }

  network(html: string): Promise<NetworkSnapshot> {
    return this.parse<NetworkSnapshot>("network", html);
  }

  hardware(html: string): Promise<HardwareSnapshot> {
    return this.parse<HardwareSnapshot>("hardware", html);
  }

  lan(html: string): Promise<LanSnapshot> {
    return this.parse<LanSnapshot>("lan", html);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    const worker = this.worker;
    this.worker = undefined;
    if (worker) {
      this.rejectPending(worker, new ParserWorkerError("closed"));
      worker.terminate();
    } else if (this.pending) {
      this.clearPending(new ParserWorkerError("closed"));
    }
  }

  private parse<T>(kind: ParserKind, html: string): Promise<T> {
    if (this.closed) return Promise.reject(new ParserWorkerError("closed"));
    if (this.pending) return Promise.reject(new ParserWorkerError("busy"));
    if (typeof html !== "string")
      return Promise.reject(new ParserWorkerError("invalid_request"));
    if (Buffer.byteLength(html, "utf8") > 2 * 1024 * 1024)
      return Promise.reject(new ParserWorkerError("response_too_large"));

    let worker: ParserWorkerLike;
    try {
      worker = this.getWorker();
    } catch {
      return Promise.reject(new ParserWorkerError("worker_start_failed"));
    }

    const request: ParseRequest = { id: this.nextId++, kind, html };
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(
        () => this.timeoutRequest(worker, request.id),
        this.timeoutMs,
      );
      const pending: PendingRequest = {
        id: request.id,
        worker,
        timer,
        resolve: (value) => resolve(value as T),
        reject,
      };
      this.pending = pending;
      try {
        worker.postMessage(request);
      } catch {
        this.failWorker(worker, "worker_error");
      }
    });
  }

  private getWorker(): ParserWorkerLike {
    if (this.worker) return this.worker;
    if (this.closed) throw new Error("closed");
    const worker = this.workerFactory();
    this.worker = worker;
    worker.onMessage((message) => this.receive(worker, message));
    worker.onError(() => this.failWorker(worker, "worker_error"));
    worker.onExit(() => this.failWorker(worker, "worker_exit"));
    return worker;
  }

  private receive(worker: ParserWorkerLike, value: unknown): void {
    if (
      this.worker !== worker ||
      !this.pending ||
      this.pending.worker !== worker
    )
      return;
    if (!value || typeof value !== "object") {
      this.failWorker(worker, "worker_protocol");
      return;
    }
    const response = value as {
      id?: unknown;
      data?: unknown;
      error?: unknown;
    };
    const pending = this.pending;
    if (response.id !== pending.id) return;
    if (response.error === "parse_failed") {
      this.clearPending(new ParserWorkerError("parse_failed"));
      return;
    }
    if (!("data" in response)) {
      this.failWorker(worker, "worker_protocol");
      return;
    }
    this.pending = undefined;
    clearTimeout(pending.timer);
    pending.resolve(response.data);
  }

  private timeoutRequest(worker: ParserWorkerLike, id: number): void {
    if (
      this.worker !== worker ||
      this.pending?.worker !== worker ||
      this.pending.id !== id
    )
      return;
    this.worker = undefined;
    this.clearPending(new ParserWorkerError("timeout"));
    worker.terminate();
  }

  private failWorker(
    worker: ParserWorkerLike,
    code: "worker_error" | "worker_exit" | "worker_protocol",
  ): void {
    if (this.worker !== worker) return;
    this.worker = undefined;
    this.rejectPending(worker, new ParserWorkerError(code));
    worker.terminate();
  }

  private rejectPending(
    worker: ParserWorkerLike,
    error: ParserWorkerError,
  ): void {
    if (this.pending?.worker !== worker) return;
    this.clearPending(error);
  }

  private clearPending(error: ParserWorkerError): void {
    const pending = this.pending;
    if (!pending) return;
    this.pending = undefined;
    clearTimeout(pending.timer);
    pending.reject(error);
  }
}
