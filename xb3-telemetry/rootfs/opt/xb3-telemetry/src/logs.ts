import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { GatewayClient, GatewayError, type LogKind } from "./client.js";
import { parseLogs } from "./parser.js";
import type { Metrics } from "./metrics.js";

export class LogCollector {
  private running = false;
  constructor(
    private readonly client: Pick<GatewayClient, "logs">,
    private readonly metrics: Metrics,
    private readonly write: (record: object) => void = (record) =>
      console.log(JSON.stringify(record)),
  ) {}

  async once(kinds: LogKind[], timeoutMs: number): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      // The upstream handler also uses shared temporary files. Never overlap
      // log requests, even though this client has a separate cookie jar.
      for (const kind of kinds) {
        // Explicit overlap avoids dropping events between the last poll before
        // gateway midnight and the first poll after it. Do not infer its TZ.
        for (const timeFrame of ["Today", "Yesterday"] as const) {
          const key = `${kind}:${timeFrame}`;
          const state = this.metrics.logs.get(key) ?? {
            kind,
            timeFrame,
            inProgress: false,
            success: null,
            durationSeconds: 0,
            lastSuccessSeconds: 0,
            recordsReturned: null,
          };
          this.metrics.logs.set(key, state);
          state.inProgress = true;
          const start = performance.now();
          const queryId = randomUUID();
          try {
            const rows = parseLogs(
              await this.client.logs(kind, timeFrame, timeoutMs),
              kind,
            );
            const observedAt = new Date().toISOString();
            state.success = true;
            state.recordsReturned = rows.length;
            state.lastSuccessSeconds = Date.now() / 1000;
            rows.forEach((row, index) =>
              this.write({
                event: "xb3_gateway_log_snapshot_record",
                query_id: queryId,
                observed_at: observedAt,
                kind,
                time_frame: timeFrame,
                record_index: index,
                source_time_raw: row.gatewayTimeRaw,
                record: row.raw,
              }),
            );
            this.write({
              event: "xb3_gateway_log_query",
              query_id: queryId,
              observed_at: observedAt,
              kind,
              time_frame: timeFrame,
              success: true,
              records_returned: rows.length,
            });
          } catch (error) {
            state.success = false;
            state.recordsReturned = null;
            this.write({
              event: "xb3_gateway_log_query",
              query_id: queryId,
              observed_at: new Date().toISOString(),
              kind,
              time_frame: timeFrame,
              success: false,
              reason:
                error instanceof GatewayError ? error.code : "parse_failed",
            });
            // A timed-out PHP operation may still run on the gateway. Avoid
            // stacking any category behind it in the same logging session.
            if (
              error instanceof GatewayError &&
              ["timeout", "transport_failed", "auth_backoff"].includes(
                error.code,
              )
            )
              return;
            break;
          } finally {
            state.inProgress = false;
            state.durationSeconds = (performance.now() - start) / 1000;
          }
        }
      }
    } finally {
      this.running = false;
    }
  }

  async loop(
    kinds: LogKind[],
    intervalSeconds: number,
    timeoutMs: number,
    stop: AbortSignal,
  ): Promise<void> {
    try {
      // Let initial metric collection establish its session before logging.
      await delay(60_000, undefined, { signal: stop });
      while (!stop.aborted) {
        await this.once(kinds, timeoutMs);
        await delay(intervalSeconds * 1000, undefined, { signal: stop });
      }
    } catch (error) {
      if (!stop.aborted) throw error;
    }
  }
}
