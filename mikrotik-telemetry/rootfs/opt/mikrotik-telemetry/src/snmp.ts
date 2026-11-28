import * as snmp from "net-snmp";
import type { Options } from "./config.js";
import type { SnmpSample } from "./metrics.js";
import type { WifiAssociation, WifiInterface } from "./wifi.js";

const IF_DESCR = "1.3.6.1.2.1.2.2.1.2";
const HR_STORAGE_ALLOCATION_UNITS = "1.3.6.1.2.1.25.2.3.1.4.65536";
const OIDS = {
  dhcpLeases: "1.3.6.1.4.1.14988.1.1.6.1.0",
  uptime: "1.3.6.1.2.1.1.3.0",
};

export function decodeCounter64(value: snmp.VarbindValue): string | undefined {
  if (typeof value === "bigint") return value.toString();
  if (Buffer.isBuffer(value)) {
    const bytes = value.length === 9 && value[0] === 0 ? value.subarray(1) : value;
    if (bytes.length < 1 || bytes.length > 8) return undefined;
    return BigInt("0x" + bytes.toString("hex")).toString();
  }
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
  if (typeof value === "string" && /^\d+$/.test(value)) return value;
  return undefined;
}

function numeric(value: snmp.VarbindValue): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "bigint" && value <= BigInt(Number.MAX_SAFE_INTEGER)) return Number(value);
  return undefined;
}

function usable(varbind: snmp.Varbind | undefined): snmp.VarbindValue | undefined {
  return varbind && !snmp.isVarbindError(varbind) ? varbind.value : undefined;
}

function get(session: snmp.Session, oids: string[]): Promise<(snmp.Varbind | undefined)[]> {
  return new Promise((resolve, reject) => session.get(oids, (error, varbinds) => {
    if (error || !varbinds) {
      reject(error ?? new Error("SNMP response is missing values"));
      return;
    }
    const returned = new Map(varbinds.map((varbind) => [varbind.oid, varbind]));
    resolve(oids.map((oid) => returned.get(oid)));
  }));
}

function interfaceIndex(session: snmp.Session, wanInterface: string): Promise<string> {
  return new Promise((resolve, reject) => {
    let index: string | undefined;
    session.subtree(IF_DESCR, 20, (varbinds) => {
      for (const varbind of varbinds) {
        const description = usable(varbind);
        if (String(description) === wanInterface) index = varbind.oid.slice(IF_DESCR.length + 1);
      }
    }, (error) => {
      if (error) reject(error);
      else if (!index) reject(new Error("WAN interface was not found"));
      else resolve(index);
    });
  });
}

function sessionFor(options: Options, retryOptions = { timeout: 5_000, retries: 1 }): snmp.Session {
  return snmp.createV3Session(options.routerHost, {
    name: options.snmpUsername,
    level: snmp.SecurityLevel.authPriv,
    authProtocol: snmp.AuthProtocols.sha,
    authKey: options.snmpAuthKey,
    privProtocol: snmp.PrivProtocols.aes,
    privKey: options.snmpPrivacyKey,
  }, { transport: "udp4", ...retryOptions });
}

/** Use the live interface index so a renumbered WAN port cannot select the wrong counters. */
export async function collectSnmp(options: Options): Promise<SnmpSample> {
  const session = sessionFor(options);
  try {
    const index = await interfaceIndex(session, options.wanInterface);
    const [received, transmitted, allocationUnits, memoryUsed, memoryTotal, leases, uptime, ...cores] = (await get(session, [
      `1.3.6.1.2.1.31.1.1.1.6.${index}`,
      `1.3.6.1.2.1.31.1.1.1.10.${index}`,
      HR_STORAGE_ALLOCATION_UNITS,
      "1.3.6.1.2.1.25.2.3.1.6.65536",
      "1.3.6.1.2.1.25.2.3.1.5.65536",
      OIDS.dhcpLeases, OIDS.uptime,
      ...[1, 2, 3, 4].map((core) => `1.3.6.1.2.1.25.3.3.1.2.${core}`),
    ])).map(usable);
    const wanReceiveOctets = decodeCounter64(received);
    const wanTransmitOctets = decodeCounter64(transmitted);
    if (wanReceiveOctets === undefined || wanTransmitOctets === undefined)
      throw new Error("WAN byte counters unavailable");
    const unit = numeric(allocationUnits);
    const scale = (value: snmp.VarbindValue | undefined): number | undefined => {
      const number = numeric(value);
      return unit !== undefined && number !== undefined ? unit * number : undefined;
    };
    const cpuLoadPercent = new Map<number, number>();
    cores.forEach((value, offset) => { const load = numeric(value); if (load !== undefined) cpuLoadPercent.set(offset + 1, load); });
    const ticks = numeric(uptime);
    return {
      wanReceiveOctets, wanTransmitOctets,
      cpuLoadPercent, memoryUsedBytes: scale(memoryUsed), memoryTotalBytes: scale(memoryTotal),
      dhcpLeases: numeric(leases), uptimeSeconds: ticks === undefined ? undefined : ticks / 100,
    };
  } finally { session.close(); }
}

const WIFI_TABLE = "1.3.6.1.4.1.14988.1.1.21.4.1";

function wifiInterfaceIndices(session: snmp.Session, prefix: string): Promise<Set<string>> {
  return new Promise((resolve, reject) => {
    const indices = new Set<string>();
    session.subtree(prefix, 4, (varbinds) => {
      for (const varbind of varbinds) {
        if (varbind.type !== snmp.ObjectType.TimeTicks || !varbind.oid.startsWith(prefix + ".")) continue;
        const ifIndex = varbind.oid.slice(prefix.length + 1);
        if (/^[1-9][0-9]*$/.test(ifIndex) && Number(ifIndex) <= 2147483647) indices.add(ifIndex);
      }
    }, (error) => {
      if (error) reject(error);
      else resolve(indices);
    });
  });
}

/** Router-supplied DisplayString text without control characters, or undefined for other types. */
export function displayString(varbind: Pick<snmp.Varbind, "type" | "value"> | undefined): string | undefined {
  if (varbind?.type !== snmp.ObjectType.OctetString) return undefined;
  const text = Buffer.isBuffer(varbind.value) ? varbind.value.toString("utf8") : typeof varbind.value === "string" ? varbind.value : undefined;
  return text?.replace(/[\u0000-\u001f\u007f]/g, "");
}

/** Type and value as received, for diagnostic logs. */
export function rawVarbind(varbind: Pick<snmp.Varbind, "type" | "value"> | undefined): string {
  if (!varbind) return "missing";
  const type = varbind.type === undefined ? "Unknown" : snmp.ObjectType[varbind.type] ?? String(varbind.type);
  if (varbind.type === snmp.ObjectType.OctetString) return `${type}:${JSON.stringify(displayString(varbind))}`;
  if (varbind.type === snmp.ObjectType.Counter64) return `${type}:${decodeCounter64(varbind.value) ?? "?"}`;
  if (typeof varbind.value === "number" || typeof varbind.value === "bigint") return `${type}:${varbind.value}`;
  return type;
}

const WIFI_COLUMNS = { uptime: 4, signal: 6, band: 8, txRate: 9, rxRate: 10, txBytes: 13, rxBytes: 14 } as const;

/** Discover only configured MACs and read each association's counters and link state in a single request. */
export async function collectWifiSnmp(options: Options): Promise<WifiAssociation[]> {
  const session = sessionFor(options, { timeout: 2_000, retries: 0 });
  const result: WifiAssociation[] = [];
  try {
    for (const { name, macIndex } of options.wifiClients) {
      const indices = await wifiInterfaceIndices(session, `${WIFI_TABLE}.${WIFI_COLUMNS.uptime}.${macIndex}`);
      for (const ifIndex of indices) {
        const suffix = `${macIndex}.${ifIndex}`;
        const [uptime, signal, band, txRate, rxRate, tx, rx] = await get(session, [
          WIFI_COLUMNS.uptime, WIFI_COLUMNS.signal, WIFI_COLUMNS.band, WIFI_COLUMNS.txRate, WIFI_COLUMNS.rxRate,
          WIFI_COLUMNS.txBytes, WIFI_COLUMNS.rxBytes,
        ].map((column) => `${WIFI_TABLE}.${column}.${suffix}`));
        if (uptime?.type !== snmp.ObjectType.TimeTicks
            || tx?.type !== snmp.ObjectType.Counter64 || rx?.type !== snmp.ObjectType.Counter64) continue;
        const uptimeTicks = numeric(uptime.value);
        const transmit = decodeCounter64(tx.value);
        const receive = decodeCounter64(rx.value);
        if (uptimeTicks === undefined || receive === undefined || transmit === undefined) continue;
        const gauge = (varbind: snmp.Varbind | undefined) =>
          varbind?.type === snmp.ObjectType.Gauge ? numeric(varbind.value) : undefined;
        const bandText = displayString(band);
        result.push({
          client: name, ifIndex, receive: BigInt(receive), transmit: BigInt(transmit), uptimeTicks,
          signal: signal?.type === snmp.ObjectType.Integer ? numeric(signal.value) : undefined,
          band: bandText ? bandText : undefined,
          txRate: gauge(txRate), rxRate: gauge(rxRate),
          raw: [[4, uptime], [6, signal], [8, band], [9, txRate], [10, rxRate]]
            .map(([column, varbind]) => `col${column}=${rawVarbind(varbind as snmp.Varbind | undefined)}`).join(" "),
        });
      }
    }
    return result;
  } finally { session.close(); }
}

const WIFI_INTERFACES = "1.3.6.1.4.1.14988.1.1.21.5.1";

/** Group a walk of mtxrWifiInterfaces (columns 2 Name, 3 Ssid, 4 Freq, 5 CurrentChannel) into rows by table index. */
export function wifiInterfacesFromWalk(varbinds: Pick<snmp.Varbind, "oid" | "type" | "value">[]): WifiInterface[] {
  const rows = new Map<string, Map<number, Pick<snmp.Varbind, "type" | "value">>>();
  for (const varbind of varbinds) {
    const match = varbind.oid.startsWith(WIFI_INTERFACES + ".") ? /^([2-5])\.([0-9]+)$/.exec(varbind.oid.slice(WIFI_INTERFACES.length + 1)) : null;
    if (!match) continue;
    const [, column, index] = match as unknown as [string, string, string];
    if (!rows.has(index)) rows.set(index, new Map());
    rows.get(index)!.set(Number(column), varbind);
  }
  const result: WifiInterface[] = [];
  for (const [index, columns] of rows) {
    const name = displayString(columns.get(2));
    if (!name) continue;
    const frequency = displayString(columns.get(4))?.trim();
    const frequencyMhz = frequency && /^[0-9]+(?:\.[0-9]+)?$/.test(frequency) ? Number(frequency) : undefined;
    result.push({
      index, name, ssid: displayString(columns.get(3)) ?? "", channel: displayString(columns.get(5)) ?? "", frequencyMhz,
      raw: Object.fromEntries([2, 3, 4, 5].map((column) => [`col${column}`, rawVarbind(columns.get(column))])),
    });
  }
  return result.sort((a, b) => Number(a.index) - Number(b.index));
}

/** Walk the router's own Wi-Fi interfaces. A missing table yields an empty list. */
export async function collectWifiInterfaces(options: Options): Promise<WifiInterface[]> {
  const session = sessionFor(options, { timeout: 2_000, retries: 0 });
  try {
    const varbinds: snmp.Varbind[] = [];
    await new Promise<void>((resolve, reject) => {
      session.subtree(WIFI_INTERFACES, 20, (batch) => {
        for (const varbind of batch) if (!snmp.isVarbindError(varbind)) varbinds.push(varbind);
      }, (error) => { if (error) reject(error); else resolve(); });
    });
    return wifiInterfacesFromWalk(varbinds);
  } finally { session.close(); }
}
