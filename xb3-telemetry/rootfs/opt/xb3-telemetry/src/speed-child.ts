/**
 * One speed test in its own process, so transfers and garbage collection cannot
 * delay the parent's one-second probe scheduler. Prints one JSON line.
 */
import { measure } from "./speed-measure.js";
import { HttpsSpeedTransport } from "./speed-transport.js";

const transport = new HttpsSpeedTransport();
try {
  const result = await measure(transport);
  process.stdout.write(
    JSON.stringify({ ok: true, colo: transport.colo, result }) + "\n",
  );
} catch (error) {
  const reason =
    error instanceof Error &&
    /^(http_\d{3}|request_timeout)$/.test(error.message)
      ? error.message
      : error instanceof Error &&
          "code" in error &&
          typeof error.code === "string"
        ? error.code
        : "measurement_failed";
  process.stdout.write(
    JSON.stringify({ ok: false, colo: transport.colo, reason }) + "\n",
  );
  process.exitCode = 1;
} finally {
  transport.close();
}
