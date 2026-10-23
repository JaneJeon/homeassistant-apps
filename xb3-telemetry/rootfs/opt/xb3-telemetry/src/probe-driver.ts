import { execFile as nodeExecFile } from "node:child_process";
import { isIP } from "node:net";

export type ProbeDriverResult = {
  outcome: "reply" | "no_reply_by_deadline" | "local_error";
  rttMs: number | null;
  reason?: string;
};

export type ProbeDriver = (
  address: string,
  stop?: AbortSignal,
) => Promise<ProbeDriverResult>;

type ExecError = NodeJS.ErrnoException & { killed?: boolean; signal?: string };
type ExecFile = (
  file: string,
  args: string[],
  options: {
    env: NodeJS.ProcessEnv;
    timeout: number;
    killSignal: NodeJS.Signals;
    maxBuffer: number;
    signal?: AbortSignal;
  },
  callback: (error: ExecError | null, stdout: string, stderr: string) => void,
) => unknown;

const localError = (reason: string): ProbeDriverResult => ({
  outcome: "local_error",
  rttMs: null,
  reason: reason.slice(0, 160),
});

/** Classify only the stable packet counters and reply time emitted by ping. */
export function classifyPing(
  output: string,
  exitCode: number | null,
): ProbeDriverResult {
  const summary = output.match(
    /\b(\d+)\s+packets transmitted,\s*(\d+)\s+packets received,\s*(\d+(?:\.\d+)?)% packet loss\b/i,
  );
  if (!summary) return localError("unrecognized ping output");

  const transmitted = Number(summary[1]);
  const received = Number(summary[2]);
  const loss = Number(summary[3]);
  if (transmitted !== 1 || !Number.isFinite(loss))
    return localError("unexpected ping packet counters");

  if (exitCode === 0 && received === 1 && loss === 0) {
    const time = output.match(/\btime=([0-9]+(?:\.[0-9]+)?)\s*ms\b/i);
    const rttMs = time ? Number(time[1]) : NaN;
    if (Number.isFinite(rttMs) && rttMs >= 0)
      return { outcome: "reply", rttMs };
    return localError("reply summary missing valid RTT");
  }
  if (exitCode === 1 && received === 0 && loss === 100)
    return { outcome: "no_reply_by_deadline", rttMs: null };
  return localError("ping result did not match a recognized outcome");
}

function errorReason(error: ExecError): string {
  if (error.code === "ENOENT") return "ping executable unavailable";
  if (error.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER")
    return "ping output exceeded limit";
  if (error.killed || error.signal) return "ping process stopped before result";
  return `ping process error${error.code ? ` (${error.code})` : ""}`;
}

export function createPingProbe(
  execFile: ExecFile = nodeExecFile as unknown as ExecFile,
): ProbeDriver {
  return async (address, stop) => {
    if (isIP(address) !== 4) return localError("invalid IPv4 address");
    if (stop?.aborted) return localError("probe aborted");

    return new Promise((resolve) => {
      execFile(
        "/bin/ping",
        ["-4", "-n", "-c", "1", "-W", "1", address],
        {
          env: { ...process.env, LC_ALL: "C" },
          timeout: 1500,
          killSignal: "SIGKILL",
          maxBuffer: 8192,
          signal: stop,
        },
        (error, stdout, stderr) => {
          if (error && (error.killed || error.signal)) {
            resolve(
              localError(stop?.aborted ? "probe aborted" : errorReason(error)),
            );
            return;
          }
          if (error && typeof error.code !== "number") {
            resolve(
              localError(stop?.aborted ? "probe aborted" : errorReason(error)),
            );
            return;
          }
          if (error && stderr.trim()) {
            resolve(localError("ping reported a process error"));
            return;
          }
          resolve(
            classifyPing(
              stdout.slice(0, 8192),
              error ? (error.code as unknown as number) : 0,
            ),
          );
        },
      );
    });
  };
}

export const pingProbe = createPingProbe();
