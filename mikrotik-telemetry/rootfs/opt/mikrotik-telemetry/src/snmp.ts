import * as snmp from "net-snmp";
import type { Options } from "./config.js";
import type { SnmpSample } from "./metrics.js";
import type { WifiAssociation } from "./wifi.js";

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

/** Discover only configured MACs and read each association's counters in a single request. */
export async function collectWifiSnmp(options: Options): Promise<WifiAssociation[]> {
  const session = sessionFor(options, { timeout: 2_000, retries: 0 });
  const result: WifiAssociation[] = [];
  try {
    for (const { name, macIndex } of options.wifiClients) {
      const indices = await wifiInterfaceIndices(session, `${WIFI_TABLE}.4.${macIndex}`);
      for (const ifIndex of indices) {
        const suffix = `${macIndex}.${ifIndex}`;
        const [uptime, tx, rx] = await get(session, [`${WIFI_TABLE}.4.${suffix}`, `${WIFI_TABLE}.13.${suffix}`, `${WIFI_TABLE}.14.${suffix}`]);
        if (uptime?.type !== snmp.ObjectType.TimeTicks
            || tx?.type !== snmp.ObjectType.Counter64 || rx?.type !== snmp.ObjectType.Counter64) continue;
        const uptimeTicks = numeric(uptime.value);
        const transmit = decodeCounter64(tx.value);
        const receive = decodeCounter64(rx.value);
        if (uptimeTicks !== undefined && receive !== undefined && transmit !== undefined)
          result.push({ client: name, ifIndex, receive: BigInt(receive), transmit: BigInt(transmit), uptimeTicks });
      }
    }
    return result;
  } finally { session.close(); }
}
