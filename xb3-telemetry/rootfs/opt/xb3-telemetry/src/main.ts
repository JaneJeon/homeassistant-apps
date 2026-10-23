import { createServer } from "node:http";
import { GatewayClient } from "./client.js";
import { Collector } from "./collector.js";
import { loadOptions } from "./config.js";
import { LogCollector } from "./logs.js";
import { Metrics } from "./metrics.js";

async function main(): Promise<void> {
  const options = await loadOptions();
  const client = () =>
    new GatewayClient(
      options.gatewayUrl,
      options.username,
      options.password,
      12_000,
    );
  const metrics = new Metrics();
  const collector = new Collector(client());
  // Each collector keeps its own PHP/CSRF session. A log request is never part
  // of a Prometheus scrape, and source records go to existing container logs.
  const logs = new LogCollector(client(), metrics);
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
    process.stdout.write(metrics.render(await collector.collect()));
    return;
  }
  const stop = new AbortController();
  let lastClockObservation = 0;
  const server = createServer(async (request, response) => {
    if (request.method !== "GET") {
      response.writeHead(405, { Allow: "GET" }).end();
      return;
    }
    if (request.url === "/healthz") {
      response.writeHead(200, { "Content-Type": "text/plain" }).end("ok\n");
      return;
    }
    if (request.url !== "/metrics") {
      response.writeHead(404).end();
      return;
    }
    try {
      const collection = await collector.collect();
      const clock = collection.network.data?.gatewayTimeRaw;
      if (
        clock &&
        collection.network.observedAtSeconds !== lastClockObservation
      ) {
        lastClockObservation = collection.network.observedAtSeconds;
        // Keep an observed clock pair in the existing log pipeline. Do not
        // assign a timezone or put a changing wall-clock string in labels.
        console.log(
          JSON.stringify({
            event: "xb3_gateway_clock_snapshot",
            source_time_raw: clock,
            observed_at: new Date(lastClockObservation * 1000).toISOString(),
            fetch_duration_seconds: collection.network.durationSeconds,
          }),
        );
      }
      response.writeHead(200, {
        "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
        "Cache-Control": "no-store",
      });
      response.end(metrics.render(collection));
    } catch {
      response.writeHead(500).end("Collection failed\n");
    }
  });
  server.requestTimeout = 55_000;
  server.headersTimeout = 10_000;
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port, options.host, resolve);
  });
  console.log(
    JSON.stringify({
      event: "xb3_exporter_started",
      port: options.port,
      log_mode: options.logMode,
    }),
  );
  if (options.logMode !== "off")
    void logs
      .loop([...kinds], options.logIntervalSeconds, 150_000, stop.signal)
      .catch(() =>
        console.error(JSON.stringify({ event: "xb3_log_worker_failed" })),
      );
  const shutdown = () => {
    stop.abort();
    server.close();
    setTimeout(() => process.exit(0), 5_000).unref();
  };
  process.once("SIGTERM", shutdown);
  process.once("SIGINT", shutdown);
}

main().catch((error) => {
  // Configuration validation never includes values in its messages.
  const message =
    error instanceof Error &&
    /^(Invalid option:|Set gateway_password|Cannot read app options|Invalid app options)/.test(
      error.message,
    )
      ? error.message
      : "Exporter startup failed";
  console.error(
    JSON.stringify({ event: "xb3_exporter_failed", reason: message }),
  );
  process.exitCode = 1;
});
