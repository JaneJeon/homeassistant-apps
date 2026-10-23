import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { LogCollector } from "../src/logs.js";
import { Metrics } from "../src/metrics.js";
import { GatewayError } from "../src/client.js";
import { PortalGate, type PortalGateClock } from "../src/portal-gate.js";

const fixture = (name: string) =>
  readFileSync(
    fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)),
    "utf8",
  );
const firewallRows = JSON.parse(fixture("firewall.json")) as Record<
  string,
  unknown
>[];

class ManualGateClock implements PortalGateClock {
  monotonic = 0;
  wall = 1_800_000_000_000;
  readonly sleeps: number[] = [];
  private pending: Array<{
    resolve: () => void;
    reject: (error: Error) => void;
    stop?: AbortSignal;
    abort?: () => void;
  }> = [];
  monotonicMs(): number {
    return this.monotonic;
  }
  wallMs(): number {
    return this.wall;
  }
  sleep(ms: number, stop?: AbortSignal): Promise<void> {
    this.sleeps.push(ms);
    return new Promise<void>((resolve, reject) => {
      const item = {
        resolve,
        reject,
        stop,
        abort: undefined as (() => void) | undefined,
      };
      item.abort = () => {
        this.remove(item);
        reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      };
      if (stop?.aborted) {
        item.abort();
        return;
      }
      stop?.addEventListener("abort", item.abort, { once: true });
      this.pending.push(item);
    });
  }
  advance(ms: number): void {
    this.monotonic += ms;
    this.wall += ms;
    const pending = [...this.pending];
    for (const item of pending) {
      this.remove(item);
      item.resolve();
    }
  }
  private remove(item: (typeof this.pending)[number]): void {
    const index = this.pending.indexOf(item);
    if (index >= 0) this.pending.splice(index, 1);
    if (item.stop && item.abort)
      item.stop.removeEventListener("abort", item.abort);
  }
}

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

describe("LogCollector", () => {
  it("queries Today and Yesterday sequentially, labels snapshots, and keeps repeated raw counts unaccumulated", async () => {
    const lateNightRow = firewallRows[1];
    const responses = [
      { timeFrame: "Today", rows: [firewallRows[0]] },
      { timeFrame: "Yesterday", rows: [] },
      { timeFrame: "Today", rows: [] },
      { timeFrame: "Yesterday", rows: [lateNightRow] },
      { timeFrame: "Today", rows: [] },
      { timeFrame: "Yesterday", rows: [lateNightRow] },
    ];
    const requests: Array<{ kind: string; timeFrame: string }> = [];
    const outputs: Record<string, unknown>[] = [];
    let concurrent = 0;
    let maxConcurrent = 0;
    const client = {
      async logs(kind: string, timeFrame: string) {
        concurrent += 1;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        requests.push({ kind, timeFrame });
        const response = responses[requests.length - 1];
        assert.ok(response);
        assert.equal(response.timeFrame, timeFrame);
        await new Promise((resolve) => setTimeout(resolve, 1));
        concurrent -= 1;
        return JSON.stringify(response.rows);
      },
    };
    const metrics = new Metrics();
    const collector = new LogCollector(client, metrics, (record) =>
      outputs.push(record as Record<string, unknown>),
    );

    await collector.once(["firewall"], 5000);
    await collector.once(["firewall"], 5000);
    await collector.once(["firewall"], 5000);

    assert.deepEqual(
      requests.map(({ kind, timeFrame }) => [kind, timeFrame]),
      [
        ["firewall", "Today"],
        ["firewall", "Yesterday"],
        ["firewall", "Today"],
        ["firewall", "Yesterday"],
        ["firewall", "Today"],
        ["firewall", "Yesterday"],
      ],
    );
    assert.equal(maxConcurrent, 1);

    const queries = outputs.filter(
      (row) => row.event === "xb3_gateway_log_query",
    );
    const snapshots = outputs.filter(
      (row) => row.event === "xb3_gateway_log_snapshot_record",
    );
    assert.equal(queries.length, 6);
    assert.equal(new Set(queries.map((row) => row.query_id)).size, 6);
    assert.equal(snapshots.length, 3);
    assert.deepEqual(
      snapshots.map((row) => [
        row.time_frame,
        (row.record as Record<string, unknown>).Count,
      ]),
      [
        ["Today", firewallRows[0].Count],
        ["Yesterday", lateNightRow.Count],
        ["Yesterday", lateNightRow.Count],
      ],
    );
    const lateNightSnapshots = snapshots.filter(
      (row) =>
        (row.record as Record<string, unknown>).time === lateNightRow.time,
    );
    assert.equal(lateNightSnapshots.length, 2);
    assert.ok(
      lateNightSnapshots.every((row) => row.time_frame === "Yesterday"),
    );
    for (const snapshot of snapshots) {
      const query = queries.find((row) => row.query_id === snapshot.query_id);
      assert.ok(query);
      assert.equal(query.time_frame, snapshot.time_frame);
      assert.equal(query.kind, snapshot.kind);
    }
    assert.equal(metrics.logs.get("firewall:Today")?.timeFrame, "Today");
    assert.equal(metrics.logs.get("firewall:Yesterday")?.recordsReturned, 1);
    assert.equal(metrics.logs.get("firewall:Yesterday")?.success, true);
    assert.equal(
      (lateNightSnapshots[0].record as Record<string, unknown>).Count,
      "144 \n",
    );
  });

  it("does not overlap concurrent once calls", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const requests: string[] = [];
    let concurrent = 0;
    let maxConcurrent = 0;
    const client = {
      async logs(_kind: string, timeFrame: string) {
        requests.push(timeFrame);
        concurrent += 1;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        if (requests.length === 1) await gate;
        concurrent -= 1;
        return "[]";
      },
    };
    const collector = new LogCollector(client, new Metrics(), () => {});
    const first = collector.once(["firewall"], 5000);
    const second = collector.once(["firewall"], 5000);
    await second;
    assert.deepEqual(requests, ["Today"]);
    release();
    await first;
    assert.deepEqual(requests, ["Today", "Yesterday"]);
    assert.equal(maxConcurrent, 1);
  });

  it("records a parse failure separately from a valid empty snapshot", async () => {
    const failedOutputs: Record<string, unknown>[] = [];
    const failedMetrics = new Metrics();
    let failedCalls = 0;
    const invalidClient = {
      async logs() {
        failedCalls += 1;
        return "{}";
      },
    };
    await new LogCollector(invalidClient, failedMetrics, (record) =>
      failedOutputs.push(record as Record<string, unknown>),
    ).once(["firewall"], 5000);
    assert.equal(failedCalls, 1);
    assert.equal(failedMetrics.logs.get("firewall:Today")?.success, false);
    assert.equal(
      failedMetrics.logs.get("firewall:Today")?.recordsReturned,
      null,
    );
    assert.deepEqual(
      failedOutputs
        .filter((row) => row.event === "xb3_gateway_log_query")
        .map((row) => [row.success, row.reason]),
      [[false, "parse_failed"]],
    );
    assert.equal(
      failedOutputs.some(
        (row) => row.event === "xb3_gateway_log_snapshot_record",
      ),
      false,
    );

    const emptyMetrics = new Metrics();
    const emptyOutputs: Record<string, unknown>[] = [];
    await new LogCollector(
      {
        async logs() {
          return "[]";
        },
      },
      emptyMetrics,
      (record) => emptyOutputs.push(record as Record<string, unknown>),
    ).once(["firewall"], 5000);
    assert.equal(emptyMetrics.logs.get("firewall:Today")?.success, true);
    assert.equal(emptyMetrics.logs.get("firewall:Today")?.recordsReturned, 0);
    assert.equal(emptyMetrics.logs.get("firewall:Yesterday")?.success, true);
    assert.equal(
      emptyMetrics.logs.get("firewall:Yesterday")?.recordsReturned,
      0,
    );
    assert.deepEqual(
      emptyOutputs
        .filter((row) => row.event === "xb3_gateway_log_query")
        .map((row) => [row.time_frame, row.success, row.records_returned]),
      [
        ["Today", true, 0],
        ["Yesterday", true, 0],
      ],
    );
  });

  it("stops the entire poll after an ambiguous gateway failure and permits a later retry", async () => {
    for (const code of [
      "timeout",
      "transport_failed",
      "auth_backoff",
    ] as const) {
      const calls: Array<[string, string]> = [];
      const outputs: Record<string, unknown>[] = [];
      let failFirst = true;
      const client = {
        async logs(kind: string, timeFrame: string) {
          calls.push([kind, timeFrame]);
          if (failFirst) {
            failFirst = false;
            throw new GatewayError(code);
          }
          return "[]";
        },
      };
      const metrics = new Metrics();
      const collector = new LogCollector(client, metrics, (record) =>
        outputs.push(record as Record<string, unknown>),
      );

      await collector.once(["system", "event", "firewall"], 5000);
      assert.deepEqual(calls, [["system", "Today"]]);
      assert.deepEqual(
        outputs
          .filter((row) => row.event === "xb3_gateway_log_query")
          .map((row) => row.reason),
        [code],
      );
      assert.equal(metrics.logs.get("system:Today")?.success, false);

      await collector.once(["system", "event", "firewall"], 5000);
      assert.deepEqual(calls.slice(1), [
        ["system", "Today"],
        ["system", "Yesterday"],
        ["event", "Today"],
        ["event", "Yesterday"],
        ["firewall", "Today"],
        ["firewall", "Yesterday"],
      ]);
      assert.equal(metrics.logs.get("system:Today")?.success, true);
      assert.equal(metrics.logs.get("event:Yesterday")?.recordsReturned, 0);
    }
  });

  it("waits behind metrics and holds the shared portal gate across Today and Yesterday", async () => {
    const clock = new ManualGateClock();
    const access = new PortalGate({ clock });
    const metricsLease = access.tryAcquireMetrics();
    assert.ok(metricsLease);
    const calls: string[] = [];
    const client = {
      async logs(_kind: string, timeFrame: string) {
        calls.push(timeFrame);
        assert.equal(access.state.owner, "logs");
        assert.equal(access.tryAcquireMetrics(), null);
        return "[]";
      },
    };
    const collector = new LogCollector(client, new Metrics(), () => {}, access);
    const pending = collector.once(["firewall"], 5000);
    assert.equal(access.state.logsWaiting, true);
    assert.deepEqual(calls, []);
    metricsLease.release();
    clock.advance(1000);
    await pending;
    assert.deepEqual(calls, ["Today", "Yesterday"]);
    assert.equal(access.state.owner, null);
    assert.equal(access.state.logsWaiting, false);
  });

  it("cleans up an aborted waiting run so a later run can call the client", async () => {
    const clock = new ManualGateClock();
    const access = new PortalGate({ clock });
    const metricsLease = access.tryAcquireMetrics();
    assert.ok(metricsLease);
    const calls: string[] = [];
    const collector = new LogCollector(
      {
        async logs(_kind: string, timeFrame: string) {
          calls.push(timeFrame);
          return "[]";
        },
      },
      new Metrics(),
      () => {},
      access,
    );
    const controller = new AbortController();
    const waiting = collector.once(["firewall"], 5000, controller.signal);
    assert.equal(access.state.logsWaiting, true);
    controller.abort();
    await waiting;
    assert.deepEqual(calls, []);
    assert.equal(access.state.logsWaiting, false);
    metricsLease.release();

    await collector.once(["firewall"], 5000);
    assert.deepEqual(calls, ["Today", "Yesterday"]);
  });

  it("keeps a shared cooldown after a log timeout or transport failure releases its lease", async () => {
    for (const code of ["timeout", "transport_failed"] as const) {
      const clock = new ManualGateClock();
      const access = new PortalGate({ clock });
      const calls: string[] = [];
      const collector = new LogCollector(
        {
          async logs(_kind: string, timeFrame: string) {
            calls.push(timeFrame);
            throw new GatewayError(code);
          },
        },
        new Metrics(),
        () => {},
        access,
      );
      await collector.once(["firewall"], 5000);
      assert.deepEqual(calls, ["Today"]);
      assert.equal(access.state.owner, null);
      assert.equal(access.state.cooldownRemainingSeconds, 60);
      assert.equal(access.tryAcquireMetrics(), null);
    }
  });
});
