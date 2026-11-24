import { execFile } from "node:child_process";
import type { PingOutcome } from "./metrics.js";

export function classifyPing(exitCode: number | null, output: string): PingOutcome {
  if (exitCode === 0 && /1 packets transmitted, 1 packets received/.test(output)) return "reply";
  if (exitCode === 1 && /1 packets transmitted, 0 packets received/.test(output)) return "no_reply_by_deadline";
  return "local_error";
}

export function readReplySeconds(output: string): number | undefined {
  const match = /\btime=(\d+(?:\.\d+)?) ms\b/.exec(output);
  return match ? Number(match[1]) / 1_000 : undefined;
}

export function pingOnce(host: string): Promise<{ outcome: PingOutcome; seconds?: number }> {
  return new Promise((resolve) => {
    execFile(
      "/bin/ping", ["-4", "-n", "-c", "1", "-W", "1", host],
      { timeout: 1500, killSignal: "SIGKILL", maxBuffer: 8192, env: { ...process.env, LC_ALL: "C" } },
      (error, stdout) => {
        const exitCode = error ? (typeof error.code === "number" ? error.code : null) : 0;
        const outcome = classifyPing(exitCode, stdout);
        resolve({ outcome, seconds: outcome === "reply" ? readReplySeconds(stdout) : undefined });
      },
    );
  });
}
