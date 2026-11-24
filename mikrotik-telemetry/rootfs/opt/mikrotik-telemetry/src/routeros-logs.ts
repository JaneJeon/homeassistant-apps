import { createSocket, type RemoteInfo, type Socket } from "node:dgram";
import type { State } from "./metrics.js";

const MAX_MESSAGE_BYTES = 8_192;
const MAX_QUEUE_RECORDS = 512;
const BATCH_RECORDS = 64;
const FLUSH_DELAY_MS = 250;

export interface RouterosLog {
  message: string;
  timestamp: string;
  source: "routeros";
  router: string;
  router_ip: string;
  routeros_timestamp: string;
  routeros_hostname: string;
  syslog_priority: number;
  syslog_facility: number;
  syslog_severity: number;
}

function safeText(input: string): string {
  let result = "";
  let bytes = 0;
  for (const character of input) {
    if (/^[\u0000-\u001F\u007F-\u009F]$/.test(character)) continue;
    const size = Buffer.byteLength(character);
    if (bytes + size > MAX_MESSAGE_BYTES) break;
    result += character;
    bytes += size;
  }
  return result.trim();
}

/** Parse the BSD header RouterOS emits; reported wall time remains a searchable field, not an assumed timezone. */
export function parseRouterosSyslog(input: Buffer, sourceAddress: string, receivedAt = new Date()): RouterosLog | null {
  const line = input.toString("utf8").replace(/[\r\n]+$/g, "");
  const match = /^<(\d{1,3})>([A-Z][a-z]{2}\s{1,2}\d{1,2}\s\d{2}:\d{2}:\d{2})\s+(\S+)\s+([\s\S]*)$/.exec(line);
  if (!match) return null;
  const [, priorityText = "", reportedTimestamp = "", hostnameText = "", messageText = ""] = match;
  const priority = Number(priorityText);
  const hostname = safeText(hostnameText);
  const message = safeText(messageText);
  if (!Number.isInteger(priority) || priority < 0 || priority > 191 || !reportedTimestamp || !hostname || !message) return null;
  return {
    message, timestamp: receivedAt.toISOString(), source: "routeros", router: hostname, router_ip: sourceAddress,
    routeros_timestamp: reportedTimestamp, routeros_hostname: hostname, syslog_priority: priority,
    syslog_facility: Math.floor(priority / 8), syslog_severity: priority % 8,
  };
}

export class RouterosLogIngest {
  private readonly queue: RouterosLog[] = [];
  private socket: Socket | undefined;
  private flushing = false;
  private flushTimer: NodeJS.Timeout | undefined;
  private failed = false;

  constructor(private readonly routerHost: string, private readonly endpoint: URL, private readonly state: State) {}

  start(): void {
    this.socket = createSocket("udp4");
    this.socket.on("message", (message, remote) => this.receive(message, remote));
    this.socket.on("error", () => console.error("RouterOS syslog listener encountered an error"));
    this.socket.bind(5514, "0.0.0.0", () => console.info("RouterOS syslog listener enabled"));
  }

  stop(): void {
    if (this.flushTimer) clearTimeout(this.flushTimer);
    this.socket?.close();
  }

  receive(message: Buffer, remote: Pick<RemoteInfo, "address">): void {
    if (remote.address !== this.routerHost) { this.state.routerosLogs.dropped.unauthorized_source++; return; }
    const record = parseRouterosSyslog(message, remote.address);
    if (!record) { this.state.routerosLogs.dropped.invalid_syslog++; return; }
    if (this.queue.length >= MAX_QUEUE_RECORDS) { this.state.routerosLogs.dropped.queue_full++; return; }
    this.queue.push(record);
    this.state.routerosLogs.accepted++;
    this.state.routerosLogs.queued = this.queue.length;
    this.scheduleFlush();
  }

  private scheduleFlush(): void {
    if (!this.flushTimer && !this.flushing)
      this.flushTimer = setTimeout(() => { this.flushTimer = undefined; void this.flush(); }, FLUSH_DELAY_MS);
  }

  private async flush(): Promise<void> {
    if (this.flushing || this.queue.length === 0) return;
    this.flushing = true;
    const batch = this.queue.splice(0, BATCH_RECORDS);
    this.state.routerosLogs.queued = this.queue.length;
    try {
      const response = await fetch(this.endpoint, {
        method: "POST", headers: { "Content-Type": "application/stream+json" },
        body: batch.map((record) => JSON.stringify(record)).join("\n") + "\n",
        redirect: "error", signal: AbortSignal.timeout(3_000),
      });
      await response.body?.cancel();
      if (!response.ok) throw new Error("VictoriaLogs rejected batch");
      this.state.routerosLogs.forwarded += batch.length;
      if (this.failed) console.info("RouterOS log forwarding resumed");
      this.failed = false;
    } catch {
      this.state.routerosLogs.dropped.forward_failed += batch.length;
      if (!this.failed) console.error("RouterOS log batch was rejected or unreachable");
      this.failed = true;
    } finally {
      this.flushing = false;
      if (this.queue.length) this.scheduleFlush();
    }
  }
}
