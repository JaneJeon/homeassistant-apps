import { parentPort } from "node:worker_threads";
import { parseHardware, parseLan, parseNetwork } from "./parser.js";

type ParseKind = "network" | "hardware" | "lan";
interface ParseRequest {
  id: number;
  kind: ParseKind;
  html: string;
}

function isRequest(value: unknown): value is ParseRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Partial<ParseRequest>;
  return (
    Number.isSafeInteger(request.id) &&
    (request.kind === "network" ||
      request.kind === "hardware" ||
      request.kind === "lan") &&
    typeof request.html === "string"
  );
}

const port = parentPort;
if (!port) throw new Error("parser worker requires a parent port");

port.on("message", (message: unknown) => {
  if (!isRequest(message)) {
    const id =
      message &&
      typeof message === "object" &&
      Number.isSafeInteger((message as { id?: unknown }).id)
        ? (message as { id: number }).id
        : undefined;
    if (id !== undefined) port.postMessage({ id, error: "parse_failed" });
    return;
  }

  try {
    const data =
      message.kind === "network"
        ? parseNetwork(message.html)
        : message.kind === "hardware"
          ? parseHardware(message.html)
          : parseLan(message.html);
    port.postMessage({ id: message.id, data });
  } catch {
    // Never return page HTML, parser exception text, or source values.
    port.postMessage({ id: message.id, error: "parse_failed" });
  }
});
