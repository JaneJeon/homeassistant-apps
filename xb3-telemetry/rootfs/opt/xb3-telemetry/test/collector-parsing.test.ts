import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Collector } from "../src/collector.js";
import type {
  HardwareSnapshot,
  LanSnapshot,
  NetworkSnapshot,
} from "../src/types.js";

const page = "<html>captured response</html>";
const emptyParsers = {
  network: (_html: string) => ({}) as NetworkSnapshot,
  hardware: (_html: string) => ({}) as HardwareSnapshot,
  lan: (_html: string) => ({}) as LanSnapshot,
};

describe("Collector parser timing", () => {
  it("captures HTTP completion before an asynchronous parser finishes", async () => {
    let releaseParser!: () => void;
    const parserGate = new Promise<void>((resolve) => {
      releaseParser = resolve;
    });
    let parserEntered!: () => void;
    const enteredParser = new Promise<void>((resolve) => {
      parserEntered = resolve;
    });
    let parserStartedAtSeconds = 0;
    let fetched = false;
    const client = {
      network: async () => {
        fetched = true;
        return page;
      },
      hardware: async () => page,
      lan: async () => page,
    };
    const collector = new Collector(client, {
      ...emptyParsers,
      network: async (html) => {
        assert.equal(html, page);
        assert.equal(fetched, true);
        parserStartedAtSeconds = Date.now() / 1000;
        parserEntered();
        await parserGate;
        return {} as NetworkSnapshot;
      },
    });

    const pending = collector.collect({ auxiliary: false });
    await enteredParser;
    const releasedAtSeconds = Date.now() / 1000;
    releaseParser();
    const collection = await pending;
    const result = collection.network;
    assert.ok(result.data);
    assert.ok(result.observedAtSeconds <= parserStartedAtSeconds);
    assert.ok(result.observedAtSeconds <= releasedAtSeconds);
    assert.ok(result.startedAtMonotonicMs !== undefined);
    assert.ok(result.observedAtMonotonicMs !== undefined);
    assert.ok(result.parseDurationSeconds !== undefined);
    assert.ok(result.durationSeconds >= result.parseDurationSeconds);
    assert.ok(collection.durationSeconds >= result.durationSeconds);
    assert.equal(result.error, null);
    assert.equal(collection.hardware.error, "not_scheduled");
    assert.equal(collection.lan.error, "not_scheduled");
  });

  it("preserves response timing bounds when parsing fails", async () => {
    const collector = new Collector(
      {
        network: async () => page,
        hardware: async () => page,
        lan: async () => page,
      },
      {
        ...emptyParsers,
        network: async () => {
          throw new Error("malformed captured page");
        },
      },
    );
    const collection = await collector.collect({ auxiliary: false });
    const result = collection.network;
    assert.equal(result.data, null);
    assert.equal(result.error, "parse_failed");
    assert.ok(result.startedAtSeconds !== undefined);
    assert.ok(result.observedAtSeconds >= result.startedAtSeconds);
    assert.ok(result.startedAtMonotonicMs !== undefined);
    assert.ok(result.observedAtMonotonicMs !== undefined);
    assert.ok(result.parseDurationSeconds !== undefined);
    assert.ok(result.durationSeconds >= result.parseDurationSeconds);
    assert.notEqual(result.error, "timeout");
  });
});
