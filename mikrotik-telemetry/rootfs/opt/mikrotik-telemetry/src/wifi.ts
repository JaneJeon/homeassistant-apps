export interface WifiAssociation {
  client: string;
  ifIndex: string;
  receive: bigint;
  transmit: bigint;
  uptimeTicks: number;
  /** Raw mtxrWifiRegistrationSignal (column 6). */
  signal?: number;
  /** mtxrWifiRegistrationBand text (column 8), such as "5ghz-ax". */
  band?: string;
  /** Raw mtxrWifiRegistrationTxRate and RxRate (columns 9 and 10); the MIB states no unit. */
  txRate?: number;
  rxRate?: number;
  /** Raw column values for the diagnostic log line. Never contains the MAC address. */
  raw?: string;
}

/** One row of mtxrWifiInterfaces: the router's own radio interfaces, not stations. */
export interface WifiInterface {
  index: string;
  name: string;
  ssid: string;
  channel: string;
  frequencyMhz?: number;
  /** Raw column values for the diagnostic log line. */
  raw: Record<string, string>;
}

export interface WifiTotals {
  client: string;
  ifIndex: string;
  receive: bigint;
  transmit: bigint;
}

/** Total observed deltas. Each first sample or reset becomes the next baseline. */
export class WifiCounters {
  private previous = new Map<string, WifiAssociation>();
  private totals = new Map<string, WifiTotals>();

  observe(rows: WifiAssociation[]): WifiTotals[] {
    const current = new Map(rows.map((row) => [`${row.client}:${row.ifIndex}`, row]));
    for (const [key, row] of current) {
      const before = this.previous.get(key);
      const total = this.totals.get(key) ?? {
        client: row.client, ifIndex: row.ifIndex, receive: 0n, transmit: 0n,
      };
      if (before && row.uptimeTicks >= before.uptimeTicks
          && row.receive >= before.receive && row.transmit >= before.transmit) {
        total.receive += row.receive - before.receive;
        total.transmit += row.transmit - before.transmit;
      }
      this.totals.set(key, total);
    }
    this.previous = current;
    return [...current.keys()].map((key) => this.totals.get(key)!);
  }
}

/** Apply one client poll: `undefined` marks a failed poll, which clears link gauges and rebaselines counters. */
export function recordWifiPoll(
  state: { wifiTotals: WifiTotals[]; wifiStations: WifiAssociation[] },
  counters: WifiCounters, rows: WifiAssociation[] | undefined,
): void {
  state.wifiTotals = counters.observe(rows ?? []);
  state.wifiStations = rows ?? [];
}
