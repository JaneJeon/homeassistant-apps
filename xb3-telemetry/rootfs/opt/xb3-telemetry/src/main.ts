import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { networkInterfaces } from "node:os";
import { setTimeout as delay } from "node:timers/promises";
import { GatewayClient } from "./client.js";
import { Collector, type Collection, type PageResult } from "./collector.js";
import { loadOptions } from "./config.js";
import { GatewaySampler } from "./gateway-sampler.js";
import { WorkerParsers } from "./worker-parsers.js";
import { Correlator } from "./correlation.js";
import { LogCollector } from "./logs.js";
import { Metrics } from "./metrics.js";
import { PortalGate } from "./portal-gate.js";
import {
  ProbeRecorder,
  PROBE_TARGETS,
  renderProbeMetrics,
  validationDriver,
} from "./probes.js";
import { SpeedRunner } from "./speed-runner.js";

const VERSION = "0.3.0";

function emptyCollection(): Collection {
  const page = <T>(): PageResult<T> => ({
    data: null,
    durationSeconds: 0,
    observedAtSeconds: 0,
    error: "not_collected_yet",
  });
  return { network: page(), hardware: page(), lan: page(), durationSeconds: 0 };
}
function currentCollection(
  collection: Collection | null,
  interval: number,
): Collection {
  const value = collection ?? emptyCollection();
  const now = Date.now() / 1000;
  const fresh = <T>(page: PageResult<T>, maxAge: number): PageResult<T> =>
    page.data !== null &&
    (page.observedAtMonotonicMs !== undefined
      ? (performance.now() - page.observedAtMonotonicMs) / 1000
      : now - page.observedAtSeconds) > maxAge
      ? { ...page, data: null, error: "observation_expired" }
      : page;
  return {
    ...value,
    network: fresh(value.network, Math.max(90, interval * 2)),
    hardware: fresh(value.hardware, 180),
    lan: fresh(value.lan, 180),
  };
}

async function main(): Promise<void> {
  const options = await loadOptions();
  const startupId = randomUUID();
  const startupSeconds = Date.now() / 1000;
  let droppedLogRecords = 0;
  let correlator: Correlator | undefined;
  const write = (record: object) => {
    if (process.stdout.writableLength > 1_048_576) {
      droppedLogRecords++;
      return;
    }
    const event = {
      startup_id: startupId,
      startup_seconds: startupSeconds,
      ...record,
    };
    process.stdout.write(JSON.stringify(event) + "\n");
    correlator?.observe(event);
  };
  correlator = new Correlator(write);
  const client = () =>
    new GatewayClient(
      options.gatewayUrl,
      options.username,
      options.password,
      12_000,
    );
  const metrics = new Metrics();
  const access = new PortalGate();
  const collector = new Collector(client());
  const logs = new LogCollector(client(), metrics, write, access);
  const kinds =
    options.logMode === "all"
      ? (["system", "event", "firewall"] as const)
      : (["firewall"] as const);
  if (process.argv.includes("--logs-once")) {
    await logs.once([...kinds], 150_000);
    if ([...metrics.logs.values()].some((state) => !state.success))
      process.exitCode = 1;
    return;
  }
  if (process.argv.includes("--once")) {
    const result = await collector.collect();
    metrics.observe(result);
    process.stdout.write(metrics.render(result));
    return;
  }
  const stop = new AbortController();
  const probes = new ProbeRecorder({ startupId, startupSeconds, write });
  const workerParsers = new WorkerParsers();
  let validation: ProbeRecorder | undefined;
  let validationRequested = false;
  let gatewayWorkerRunning = true;
  const sampler = new GatewaySampler(new Collector(client(), workerParsers), {
    startupId,
    startupSeconds,
    networkIntervalMs: options.networkIntervalSeconds * 1000,
    auxiliaryIntervalMs: 60_000,
    access,
    write,
    onObservation: (collection) => metrics.observe(collection),
  });
  const speed = new SpeedRunner({
    intervalMinutes: options.speedtestIntervalMinutes,
    write,
  });
  const gauge = (name: string, help: string, value: number) =>
    `# HELP ${name} ${help}\n# TYPE ${name} gauge\n${name} ${value}\n`;
  const server = createServer((request, response) => {
    if (request.method !== "GET") {
      response.writeHead(405, { Allow: "GET" }).end();
      return;
    }
    if (request.url === "/healthz") {
      response.writeHead(200, { "Content-Type": "text/plain" }).end("ok\n");
      return;
    }
    // Read-only latest speed result; tests start only through Supervisor stdin.
    if (request.url === "/speedtest") {
      response
        .writeHead(200, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        })
        .end(JSON.stringify(speed.snapshot()) + "\n");
      return;
    }
    if (request.url !== "/metrics") {
      response.writeHead(404).end();
      return;
    }
    try {
      const state = sampler.state;
      let output = metrics.render(
        currentCollection(state.collection, options.networkIntervalSeconds),
      );
      output += renderProbeMetrics(
        validation ? [probes, validation] : [probes],
      );
      output += gauge(
        "xb3_exporter_start_timestamp_seconds",
        "Process wall-clock start; joins stored samples with startup_seconds in logs.",
        startupSeconds,
      );
      output += gauge(
        "xb3_gateway_collection_sequence",
        "Latest completed collection sequence. Per-page sequence identifies older auxiliary observations.",
        state.completedSequence,
      );
      output += gauge(
        "xb3_gateway_collection_in_progress",
        "A passive gateway sequence is active independently of HTTP scrapes.",
        Number(state.inProgress),
      );
      output +=
        `# HELP xb3_page_observation_sequence Collection sequence of each cached page result, including failed attempts.\n# TYPE xb3_page_observation_sequence gauge\n` +
        Object.entries(state.pageSequences)
          .map(
            ([page, sequence]) =>
              `xb3_page_observation_sequence{page="${page}"} ${sequence}`,
          )
          .join("\n") +
        "\n";
      output += gauge(
        "xb3_gateway_worker_running",
        "Background gateway worker is running.",
        Number(gatewayWorkerRunning),
      );
      output += gauge(
        "xb3_gateway_last_completed_timestamp_seconds",
        "Last collection completion, including failed/partial attempts.",
        state.lastCompletedSeconds,
      );
      output += gauge(
        "xb3_gateway_cooldown_until_timestamp_seconds",
        "Shared portal cooldown after an ambiguous metrics or log transport failure.",
        state.cooldownUntilSeconds,
      );
      output += gauge(
        "xb3_gateway_portal_busy",
        "A metrics or logs sequence owns the shared portal gate.",
        Number(access.state.owner !== null),
      );
      output += gauge(
        "xb3_gateway_portal_logs_waiting",
        "A log poll is waiting for exclusive portal access or shared cooldown.",
        Number(access.state.logsWaiting),
      );
      output += gauge(
        "xb3_gateway_portal_cooldown_remaining_seconds",
        "Remaining shared portal cooldown measured with the monotonic clock.",
        access.state.cooldownRemainingSeconds,
      );
      output += gauge(
        "xb3_gateway_schedule_skipped_slots",
        "Scheduled gateway slots skipped since startup.",
        state.skippedTicks,
      );
      output += gauge(
        "xb3_gateway_network_interval_seconds",
        "Configured network-page schedule; actual retrieval bounds are logged.",
        options.networkIntervalSeconds,
      );
      output += gauge(
        "xb3_probe_interval_seconds",
        "Per-target scheduled probe interval. Actual launch spacing and skips are recorded.",
        1,
      );
      output += gauge(
        "xb3_probe_live_recorder_running",
        "Live probe scheduler is running.",
        Number(probes.isRunning),
      );
      output += speed.render();
      output += `# HELP xb3_log_records_dropped_total Records dropped locally because stdout buffering exceeded its limit.\n# TYPE xb3_log_records_dropped_total counter\nxb3_log_records_dropped_total ${droppedLogRecords}\n`;
      response.writeHead(200, {
        "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
        "Cache-Control": "no-store",
      });
      response.end(output);
    } catch {
      response.writeHead(500).end("Rendering failed\n");
    }
  });
  server.requestTimeout = 5_000;
  server.headersTimeout = 5_000;
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port, options.host, resolve);
  });
  write({
    event: "xb3_exporter_started",
    version: VERSION,
    speedtest_interval_minutes: options.speedtestIntervalMinutes,
    port: options.port,
    log_mode: options.logMode,
    network_interval_seconds: options.networkIntervalSeconds,
    auxiliary_interval_seconds: 60,
    probe_interval_seconds: 1,
    probe_tool: "busybox_ping",
    timestamp_basis:
      "subprocess launch/completion; utility RTT; kernel send/receive times unavailable",
    targets: PROBE_TARGETS,
    interfaces: Object.entries(networkInterfaces()).flatMap(
      ([name, addresses]) =>
        (addresses ?? [])
          .filter((a) => a.family === "IPv4" && !a.internal)
          .map((a) => ({ name, address: a.address })),
    ),
    vantage:
      "app container network namespace; post-NAT source and Core path equivalence not established",
  });
  void sampler.run(stop.signal).catch(() => {
    gatewayWorkerRunning = false;
    write({ event: "xb3_gateway_worker_failed" });
  });
  void probes
    .run(stop.signal)
    .catch(() => write({ event: "xb3_probe_worker_failed" }));
  if (options.logMode !== "off")
    void logs
      .loop([...kinds], options.logIntervalSeconds, 150_000, stop.signal)
      .catch(() => write({ event: "xb3_log_worker_failed" }));
  void speed
    .run(stop.signal)
    .catch(() => write({ event: "xb3_speedtest_worker_failed" }));

  // Supervisor-authenticated stdin only: one finite, isolated synthetic test,
  // and on-request speed tests (one at a time).
  let input = "";
  let discarding = false;
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk: string) => {
    for (const char of chunk) {
      if (char !== "\n") {
        if (!discarding) {
          input += char;
          if (Buffer.byteLength(input) > 1024) {
            input = "";
            discarding = true;
          }
        }
        continue;
      }
      if (discarding) {
        discarding = false;
        write({ event: "xb3_validation_rejected", reason: "input_too_long" });
        continue;
      }
      const line = input;
      input = "";
      try {
        const command = JSON.parse(line);
        if (
          !command ||
          Object.keys(command).sort().join(",") !== "command,id" ||
          !["verify_correlation", "speed_test"].includes(command.command) ||
          typeof command.id !== "string" ||
          !/^[A-Za-z0-9_-]{1,64}$/.test(command.id)
        )
          throw new Error();
        if (command.command === "speed_test") {
          speed.trigger("manual", command.id);
          continue;
        }
        if (validationRequested) {
          write({
            event: "xb3_validation_rejected",
            reason: "already_requested",
          });
          continue;
        }
        validationRequested = true;
        validation = new ProbeRecorder({
          startupId,
          startupSeconds,
          mode: "validation",
          validationId: command.id,
          maxTicks: 20,
          driver: validationDriver(),
          write,
        });
        write({
          event: "xb3_validation_started",
          validation_id: command.id,
          synthetic: true,
          mode: "validation",
          expected_ticks: 20,
          baseline_delay_seconds: 6,
          expected_outcomes: {
            reply: 52,
            no_reply_by_deadline: 6,
            local_error: 2,
            skipped_local: 0,
          },
          external_loss_sequences: [5, 6, 7],
          external_local_error_sequences: [9],
          gateway_before_sequence: sampler.state.sequence,
        });
        void delay(6000, undefined, { signal: stop.signal })
          .then(() => validation!.run(stop.signal))
          .then(() =>
            write({
              event: "xb3_validation_completed",
              validation_id: command.id,
              mode: "validation",
              synthetic: true,
              completed_at: new Date().toISOString(),
              states: [...validation!.states].map(([target, s]) => ({
                target,
                scheduled: s.scheduled,
                launched: s.launched,
                completed: s.completed,
              })),
            }),
          )
          .catch(() =>
            write({
              event: "xb3_validation_failed",
              validation_id: command.id,
              mode: "validation",
              synthetic: true,
            }),
          );
      } catch {
        write({ event: "xb3_validation_rejected", reason: "invalid_command" });
      }
    }
  });
  const correlationTimer = setInterval(() => correlator?.expire(), 5000);
  correlationTimer.unref();
  const shutdown = () => {
    stop.abort();
    correlator?.close();
    clearInterval(correlationTimer);
    server.close();
    setTimeout(() => {
      workerParsers.close();
      correlator?.close();
      process.exit(0);
    }, 5_000).unref();
  };
  process.once("SIGTERM", shutdown);
  process.once("SIGINT", shutdown);
}
main().catch((error) => {
  const reason =
    error instanceof Error &&
    /^(Invalid option:|Set gateway_password|Cannot read app options|Invalid app options)/.test(
      error.message,
    )
      ? error.message
      : "Exporter startup failed";
  console.error(JSON.stringify({ event: "xb3_exporter_failed", reason }));
  process.exitCode = 1;
});
