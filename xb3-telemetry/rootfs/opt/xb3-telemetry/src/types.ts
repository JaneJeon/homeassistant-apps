export type GroupErrorCode =
  | "missing_section"
  | "missing_field"
  | "invalid_schema"
  | "invalid_value"
  | "nonpositive_value";

export interface Channel {
  slot: number;
  locked: boolean;
  frequencyMHz: number | null;
  snrDb: number | null;
  powerDbmv: number | null;
  modulation: string | null;
}

export interface CodewordSlot {
  slot: number;
  unerrored: string;
  correctable: string;
  uncorrectable: string;
}

export interface NetworkSnapshot {
  gatewayTimeRaw: string | null;
  uptimeSeconds: number | null;
  internetActive: boolean | null;
  modem: {
    model: string | null;
    firmware: string | null;
    hardwareVersion: string | null;
  };
  downstream: Channel[] | null;
  upstream: Channel[] | null;
  codewords: CodewordSlot[] | null;
  groupErrors: Record<string, GroupErrorCode>;
}

export interface HardwareSnapshot {
  model: string | null;
  cpuMhz: number | null;
  dramReported: {
    total: number;
    used: number;
    available: number;
    unit: string;
  } | null;
  flashMb: { total: number; used: number; available: number } | null;
  groupErrors: Record<string, GroupErrorCode>;
}

export interface LanPort {
  port: number;
  active: boolean;
  speedMbps: number | null;
}

export interface LanSnapshot {
  ports: LanPort[] | null;
  groupErrors: Record<string, GroupErrorCode>;
}

export interface GatewayLog {
  kind: "system" | "event" | "firewall";
  gatewayTimeRaw: string;
  description: string;
  raw: Record<string, unknown>;
  level?: string;
  count?: number;
  type?: string;
}
