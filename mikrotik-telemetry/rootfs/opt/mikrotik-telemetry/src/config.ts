import { readFile } from "node:fs/promises";
import { isIPv4 } from "node:net";

export interface Options {
  routerHost: string;
  wanInterface: string;
  snmpUsername: string;
  snmpAuthKey: string;
  snmpPrivacyKey: string;
  pollIntervalSeconds: number;
  victoriametricsUrl?: URL;
}

export async function loadOptions(): Promise<Options> {
  const path = process.env.MIKROTIK_OPTIONS_FILE ?? "/data/options.json";
  let data: unknown;
  try {
    data = JSON.parse(await readFile(path, "utf8"));
  } catch {
    throw new Error("Cannot read MikroTik app options");
  }
  if (!data || typeof data !== "object" || Array.isArray(data))
    throw new Error("Invalid MikroTik app options");
  const values = data as Record<string, unknown>;
  const required = (key: string, fallback = ""): string => {
    const value = values[key];
    if (value === undefined && fallback) return fallback;
    if (typeof value !== "string" || !value.trim())
      throw new Error(`Invalid option: ${key}`);
    return value.trim();
  };
  const interval = values.poll_interval_seconds ?? 15;
  if (!Number.isInteger(Number(interval)) || Number(interval) < 5 || Number(interval) > 300)
    throw new Error("Invalid option: poll_interval_seconds");
  const routerHost = required("router_host");
  if (!isIPv4(routerHost)) throw new Error("Invalid option: router_host");
  const metricsEndpoint = values.victoriametrics_url;
  let victoriametricsUrl: URL | undefined;
  if (metricsEndpoint !== undefined && metricsEndpoint !== "") {
    try {
      if (typeof metricsEndpoint !== "string") throw new Error();
      victoriametricsUrl = new URL(metricsEndpoint);
      if (!["http:", "https:"].includes(victoriametricsUrl.protocol)
          || victoriametricsUrl.username || victoriametricsUrl.password)
        throw new Error();
      victoriametricsUrl.pathname = "/api/v1/import/prometheus";
      victoriametricsUrl.search = "";
      victoriametricsUrl.searchParams.set("extra_label", "job=mikrotik-telemetry");
    } catch {
      throw new Error("Invalid option: victoriametrics_url");
    }
  }
  return {
    routerHost,
    wanInterface: typeof values.wan_interface === "string" && values.wan_interface.trim()
      ? values.wan_interface.trim() : "ether1",
    snmpUsername: required("snmp_username", "ha-observability"),
    snmpAuthKey: required("snmp_auth_key"),
    snmpPrivacyKey: required("snmp_privacy_key"),
    pollIntervalSeconds: Number(interval),
    victoriametricsUrl,
  };
}
