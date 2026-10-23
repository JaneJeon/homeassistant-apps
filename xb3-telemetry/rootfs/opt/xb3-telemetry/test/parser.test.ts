import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import * as cheerio from "cheerio";
import {
  parseHardware,
  parseLan,
  parseLogs,
  parseNetwork,
} from "../src/parser.js";
import type { GroupErrorCode } from "../src/types.js";

const fixturePath = (name: string) =>
  fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));
const fixture = (name: string) => readFileSync(fixturePath(name), "utf8");
const network = fixture("network.html");
const hardware = fixture("hardware.html");
const partialNetwork = fixture("network-partial.html");

const allowedErrors = new Set<GroupErrorCode>([
  "missing_section",
  "missing_field",
  "invalid_schema",
  "invalid_value",
  "nonpositive_value",
]);

function assertBoundedErrors(errors: Record<string, GroupErrorCode>) {
  for (const code of Object.values(errors)) assert.ok(allowedErrors.has(code));
  assert.ok(Object.values(errors).every((value) => !/[\r\n]/.test(value)));
}

describe("parseNetwork", () => {
  it("parses captured channels and preserves displayed UI slot positions", () => {
    const result = parseNetwork(network);
    assert.equal(result.internetActive, true);
    assert.equal(result.gatewayTimeRaw, "2000-01-02 00:06:05");
    assert.equal(result.uptimeSeconds, 2 * 86400 + 17 * 3600 + 39 * 60 + 15);
    assert.equal(result.modem.model, "TG1682P");
    assert.equal(result.modem.hardwareVersion, "11.0");
    assert.equal(result.modem.firmware, "FIXTURE_FIRMWARE");
    assert.equal(result.groupErrors.modem, undefined);
    assert.equal(result.downstream?.length, 32);
    assert.equal(
      result.downstream?.filter((channel) => channel.locked).length,
      24,
    );
    assert.equal(result.upstream?.length, 8);
    assert.equal(
      result.upstream?.filter((channel) => channel.locked).length,
      4,
    );
    assert.deepEqual(result.downstream?.[0], {
      slot: 1,
      locked: true,
      frequencyMHz: 675,
      snrDb: 40.37,
      powerDbmv: 7.6,
      modulation: "256 QAM",
    });
    assert.equal(result.codewords?.[1].uncorrectable, "151504");
    assert.deepEqual(
      result.downstream
        ?.slice(24)
        .map(
          ({ slot, locked, frequencyMHz, snrDb, powerDbmv, modulation }) => ({
            slot,
            locked,
            frequencyMHz,
            snrDb,
            powerDbmv,
            modulation,
          }),
        ),
      Array.from({ length: 8 }, (_, index) => ({
        slot: index + 25,
        locked: false,
        frequencyMHz: null,
        snrDb: null,
        powerDbmv: null,
        modulation: null,
      })),
    );
    assert.deepEqual(
      result.codewords?.slice(24),
      Array.from({ length: 8 }, (_, index) => ({
        slot: index + 25,
        unerrored: "0",
        correctable: "0",
        uncorrectable: "0",
      })),
    );
    assert.equal(result.groupErrors.downstream, undefined);
    assert.equal(result.groupErrors.upstream, undefined);
    assert.equal(result.groupErrors.codewords, undefined);
    assertBoundedErrors(result.groupErrors);
  });

  it("keeps valid RF groups when the captured portal metadata is blank or false-zero", () => {
    const result = parseNetwork(partialNetwork);
    assert.equal(result.gatewayTimeRaw, null);
    assert.equal(result.uptimeSeconds, null);
    assert.equal(result.internetActive, true);
    assert.deepEqual(result.modem, {
      model: null,
      firmware: null,
      hardwareVersion: null,
    });
    assert.equal(result.downstream?.length, 32);
    assert.equal(result.upstream?.length, 8);
    assert.equal(result.codewords?.length, 32);
    assert.equal(result.groupErrors.clock, "missing_field");
    assert.equal(result.groupErrors.uptime, "nonpositive_value");
    assert.equal(result.groupErrors.internet_ui_status, undefined);
    assert.equal(result.groupErrors.modem, "missing_field");
    assertBoundedErrors(result.groupErrors);
  });

  it("treats uptime zero as unknown while keeping the other metadata values", () => {
    const $ = cheerio.load(network);
    $(".module.forms .readonlyLabel")
      .filter((_, el) => $(el).text().trim() === "System Uptime:")
      .closest(".form-row")
      .find(".value")
      .text("0 days 0h: 0m: 0s");
    const result = parseNetwork($.html());
    assert.equal(result.gatewayTimeRaw, "2000-01-02 00:06:05");
    assert.equal(result.internetActive, true);
    assert.equal(result.uptimeSeconds, null);
    assert.equal(result.groupErrors.clock, undefined);
    assert.equal(result.groupErrors.uptime, "nonpositive_value");
    assert.equal(result.groupErrors.internet_ui_status, undefined);
  });

  it("keeps positive uptime independent when the displayed gateway clock is blank", () => {
    const $ = cheerio.load(network);
    $(".module.forms .readonlyLabel")
      .filter((_, el) => $(el).text().trim() === "Local time:")
      .closest(".form-row")
      .find(".value")
      .empty();
    const result = parseNetwork($.html());
    assert.equal(result.gatewayTimeRaw, null);
    assert.equal(result.uptimeSeconds, 2 * 86400 + 17 * 3600 + 39 * 60 + 15);
    assert.equal(result.internetActive, true);
    assert.equal(result.groupErrors.clock, "missing_field");
    assert.equal(result.groupErrors.uptime, undefined);
    assert.equal(result.groupErrors.internet_ui_status, undefined);
  });

  it("rejects login and unrelated pages, and reports malformed RF as a group error", () => {
    assert.throws(
      () => parseNetwork("<html><body>Login</body></html>"),
      /Unexpected network page/,
    );
    assert.throws(
      () =>
        parseNetwork(
          '<h1>Gateway &gt; Hardware</h1><div class="module div_down"></div>',
        ),
      /Unexpected network page/,
    );

    const $ = cheerio.load(network);
    const frequency = $(".module.div_down table.data tbody tr").filter(
      (_, row) => $(row).children("th").first().text().trim() === "Frequency",
    );
    frequency.children("td").eq(0).text("bad MHz");
    const malformed = parseNetwork($.html());
    assert.equal(malformed.downstream, null);
    assert.equal(malformed.groupErrors.downstream, "invalid_value");
    assert.equal(malformed.upstream?.length, 8);
  });

  it("rejects a downstream row whose slot count shifts after a missing table cell", () => {
    const $ = cheerio.load(network);
    const frequency = $(".module.div_down table.data tbody tr").filter(
      (_, row) => $(row).children("th").first().text().trim() === "Frequency",
    );
    frequency.children("td").eq(0).remove();
    const result = parseNetwork($.html());
    assert.equal(result.downstream, null);
    assert.equal(result.groupErrors.downstream, "invalid_schema");
    assert.equal(result.upstream?.length, 8);
  });

  it("rejects a zero frequency as unknown while preserving the separate upstream group", () => {
    const $ = cheerio.load(network);
    const frequency = $(".module.div_down table.data tbody tr").filter(
      (_, row) => $(row).children("th").first().text().trim() === "Frequency",
    );
    frequency.children("td").eq(0).text("0 MHz");
    const result = parseNetwork($.html());
    assert.equal(result.downstream, null);
    assert.equal(result.groupErrors.downstream, "nonpositive_value");
    assert.equal(result.upstream?.length, 8);
  });

  it("keeps downstream RF valid when the independent codeword table is malformed", () => {
    const $ = cheerio.load(network);
    $(".module.div_code table.data tbody tr")
      .first()
      .children("td")
      .eq(0)
      .remove();
    const result = parseNetwork($.html());
    assert.equal(result.downstream?.length, 32);
    assert.equal(result.codewords, null);
    assert.equal(result.groupErrors.codewords, "invalid_schema");
  });
});

describe("parseHardware", () => {
  it("parses hardware values and leaves the reported DRAM unit untouched", () => {
    const result = parseHardware(hardware);
    assert.deepEqual(result, {
      model: "TG1682P",
      cpuMhz: 450,
      dramReported: {
        total: 1048576,
        used: 995932,
        available: 52644,
        unit: "MB",
      },
      flashMb: { total: 256, used: 252, available: 4 },
      groupErrors: {},
    });
  });

  it("does not turn false-zero flash into a healthy zero reading", () => {
    const $ = cheerio.load(hardware);
    $(".module.forms .readonlyLabel")
      .filter((_, el) => $(el).text().trim() === "Flash Total Memory:")
      .closest(".form-row")
      .find(".value")
      .text("0 MB");
    const result = parseHardware($.html());
    assert.equal(result.flashMb, null);
    assert.equal(result.groupErrors.flash, "nonpositive_value");
    assert.deepEqual(result.dramReported, {
      total: 1048576,
      used: 995932,
      available: 52644,
      unit: "MB",
    });
  });

  it("rejects nonpositive DRAM total and negative memory operands", () => {
    const zeroTotal = cheerio.load(hardware);
    zeroTotal(".module.forms .readonlyLabel")
      .filter((_, el) => zeroTotal(el).text().trim() === "DRAM Total Memory:")
      .closest(".form-row")
      .find(".value")
      .text("0 MB");
    const zeroResult = parseHardware(zeroTotal.html());
    assert.equal(zeroResult.dramReported, null);
    assert.equal(zeroResult.groupErrors.dram, "nonpositive_value");

    const negativeUsed = cheerio.load(hardware);
    negativeUsed(".module.forms .readonlyLabel")
      .filter((_, el) => negativeUsed(el).text().trim() === "DRAM Used Memory:")
      .closest(".form-row")
      .find(".value")
      .text("-1 MB");
    const negativeResult = parseHardware(negativeUsed.html());
    assert.equal(negativeResult.dramReported, null);
    assert.equal(negativeResult.groupErrors.dram, "invalid_value");
  });

  it("leaves flash unavailable if a raw component is missing", () => {
    const $ = cheerio.load(hardware);
    $(".module.forms .readonlyLabel")
      .filter((_, el) => $(el).text().trim() === "Flash Used Memory:")
      .closest(".form-row")
      .remove();
    const result = parseHardware($.html());
    assert.equal(result.flashMb, null);
    assert.equal(result.groupErrors.flash, "missing_field");
  });

  it("rejects negative flash operands without disturbing independent hardware groups", () => {
    const $ = cheerio.load(hardware);
    $(".module.forms .readonlyLabel")
      .filter((_, el) => $(el).text().trim() === "Flash Used Memory:")
      .closest(".form-row")
      .find(".value")
      .text("-1 MB");
    const result = parseHardware($.html());
    assert.equal(result.flashMb, null);
    assert.equal(result.groupErrors.flash, "invalid_value");
    assert.equal(result.dramReported?.total, 1048576);
  });

  it("keeps independent hardware groups available when one numeric group is malformed", () => {
    const $ = cheerio.load(hardware);
    $(".module.forms .readonlyLabel")
      .filter((_, el) => $(el).text().trim() === "DRAM Used Memory:")
      .closest(".form-row")
      .find(".value")
      .text("unknown");
    const result = parseHardware($.html());
    assert.equal(result.dramReported, null);
    assert.equal(result.groupErrors.dram, "invalid_value");
    assert.equal(result.cpuMhz, 450);
    assert.equal(result.flashMb?.total, 256);
    assertBoundedErrors(result.groupErrors);
  });

  it("rejects unrelated pages and leaves malformed CPU unknown", () => {
    assert.throws(
      () => parseHardware("<html>Login</html>"),
      /Unexpected hardware page/,
    );
    const $ = cheerio.load(hardware);
    $(".module.forms .readonlyLabel")
      .filter((_, el) => $(el).text().trim() === "Processor Speed:")
      .closest(".form-row")
      .find(".value")
      .text("fast");
    const result = parseHardware($.html());
    assert.equal(result.cpuMhz, null);
    assert.equal(result.groupErrors.cpu, "invalid_value");
    assert.equal(result.flashMb?.total, 256);
  });
});

describe("parseLan", () => {
  const lanHtml = fixture("lan.html");

  it("reports incomplete LAN enumeration as unavailable with a quality code", () => {
    const $ = cheerio.load(lanHtml);
    $(".module.forms")
      .filter(
        (_, el) => $(el).find("h2").text().trim() === "LAN Ethernet Port 4",
      )
      .remove();
    const result = parseLan($.html());
    assert.equal(result.ports, null);
    assert.equal(result.groupErrors.lan, "missing_section");
  });

  it("rejects login and unrelated pages before returning an empty LAN snapshot", () => {
    assert.throws(() => parseLan("<html>Login</html>"), /Unexpected LAN page/);
    assert.throws(
      () => parseLan("<h1>Gateway &gt; Connection</h1>"),
      /Unexpected LAN page/,
    );
  });

  it("parses the four captured port positions and omits disconnected speeds", () => {
    const result = parseLan(lanHtml);
    assert.deepEqual(result.ports, [
      { port: 1, active: true, speedMbps: 1000 },
      { port: 2, active: true, speedMbps: 1000 },
      { port: 3, active: false, speedMbps: null },
      { port: 4, active: false, speedMbps: null },
    ]);
  });

  it("rejects invalid status and nonpositive measured link speeds as a group", () => {
    const zeroSpeedHtml = cheerio.load(lanHtml);
    zeroSpeedHtml(".module.forms")
      .filter(
        (_, el) =>
          zeroSpeedHtml(el).find("h2").text().trim() === "LAN Ethernet Port 1",
      )
      .find(".readonlyLabel")
      .filter(
        (_, el) => zeroSpeedHtml(el).text().trim() === "Connection Speed:",
      )
      .closest(".form-row")
      .find(".value")
      .text("0 Mbps");
    const zeroSpeed = parseLan(zeroSpeedHtml.html());
    assert.equal(zeroSpeed.ports, null);
    assert.equal(zeroSpeed.groupErrors.lan, "nonpositive_value");

    const invalidStatusHtml = cheerio.load(lanHtml);
    invalidStatusHtml(".module.forms")
      .filter(
        (_, el) =>
          invalidStatusHtml(el).find("h2").text().trim() ===
          "LAN Ethernet Port 1",
      )
      .find(".readonlyLabel")
      .filter(
        (_, el) =>
          invalidStatusHtml(el).text().trim() === "LAN Ethernet link status:",
      )
      .closest(".form-row")
      .find(".value")
      .text("Unknown");
    const invalidStatus = parseLan(invalidStatusHtml.html());
    assert.equal(invalidStatus.ports, null);
    assert.equal(invalidStatus.groupErrors.lan, "invalid_value");
  });
});

describe("parseLogs", () => {
  it("accepts an empty array and validates event fields without changing displayed time", () => {
    assert.deepEqual(parseLogs("[]", "system"), []);
    assert.deepEqual(
      parseLogs(
        JSON.stringify([
          { time: "2000-01-02 00:00:01", Des: "Started", Level: "Notice" },
        ]),
        "system",
      ),
      [
        {
          kind: "system",
          gatewayTimeRaw: "2000-01-02 00:00:01",
          description: "Started",
          raw: { time: "2000-01-02 00:00:01", Des: "Started", Level: "Notice" },
          level: "Notice",
        },
      ],
    );
  });

  it("parses firewall fields and rejects invalid bodies or rows", () => {
    assert.deepEqual(
      parseLogs(
        JSON.stringify([
          {
            time: "2000-01-02 00:00:01",
            Des: "Blocked",
            Count: 2,
            Type: "DROP",
          },
        ]),
        "firewall",
      )[0],
      {
        kind: "firewall",
        gatewayTimeRaw: "2000-01-02 00:00:01",
        description: "Blocked",
        raw: {
          time: "2000-01-02 00:00:01",
          Des: "Blocked",
          Count: 2,
          Type: "DROP",
        },
        count: 2,
        type: "DROP",
      },
    );
    assert.throws(
      () => parseLogs("not json", "system"),
      /Invalid log response JSON/,
    );
    assert.throws(
      () => parseLogs("<html>Login</html>", "system"),
      /Invalid log response JSON/,
    );
    assert.throws(() => parseLogs("{}", "event"), /expected an array/);
    assert.throws(() => parseLogs("[{}]", "system"), /missing time or Des/);
    assert.throws(
      () =>
        parseLogs(
          JSON.stringify([
            {
              time: "2000-01-02 00:00:01",
              Des: "Blocked",
              Count: "bad",
              Type: "DROP",
            },
          ]),
          "firewall",
        ),
      /Invalid number for Count/,
    );
  });

  it("retains the complete decoded firewall row alongside normalized fields", () => {
    const source = JSON.parse(fixture("firewall.json")) as Record<
      string,
      unknown
    >[];
    const parsed = parseLogs(fixture("firewall.json"), "firewall");
    assert.equal(parsed.length, 2);
    assert.deepEqual(
      parsed.map((row) => row.raw),
      source,
    );
    assert.deepEqual(
      parsed.map(({ gatewayTimeRaw, description, count, type }) => ({
        gatewayTimeRaw,
        description,
        count,
        type,
      })),
      [
        {
          gatewayTimeRaw: "2000/01/02 00:04:38",
          description: "FW.IPv6 INPUT drop",
          count: 55,
          type: "Firewall Blocked",
        },
        {
          gatewayTimeRaw: "2000/01/01 23:58:11",
          description: "FW.IPv6 FORWARD drop",
          count: 144,
          type: "Firewall Blocked",
        },
      ],
    );
    assert.equal(parsed[0].raw.Source, source[0].Source);
    assert.equal(parsed[0].raw.Target, source[0].Target);
    assert.equal(parsed[0].raw.Count, source[0].Count);
    assert.equal(typeof parsed[0].raw.Count, "string");
  });
});
