import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { classifyPing, createPingProbe } from "../src/probe-driver.js";

const healthy = `PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.\n64 bytes from 192.0.2.1: icmp_seq=0 ttl=64 time=0.085 ms\n--- 192.0.2.1 ping statistics ---\n1 packets transmitted, 1 packets received, 0% packet loss\nround-trip min/avg/max/stddev = 0.085/0.085/0.085/0.000 ms`;
const loss = `PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.\n--- 192.0.2.1 ping statistics ---\n1 packets transmitted, 0 packets received, 100% packet loss`;

describe("classifyPing", () => {
  it("accepts only the recognized successful counters and finite RTT", () => {
    assert.deepEqual(classifyPing(healthy, 0), {
      outcome: "reply",
      rttMs: 0.085,
    });
    assert.equal(
      classifyPing(healthy.replace("time=0.085 ms", "time=NaN ms"), 0).outcome,
      "local_error",
    );
    assert.equal(
      classifyPing(healthy.replace("time=0.085 ms", "time<1 ms"), 0).outcome,
      "local_error",
    );
    assert.equal(
      classifyPing(healthy.replace("time=0.085 ms", ""), 0).outcome,
      "local_error",
    );
  });

  it("accepts only explicit one-packet, total-loss exit 1 as no reply", () => {
    assert.deepEqual(classifyPing(loss, 1), {
      outcome: "no_reply_by_deadline",
      rttMs: null,
    });
    assert.equal(
      classifyPing("ping: permission denied", 1).outcome,
      "local_error",
    );
    assert.equal(classifyPing(loss, 2).outcome, "local_error");
    assert.equal(
      classifyPing(loss.replace("100%", "50%"), 1).outcome,
      "local_error",
    );
  });

  it("bounds the reason for malformed or unexpected output", () => {
    const result = classifyPing("x".repeat(9000), 0);
    assert.equal(result.outcome, "local_error");
    assert.ok((result.reason?.length ?? 0) <= 160);
  });
});

describe("pingProbe", () => {
  it("uses a direct, bounded ping invocation and classifies callback exit status", async () => {
    let invocation:
      | { file: string; args: string[]; options: Record<string, unknown> }
      | undefined;
    const probe = createPingProbe((file, args, options, callback) => {
      invocation = { file, args, options };
      callback(Object.assign(new Error("exit 1"), { code: 1 }), loss, "");
    });
    assert.deepEqual(await probe("192.0.2.1"), {
      outcome: "no_reply_by_deadline",
      rttMs: null,
    });
    assert.equal(invocation?.file, "/bin/ping");
    assert.deepEqual(invocation?.args, [
      "-4",
      "-n",
      "-c",
      "1",
      "-W",
      "1",
      "192.0.2.1",
    ]);
    assert.equal(invocation?.args.includes("-w"), false);
    assert.equal(invocation?.options.timeout, 1500);
    assert.equal(invocation?.options.killSignal, "SIGKILL");
    assert.equal(invocation?.options.maxBuffer, 8192);
    assert.equal((invocation?.options.env as NodeJS.ProcessEnv).LC_ALL, "C");
  });

  it("rejects non-IPv4 inputs without spawning", async () => {
    let spawned = false;
    const probe = createPingProbe(() => {
      spawned = true;
    });
    assert.equal((await probe("example.com")).outcome, "local_error");
    assert.equal(spawned, false);
  });

  it("maps spawn errors and aborts to bounded local errors", async () => {
    const spawnFailure = createPingProbe((_file, _args, _options, callback) => {
      callback(Object.assign(new Error("missing"), { code: "ENOENT" }), "", "");
    });
    assert.deepEqual(await spawnFailure("192.0.2.1"), {
      outcome: "local_error",
      rttMs: null,
      reason: "ping executable unavailable",
    });

    const controller = new AbortController();
    controller.abort();
    assert.equal(
      (await createPingProbe()("192.0.2.1", controller.signal)).reason,
      "probe aborted",
    );
  });

  it("treats permission failures, process kills, and output overflow as local errors", async () => {
    for (const error of [
      Object.assign(new Error("permission denied"), { code: 1 }),
      Object.assign(new Error("parent killed child"), {
        code: 1,
        signal: "SIGKILL",
        killed: true,
      }),
      Object.assign(new Error("buffer overflow"), {
        code: "ERR_CHILD_PROCESS_STDIO_MAXBUFFER",
      }),
    ]) {
      const probe = createPingProbe((_file, _args, _options, callback) => {
        callback(error, loss, "permission denied");
      });
      assert.equal((await probe("192.0.2.1")).outcome, "local_error");
    }
    const killedWithRecognizedSummary = createPingProbe(
      (_file, _args, _options, callback) => {
        callback(
          Object.assign(new Error("killed"), {
            code: 1,
            signal: "SIGKILL",
            killed: true,
          }),
          loss,
          "",
        );
      },
    );
    assert.equal(
      (await killedWithRecognizedSummary("192.0.2.1")).outcome,
      "local_error",
    );
  });

  it("passes the parent signal through to the spawned process", async () => {
    const controller = new AbortController();
    let receivedSignal: AbortSignal | undefined;
    const probe = createPingProbe((_file, _args, options, callback) => {
      receivedSignal = options.signal;
      callback(null, healthy, "");
    });
    assert.equal(
      (await probe("192.0.2.1", controller.signal)).outcome,
      "reply",
    );
    assert.equal(receivedSignal, controller.signal);
  });
});
