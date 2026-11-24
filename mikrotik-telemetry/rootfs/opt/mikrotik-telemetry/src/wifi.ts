export interface WifiAssociation {
  client: string;
  ifIndex: string;
  receive: bigint;
  transmit: bigint;
  uptimeTicks: number;
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
