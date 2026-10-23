import { readFile } from "node:fs/promises";

export interface Options {
  gatewayUrl: string;
  username: string;
  password: string;
  host: string;
  port: number;
  logMode: "off" | "firewall" | "all";
  logIntervalSeconds: number;
}

export async function loadOptions(): Promise<Options> {
  let data: Record<string, unknown> = {};
  try {
    data = JSON.parse(
      await readFile(
        process.env.XB3_OPTIONS_FILE ?? "/data/options.json",
        "utf8",
      ),
    );
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT")
      throw new Error("Cannot read app options");
  }
  if (!data || typeof data !== "object" || Array.isArray(data))
    throw new Error("Invalid app options");
  const string = (key: string, value: unknown, fallback: string): string => {
    if (value === undefined) return fallback;
    if (typeof value !== "string") throw new Error(`Invalid option: ${key}`);
    return value;
  };
  const integer = (
    key: string,
    value: unknown,
    fallback: number,
    min: number,
    max: number,
  ): number => {
    const parsed = value === undefined ? fallback : Number(value);
    if (!Number.isInteger(parsed) || parsed < min || parsed > max)
      throw new Error(`Invalid option: ${key}`);
    return parsed;
  };
  const logMode = string(
    "log_mode",
    process.env.XB3_LOG_MODE ?? data.log_mode,
    "firewall",
  );
  if (!["off", "firewall", "all"].includes(logMode))
    throw new Error("Invalid option: log_mode");
  const password = string(
    "gateway_password",
    process.env.XB3_PASSWORD ?? data.gateway_password,
    "",
  );
  if (!password)
    throw new Error(
      "Set gateway_password in app Options or XB3_PASSWORD locally",
    );
  return {
    gatewayUrl: string(
      "gateway_url",
      process.env.XB3_GATEWAY_URL ?? data.gateway_url,
      "http://10.0.0.1",
    ),
    username: string(
      "gateway_username",
      process.env.XB3_USERNAME ?? data.gateway_username,
      "admin",
    ),
    password,
    host: process.env.XB3_LISTEN_HOST ?? "0.0.0.0",
    port: integer("port", process.env.XB3_PORT, 19090, 1024, 65535),
    logMode: logMode as Options["logMode"],
    logIntervalSeconds: integer(
      "log_interval_seconds",
      process.env.XB3_LOG_INTERVAL_SECONDS ?? data.log_interval_seconds,
      900,
      60,
      86400,
    ),
  };
}
