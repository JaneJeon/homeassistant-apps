import * as cheerio from "cheerio";
import type { Cheerio, CheerioAPI } from "cheerio";
import type { AnyNode } from "domhandler";
import type {
  Channel,
  CodewordSlot,
  GatewayLog,
  GroupErrorCode,
  HardwareSnapshot,
  LanPort,
  LanSnapshot,
  NetworkSnapshot,
} from "./types.js";

class ParseFailure extends Error {
  constructor(readonly code: GroupErrorCode) {
    super(code);
  }
}

type Rows = Map<string, string>;
type ChannelRows = Map<string, string[]>;

function fail(code: GroupErrorCode): never {
  throw new ParseFailure(code);
}

function text(el: Cheerio<AnyNode>): string {
  return el.text().replace(/\s+/g, " ").trim();
}

function required(value: string | undefined): string {
  const clean = value?.trim();
  if (!clean) fail("missing_field");
  return clean;
}

function number(value: string): number {
  const clean = value.trim();
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(clean)) fail("invalid_value");
  const parsed = Number(clean);
  if (!Number.isFinite(parsed)) fail("invalid_value");
  return parsed;
}

function logNumber(value: string, label: string): number {
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value.trim())) {
    throw new Error(`Invalid number for ${label}: ${value}`);
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed))
    throw new Error(`Invalid number for ${label}: ${value}`);
  return parsed;
}

function counter(value: string): string {
  const clean = value.trim();
  if (!/^\d+$/.test(clean)) fail("invalid_value");
  return clean;
}

function labelOf($: CheerioAPI, row: AnyNode): string {
  return text($(row).find(".readonlyLabel").first()).replace(/:\s*$/, "");
}

function readRows($: CheerioAPI, module: Cheerio<AnyNode>): Rows {
  const rows: Rows = new Map();
  module.find(".form-row").each((_, row) => {
    const label = labelOf($, row);
    if (!label || rows.has(label)) fail("invalid_schema");
    rows.set(label, text($(row).find(".value").first()));
  });
  return rows;
}

function sectionRows($: CheerioAPI, title: string): Rows | null {
  const sections = $(".module.forms").filter(
    (_, module) => text($(module).find("h2").first()) === title,
  );
  if (sections.length === 0) return null;
  if (sections.length !== 1) fail("invalid_schema");
  return readRows($, sections.first());
}

function labeledValue($: CheerioAPI, label: string): string | null {
  const matches: string[] = [];
  $(".readonlyLabel").each((_, el) => {
    if (text($(el)).replace(/:\s*$/, "") === label) {
      matches.push(text($(el).closest(".form-row").find(".value").first()));
    }
  });
  if (matches.length > 1) fail("invalid_schema");
  return matches[0] ?? null;
}

function field(rows: Rows | null, label: string): string {
  if (!rows) fail("missing_section");
  if (!rows.has(label)) fail("missing_field");
  return required(rows.get(label));
}

function valueFromAnySection($: CheerioAPI, label: string): string {
  const value = labeledValue($, label);
  if (value === null) fail("missing_field");
  return required(value);
}

function uptimeSeconds(raw: string): number {
  const match = raw.match(
    /^(?:(\d+)\s+days?\s+)?(?:(\d+)h:\s*)?(?:(\d+)m:\s*)?(\d+)s$/i,
  );
  if (!match) fail("invalid_value");
  const [, days = "0", hours = "0", minutes = "0", seconds] = match;
  const result =
    Number(days) * 86400 +
    Number(hours) * 3600 +
    Number(minutes) * 60 +
    Number(seconds);
  if (!Number.isFinite(result)) fail("invalid_value");
  if (result <= 0) fail("nonpositive_value");
  return result;
}

function unitNumber(raw: string, unit: string): number {
  const match = raw.match(
    new RegExp(`^([+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+))\\s*${unit}$`, "i"),
  );
  if (!match) fail("invalid_value");
  return number(required(match[1]));
}

function memory(raw: string): { amount: number; unit: string } {
  const match = raw.match(/^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+([A-Za-z]+)$/);
  if (!match) fail("invalid_value");
  return { amount: number(required(match[1])), unit: required(match[2]) };
}

function attempt<T>(
  errors: Record<string, GroupErrorCode>,
  group: string,
  parse: () => T,
): T | null {
  try {
    return parse();
  } catch (error) {
    errors[group] =
      error instanceof ParseFailure ? error.code : "invalid_schema";
    return null;
  }
}

function channelRows(
  $: CheerioAPI,
  selector: string,
  count: number,
): ChannelRows {
  const tables = $(selector);
  if (tables.length === 0) fail("missing_section");
  if (tables.length !== 1) fail("invalid_schema");
  const rows: ChannelRows = new Map();
  tables.find("tbody tr").each((_, row) => {
    const cells = $(row).children("th,td");
    if (cells.length < 2) fail("invalid_schema");
    const label = text(cells.first());
    if (!label || rows.has(label)) fail("invalid_schema");
    const values = cells
      .slice(1)
      .toArray()
      .map((cell) => text($(cell)));
    if (values.length !== count) fail("invalid_schema");
    rows.set(label, values);
  });
  return rows;
}

function channelRow(rows: ChannelRows, label: string, count: number): string[] {
  const values = rows.get(label);
  if (!values) fail("invalid_schema");
  if (values.length !== count) fail("invalid_schema");
  return values;
}

function cell(values: string[], index: number): string {
  const value = values[index];
  if (value === undefined) fail("invalid_schema");
  return value;
}

function channelNumber(
  raw: string,
  unit: string,
  locked: boolean,
): number | null {
  if (!raw.trim()) {
    if (locked) fail("missing_field");
    return null;
  }
  const value = unitNumber(raw, unit);
  if (unit === "MHz" && value <= 0) fail("nonpositive_value");
  return value;
}

function parseChannels(
  $: CheerioAPI,
  selector: string,
  slots: number,
  downstream: boolean,
): Channel[] {
  const rows = channelRows($, selector, slots);
  const indexes = channelRow(rows, "Index", slots);
  const locks = channelRow(rows, "Lock Status", slots);
  const frequencies = channelRow(rows, "Frequency", slots);
  const powers = channelRow(rows, "Power Level", slots);
  const modulations = channelRow(rows, "Modulation", slots);
  const snrs = downstream ? channelRow(rows, "SNR", slots) : null;

  return Array.from({ length: slots }, (_, i) => {
    const slot = number(cell(indexes, i));
    if (slot !== i + 1) fail("invalid_schema");
    const lock = cell(locks, i);
    if (lock !== "Locked" && lock !== "Not Locked") fail("invalid_value");
    const locked = lock === "Locked";
    const modulation = cell(modulations, i) || null;
    if (locked && !modulation) fail("missing_field");
    const channel: Channel = {
      slot,
      locked,
      frequencyMHz: channelNumber(cell(frequencies, i), "MHz", locked),
      snrDb: snrs ? channelNumber(cell(snrs, i), "dB", locked) : null,
      powerDbmv: channelNumber(cell(powers, i), "dBmV", locked),
      modulation,
    };
    return channel;
  });
}

function parseCodewords($: CheerioAPI): CodewordSlot[] {
  const rows = channelRows($, ".module.div_code table.data", 32);
  const unerrored = channelRow(rows, "Unerrored Codewords", 32);
  const correctable = channelRow(rows, "Correctable Codewords", 32);
  const uncorrectable = channelRow(rows, "Uncorrectable Codewords", 32);
  return Array.from({ length: 32 }, (_, i) => ({
    slot: i + 1,
    unerrored: counter(cell(unerrored, i)),
    correctable: counter(cell(correctable, i)),
    uncorrectable: counter(cell(uncorrectable, i)),
  }));
}

function hasGatewayConnectionPage($: CheerioAPI): boolean {
  const heading = text($("h1").first()).replace(/\s*>\s*/g, " > ");
  if (!/^Gateway > Connection(?: >.*)?$/i.test(heading)) return false;
  const hasRf =
    $(".module.div_down").length > 0 &&
    $(".module.div_up").length > 0 &&
    $(".module.div_code").length > 0;
  const hasLabel = (label: string) =>
    $(".readonlyLabel")
      .toArray()
      .some((element) => text($(element)).replace(/:\s*$/, "") === label);
  const hasNetworkStatus = hasLabel("Internet") && hasLabel("System Uptime");
  return hasRf || hasNetworkStatus;
}

export function parseNetwork(html: string): NetworkSnapshot {
  const $ = cheerio.load(html);
  if (!hasGatewayConnectionPage($)) throw new Error("Unexpected network page");

  const groupErrors: Record<string, GroupErrorCode> = {};
  const gatewayTimeRaw = attempt(groupErrors, "clock", () =>
    valueFromAnySection($, "Local time"),
  );
  const uptimeValue = attempt(groupErrors, "uptime", () =>
    uptimeSeconds(valueFromAnySection($, "System Uptime")),
  );
  const internetActive = attempt(groupErrors, "internet_ui_status", () => {
    const raw = valueFromAnySection($, "Internet");
    if (raw === "Active") return true;
    if (raw === "Inactive") return false;
    fail("invalid_value");
  });

  const modemRows = attempt(groupErrors, "modem", () =>
    sectionRows($, "Cable Modem"),
  );
  const modem = {
    model: attempt(groupErrors, "modem", () => field(modemRows, "Model")),
    firmware: attempt(groupErrors, "modem", () =>
      field(modemRows, "Download Version"),
    ),
    hardwareVersion: attempt(groupErrors, "modem", () =>
      field(modemRows, "HW Version"),
    ),
  };
  const downstream = attempt(groupErrors, "downstream", () =>
    parseChannels($, ".module.div_down table.data", 32, true),
  );
  const upstream = attempt(groupErrors, "upstream", () =>
    parseChannels($, ".module.div_up table.data", 8, false),
  );
  const codewords = attempt(groupErrors, "codewords", () => parseCodewords($));

  return {
    gatewayTimeRaw,
    uptimeSeconds: uptimeValue,
    internetActive,
    modem,
    downstream,
    upstream,
    codewords,
    groupErrors,
  };
}

function hardwarePage($: CheerioAPI): boolean {
  const heading = text($("h1").first()).replace(/\s*>\s*/g, " > ");
  return /^Gateway > Hardware(?: >.*)?$/i.test(heading);
}

export function parseHardware(html: string): HardwareSnapshot {
  const $ = cheerio.load(html);
  if (!hardwarePage($)) throw new Error("Unexpected hardware page");
  const groupErrors: Record<string, GroupErrorCode> = {};
  const rows = attempt(groupErrors, "hardware_section", () =>
    sectionRows($, "System Hardware"),
  );
  const model = attempt(groupErrors, "model", () => field(rows, "Model"));
  const cpuMhz = attempt(groupErrors, "cpu", () =>
    unitNumber(field(rows, "Processor Speed"), "MHz"),
  );

  const dramReported = attempt(groupErrors, "dram", () => {
    const total = memory(field(rows, "DRAM Total Memory"));
    const used = memory(field(rows, "DRAM Used Memory"));
    const available = memory(field(rows, "DRAM Available Memory"));
    if (total.unit !== used.unit || total.unit !== available.unit)
      fail("invalid_schema");
    if (total.amount <= 0) fail("nonpositive_value");
    if (used.amount < 0 || available.amount < 0) fail("invalid_value");
    return {
      total: total.amount,
      used: used.amount,
      available: available.amount,
      unit: total.unit,
    };
  });

  const flashMb = attempt(groupErrors, "flash", () => {
    const total = unitNumber(field(rows, "Flash Total Memory"), "MB");
    if (total <= 0) fail("nonpositive_value");
    const used = unitNumber(field(rows, "Flash Used Memory"), "MB");
    const available = unitNumber(field(rows, "Flash Available Memory"), "MB");
    if (used < 0 || available < 0) fail("invalid_value");
    return {
      total,
      used,
      available,
    };
  });
  return { model, cpuMhz, dramReported, flashMb, groupErrors };
}

function lanSectionRows($: CheerioAPI): Map<number, Rows> {
  const result = new Map<number, Rows>();
  $(".module.forms").each((_, module) => {
    const title = text($(module).find("h2").first());
    const match = title.match(/^LAN Ethernet Port\s+(\d+)$/i);
    if (!match) return;
    const port = Number(match[1]);
    if (!Number.isInteger(port) || result.has(port)) fail("invalid_schema");
    result.set(port, readRows($, $(module)));
  });
  return result;
}

function parsePortSpeed(raw: string): number | null {
  if (/^(?:not applicable|disconnected)$/i.test(raw.trim())) return null;
  const speed = unitNumber(raw, "Mbps");
  if (speed <= 0) fail("nonpositive_value");
  return speed;
}

function lanPage($: CheerioAPI): boolean {
  const heading = text($("h1").first()).replace(/\s*>\s*/g, " > ");
  return /^Gateway > Hardware > LAN Ethernet(?: >.*)?$/i.test(heading);
}

export function parseLan(html: string): LanSnapshot {
  const $ = cheerio.load(html);
  if (!lanPage($)) throw new Error("Unexpected LAN page");
  const groupErrors: Record<string, GroupErrorCode> = {};
  const ports = attempt(groupErrors, "lan", () => {
    const sections = lanSectionRows($);
    if ([1, 2, 3, 4].some((port) => !sections.has(port)) || sections.size !== 4)
      fail("missing_section");
    const result: LanPort[] = [];
    for (let port = 1; port <= 4; port += 1) {
      const rows = sections.get(port) ?? fail("missing_section");
      const rawStatus = field(rows, "LAN Ethernet link status");
      const active =
        rawStatus === "Active"
          ? true
          : rawStatus === "Inactive"
            ? false
            : fail("invalid_value");
      result.push({
        port,
        active,
        speedMbps: parsePortSpeed(field(rows, "Connection Speed")),
      });
    }
    return result;
  });
  return { ports, groupErrors };
}

export function parseLogs(
  input: string,
  kind: GatewayLog["kind"],
): GatewayLog[] {
  let decoded: unknown;
  try {
    decoded = JSON.parse(input);
  } catch {
    throw new Error("Invalid log response JSON");
  }
  if (!Array.isArray(decoded))
    throw new Error("Invalid log response: expected an array");

  return decoded.map((row, index) => {
    if (!row || typeof row !== "object" || Array.isArray(row))
      throw new Error(`Invalid log row ${index}`);
    const record = row as Record<string, unknown>;
    const time = record.time;
    const description = record.Des;
    if (
      typeof time !== "string" ||
      !time.trim() ||
      typeof description !== "string" ||
      !description.trim()
    ) {
      throw new Error(`Invalid log row ${index}: missing time or Des`);
    }
    const result: GatewayLog = {
      kind,
      gatewayTimeRaw: time.trim(),
      description: description.trim(),
      raw: record,
    };
    if (kind === "system" || kind === "event") {
      if (typeof record.Level !== "string" || !record.Level.trim())
        throw new Error(`Invalid ${kind} log row ${index}: missing Level`);
      result.level = record.Level.trim();
    } else if (kind === "firewall") {
      const count = record.Count;
      if (typeof count !== "number" && typeof count !== "string")
        throw new Error(`Invalid firewall log row ${index}: missing Count`);
      result.count =
        typeof count === "number" ? count : logNumber(count, "Count");
      if (!Number.isInteger(result.count) || result.count < 0)
        throw new Error(`Invalid firewall Count in row ${index}`);
      if (typeof record.Type !== "string" || !record.Type.trim())
        throw new Error(`Invalid firewall log row ${index}: missing Type`);
      result.type = record.Type.trim();
    }
    return result;
  });
}
