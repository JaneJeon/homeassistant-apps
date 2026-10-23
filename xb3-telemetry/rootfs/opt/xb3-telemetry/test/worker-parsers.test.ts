import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { ParserWorkerError, WorkerParsers } from "../src/worker-parsers.js";
import type { ParserWorkerLike } from "../src/worker-parsers.js";
import { parseNetwork } from "../src/parser.js";
import type { NetworkSnapshot } from "../src/types.js";

const fixture = (name: string) =>
  readFileSync(
    fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)),
    "utf8",
  );
const networkHtml = fixture("network-partial.html");
const networkSnapshot = parseNetwork(networkHtml);

class FakeWorker implements ParserWorkerLike {
  readonly messages: unknown[] = [];
  terminateCount = 0;
  private messageListener?: (message: unknown) => void;
  private errorListener?: (error: unknown) => void;
  private exitListener?: (code: number) => void;

  postMessage(message: unknown): void {
    this.messages.push(message);
  }

  onMessage(listener: (message: unknown) => void): void {
    this.messageListener = listener;
  }

  onError(listener: (error: unknown) => void): void {
    this.errorListener = listener;
  }

  onExit(listener: (code: number) => void): void {
    this.exitListener = listener;
  }

  terminate(): void {
    this.terminateCount += 1;
  }

  respond(message: unknown): void {
    this.messageListener?.(message);
  }

  fail(error: unknown): void {
    this.errorListener?.(error);
  }

  exit(code: number): void {
    this.exitListener?.(code);
  }
}

function requestId(worker: FakeWorker): number {
  const message = worker.messages[worker.messages.length - 1] as
    | { id?: unknown }
    | undefined;
  assert.equal(typeof message?.id, "number");
  return message.id as number;
}

describe("WorkerParsers", () => {
  it("starts lazily, returns typed parsed data, and rejects a concurrent request without queueing", async () => {
    const worker = new FakeWorker();
    let starts = 0;
    const parsers = new WorkerParsers({
      workerFactory: () => {
        starts += 1;
        return worker;
      },
    });
    assert.equal(starts, 0);

    const pending = parsers.network(networkHtml);
    assert.equal(starts, 1);
    assert.deepEqual(worker.messages[0], {
      id: 1,
      kind: "network",
      html: networkHtml,
    });
    await assert.rejects(
      parsers.hardware("<html>private secret</html>"),
      (error: unknown) =>
        error instanceof ParserWorkerError && error.code === "busy",
    );
    assert.equal(worker.messages.length, 1);

    const id = requestId(worker);
    worker.respond({ id, data: networkSnapshot });
    assert.deepEqual(await pending, networkSnapshot);
    assert.equal(worker.terminateCount, 0);
    parsers.close();
    assert.equal(worker.terminateCount, 1);
  });

  it("times out safely, ignores late replies from the old worker, and restarts lazily", async () => {
    const oldWorker = new FakeWorker();
    const nextWorker = new FakeWorker();
    const workers = [oldWorker, nextWorker];
    let starts = 0;
    const parsers = new WorkerParsers({
      timeoutMs: 5,
      workerFactory: () => {
        const worker = workers[starts];
        starts += 1;
        assert.ok(worker);
        return worker;
      },
    });
    const first = parsers.network("<html>private response text</html>");
    const staleId = requestId(oldWorker);
    await assert.rejects(
      first,
      (error: unknown) =>
        error instanceof ParserWorkerError && error.code === "timeout",
    );
    assert.equal(oldWorker.terminateCount, 1);

    oldWorker.respond({ id: staleId, data: { leaked: "late" } });
    const second = parsers.network(networkHtml);
    assert.equal(starts, 2);
    const freshId = requestId(nextWorker);
    nextWorker.respond({ id: freshId - 1, data: { stale: true } });
    nextWorker.respond({ id: freshId, data: networkSnapshot });
    assert.deepEqual(await second, networkSnapshot);
    assert.equal(nextWorker.terminateCount, 0);
    parsers.close();
  });

  it("rejects worker error and exit once, resets the instance, and allows a fresh worker", async () => {
    for (const failure of ["error", "exit"] as const) {
      const failedWorker = new FakeWorker();
      const replacement = new FakeWorker();
      const workers = [failedWorker, replacement];
      let starts = 0;
      const parsers = new WorkerParsers({
        workerFactory: () => {
          const worker = workers[starts];
          starts += 1;
          assert.ok(worker);
          return worker;
        },
      });
      const pending = parsers.network(networkHtml);
      if (failure === "error")
        failedWorker.fail(new Error("unlogged private HTML"));
      else failedWorker.exit(9);
      await assert.rejects(
        pending,
        (error: unknown) =>
          error instanceof ParserWorkerError &&
          error.code === (failure === "error" ? "worker_error" : "worker_exit"),
      );
      assert.equal(failedWorker.terminateCount, 1);

      const next = parsers.network(networkHtml);
      const id = requestId(replacement);
      replacement.respond({ id, data: networkSnapshot });
      assert.deepEqual(await next, networkSnapshot);
      assert.equal(starts, 2);
      parsers.close();
    }
  });

  it("does not respawn after close and rejects an active request", async () => {
    const worker = new FakeWorker();
    let starts = 0;
    const parsers = new WorkerParsers({
      workerFactory: () => {
        starts += 1;
        return worker;
      },
    });
    const pending = parsers.network(networkHtml);
    const rejected = assert.rejects(
      pending,
      (error: unknown) =>
        error instanceof ParserWorkerError && error.code === "closed",
    );
    parsers.close();
    await rejected;
    assert.equal(worker.terminateCount, 1);
    await assert.rejects(
      parsers.lan(networkHtml),
      (error: unknown) =>
        error instanceof ParserWorkerError && error.code === "closed",
    );
    assert.equal(starts, 1);
  });

  it("returns bounded parse errors without worker exception text and can reuse the worker", async () => {
    const worker = new FakeWorker();
    const parsers = new WorkerParsers({ workerFactory: () => worker });
    const rejected = parsers.network(networkHtml);
    worker.respond({ id: requestId(worker), error: "parse_failed" });
    await assert.rejects(
      rejected,
      (error: unknown) =>
        error instanceof ParserWorkerError &&
        error.code === "parse_failed" &&
        error.message === "parse_failed" &&
        !error.message.includes("network"),
    );

    const next = parsers.network(networkHtml);
    worker.respond({ id: requestId(worker), data: networkSnapshot });
    assert.deepEqual(await next, networkSnapshot);
    assert.equal(worker.terminateCount, 0);
    parsers.close();
  });
});
