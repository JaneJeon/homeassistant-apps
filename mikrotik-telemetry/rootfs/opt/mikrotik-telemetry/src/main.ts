import { createServer } from "node:http";
import { loadOptions } from "./config.js";
import { emptyState, renderMetrics, type State } from "./metrics.js";
import { pingOnce } from "./ping.js";
import { collectSnmp } from "./snmp.js";

async function main(): Promise<void> {
  const options = await loadOptions();
  const state: State = emptyState();
  let pingsInFlight = 0;
  const probe = async () => {
    if (pingsInFlight >= 2) {
      state.ping.local_error++;
      return;
    }
    pingsInFlight++;
    try {
      state.ping[await pingOnce(options.routerHost)]++;
    } finally {
      pingsInFlight--;
    }
  };
  const poll = async () => {
    if (state.snmpPollInProgress) return;
    state.snmpPollInProgress = true;
    const started = performance.now();
    try {
      state.sample = await collectSnmp(options);
      state.snmpUp = true;
      state.snmpLastSuccessSeconds = Date.now() / 1000;
    } catch {
      state.sample = null; // A failed poll must never expose stale measurements.
      state.snmpUp = false;
    } finally {
      state.snmpLastDurationSeconds = (performance.now() - started) / 1000;
      state.snmpPollInProgress = false;
    }
  };
  let publicationFailed = false;
  let collecting = false;
  const collectAndPublish = async () => {
    if (collecting) return;
    collecting = true;
    try {
      await poll();
      if (!options.victoriametricsUrl) return;
      const response = await fetch(options.victoriametricsUrl, {
        method: "POST",
        headers: { "Content-Type": "text/plain" },
        body: renderMetrics(state),
        redirect: "error",
        signal: AbortSignal.timeout(3_000),
      });
      await response.body?.cancel();
      if (!response.ok) throw new Error();
      if (publicationFailed) console.info("VictoriaMetrics publication resumed");
      publicationFailed = false;
    } catch {
      if (!publicationFailed) console.error("VictoriaMetrics rejected a monitoring sample or was unreachable");
      publicationFailed = true;
    } finally {
      collecting = false;
    }
  };
  if (process.argv.includes("--once")) {
    await poll();
    if (!state.snmpUp) process.exitCode = 1;
    process.stdout.write(renderMetrics(state));
    return;
  }
  const server = createServer((request, response) => {
    if (request.method !== "GET") { response.writeHead(405, { Allow: "GET" }).end(); return; }
    if (request.url === "/healthz") { response.writeHead(200, { "Content-Type": "text/plain" }).end("ok\n"); return; }
    if (request.url !== "/metrics") { response.writeHead(404).end(); return; }
    response.writeHead(200, { "Content-Type": "text/plain; version=0.0.4; charset=utf-8", "Cache-Control": "no-store" }).end(renderMetrics(state));
  });
  server.listen(19100, "0.0.0.0");
  void probe();
  void collectAndPublish();
  const probeTimer = setInterval(probe, 1_000);
  const pollTimer = setInterval(collectAndPublish, options.pollIntervalSeconds * 1_000);
  const stop = () => {
    clearInterval(probeTimer);
    clearInterval(pollTimer);
    server.close();
  };
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
}

void main().catch((error) => { console.error(error instanceof Error ? error.message : "startup failed"); process.exitCode = 1; });
