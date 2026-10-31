/**
 * One speed test in its own process, so transfers and garbage collection cannot
 * delay the parent's one-second probe scheduler. Prints one JSON line.
 */
import { HttpError, measure, METHOD } from "./speed-measure.js";
import { HttpsSpeedTransport } from "./speed-transport.js";

const transport = new HttpsSpeedTransport(METHOD.connections);
try {
  const result = await measure(transport, METHOD);
  process.stdout.write(
    JSON.stringify({ ok: true, colo: transport.colo, result }) + "\n",
  );
} catch (error) {
  const reason =
    error instanceof HttpError
      ? error.message
      : error instanceof Error && error.message === "request_timeout"
        ? error.message
        : error instanceof Error &&
            "code" in error &&
            typeof error.code === "string"
          ? error.code
          : "measurement_failed";
  process.stdout.write(
    JSON.stringify({
      ok: false,
      colo: transport.colo,
      reason,
      retryAfterSeconds:
        error instanceof HttpError ? error.retryAfterSeconds : null,
    }) + "\n",
  );
  process.exitCode = 1;
} finally {
  transport.close();
}
