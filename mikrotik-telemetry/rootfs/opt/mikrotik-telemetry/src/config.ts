import { readFile } from "node:fs/promises";
import { isIPv4 } from "node:net";

export interface WifiClient {
  name: string;
  macIndex: string;
}

export interface Options {
  routerHost: string;
  wanInterface: string;
  snmpUsername: string;
  snmpAuthKey: string;
  snmpPrivacyKey: string;
  pollIntervalSeconds: number;
  wifiClients: WifiClient[];
  victoriametricsUrl?: URL;
  victorialogsUrl?: URL;
}

export function parseWifiClients(value: unknown): WifiClient[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 16) throw new Error("Invalid option: wifi_clients");
  const names = new Set<string>();
  const addresses = new Set<string>();
  return value.map((client: unknown) => {
    if (!client || typeof client !== "object") throw new Error("Invalid option: wifi_clients");
    const { name, mac } = client as Record<string, unknown>;
    if (typeof name !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$/.test(name)
        || typeof mac !== "string" || !/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(mac))
      throw new Error("Invalid option: wifi_clients");
    const macIndex = mac.split(":").map((octet) => Number.parseInt(octet, 16)).join(".");
    if (names.has(name) || addresses.has(macIndex)) throw new Error("Duplicate wifi_clients entry");
    names.add(name);
    addresses.add(macIndex);
    return { name, macIndex };
  });
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
  const logsEndpoint = values.victorialogs_url;
  let victorialogsUrl: URL | undefined;
  if (logsEndpoint !== undefined && logsEndpoint !== "") {
    try {
      if (typeof logsEndpoint !== "string") throw new Error();
      victorialogsUrl = new URL(logsEndpoint);
      if (!["http:", "https:"].includes(victorialogsUrl.protocol)
          || victorialogsUrl.username || victorialogsUrl.password)
        throw new Error();
      victorialogsUrl.pathname = "/insert/jsonline";
      victorialogsUrl.search = "";
      victorialogsUrl.searchParams.set("_msg_field", "message");
      victorialogsUrl.searchParams.set("_time_field", "timestamp");
      victorialogsUrl.searchParams.set("_stream_fields", "source,router");
    } catch {
      throw new Error("Invalid option: victorialogs_url");
    }
  }
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
    wifiClients: parseWifiClients(values.wifi_clients),
    victoriametricsUrl,
    victorialogsUrl,
  };
}
