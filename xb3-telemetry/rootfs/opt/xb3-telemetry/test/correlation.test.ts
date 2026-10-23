import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Correlator } from "../src/correlation.js";

const sec = (n: number) => new Date(n * 1000).toISOString();
function snapshot(
  sequence: number,
  started: number,
  completed: number,
  usable = true,
  startedMono = started * 1000,
  completedMono = completed * 1000,
) {
  return {
    event: "xb3_gateway_snapshot",
    collection_sequence: sequence,
    network: {
      started_at_seconds: started,
      observed_at_seconds: completed,
      started_monotonic_ms: startedMono,
      completed_monotonic_ms: completedMono,
      data: usable
        ? {
            codewords: [
              { slot: 1, unerrored: 123, correctable: 4, uncorrectable: 0 },
            ],
          }
        : null,
    },
  };
}
function probe(
  sequence: number,
  launched: number,
  completed: number,
  extra: Record<string, unknown> = {},
  launchedMono = launched * 1000,
  completedMono = completed * 1000,
) {
  return {
    event: "xb3_probe_outcome",
    startup_id: "boot-a",
    startup_seconds: 7,
    mode: "live",
    synthetic: false,
    target: "gateway",
    sequence,
    scheduled_at: sec(launched - 1),
    launched_at: sec(launched),
    completed_at: sec(completed),
    launched_monotonic_ms: launchedMono,
    completed_monotonic_ms: completedMono,
    outcome: "no_reply_by_deadline",
    wall_offset_ms: 0,
    ...extra,
  };
}
function outputCollector() {
  const records: Record<string, unknown>[] = [];
  return {
    records,
    write: (record: Record<string, unknown>) => records.push(record),
  };
}
function onlyWindow(records: Record<string, unknown>[]) {
  const windows = records.filter(
    (record) => record.event === "xb3_loss_correlation_window",
  );
  assert.equal(windows.length, 1);
  return windows[0]!;
}

describe("Correlator", () => {
  it("brackets failed probes with usable snapshots and retains intervening failed collection IDs", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(snapshot(40, 90, 95));
    correlator.observe(snapshot(41, 100, 101, false));
    correlator.observe(probe(8, 110, 111));
    correlator.observe(snapshot(42, 120, 123));

    const window = onlyWindow(records);
    assert.equal(window.status, "retrieval_bracket");
    assert.deepEqual(window.after, {
      collection_sequence: 42,
      started_at_seconds: 120,
      completed_at_seconds: 123,
      started_monotonic_ms: 120_000,
      completed_monotonic_ms: 123_000,
    });
    const item = (window.probes as Record<string, unknown>[])[0]!;
    assert.deepEqual(item.before, {
      collection_sequence: 40,
      started_at_seconds: 90,
      completed_at_seconds: 95,
      started_monotonic_ms: 90_000,
      completed_monotonic_ms: 95_000,
    });
    assert.equal(item.bracket_complete, true);
    assert.deepEqual(item.failed_gateway_sequences, [41]);
    assert.match(
      String(window.interpretation),
      /backend acquisition times and causality unknown/,
    );
  });

  it("does not queue local errors or skipped local slots", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(snapshot(1, 90, 95));
    correlator.observe(probe(1, 100, 101, { outcome: "local_error" }));
    correlator.observe(
      probe(2, 102, 102, { outcome: "skipped_local", launched_at: null }),
    );
    correlator.observe(snapshot(2, 110, 112));
    assert.equal(
      records.filter((r) => r.event === "xb3_loss_correlation_window").length,
      0,
    );
  });

  it("keeps live and validation evidence in separate output windows", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(snapshot(1, 90, 95));
    correlator.observe(
      probe(1, 100, 101, {
        mode: "live",
        validation_id: undefined,
        synthetic: false,
      }),
    );
    correlator.observe(
      probe(2, 100, 101, {
        mode: "validation",
        validation_id: "check-1",
        synthetic: true,
      }),
    );
    correlator.observe(snapshot(2, 110, 112));
    const windows = records.filter(
      (r) => r.event === "xb3_loss_correlation_window",
    );
    assert.equal(windows.length, 2);
    assert.deepEqual(
      windows.map((w) => [w.mode, w.validation_id]),
      [
        ["live", null],
        ["validation", "check-1"],
      ],
    );
    assert.deepEqual(
      windows.map((w) => (w.probes as unknown[]).length),
      [1, 1],
    );
  });

  it("emits an incomplete bracket explicitly when no usable snapshot precedes the probe", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(probe(1, 100, 101));
    correlator.observe(snapshot(9, 110, 112));
    const item = (onlyWindow(records).probes as Record<string, unknown>[])[0]!;
    assert.equal(item.before, null);
    assert.equal(item.bracket_complete, false);
  });

  it("selects the nearest-before usable snapshot when its event arrives after the probe", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(snapshot(10, 90, 91));
    correlator.observe(probe(5, 102, 104));
    // This request completed before launch, but auxiliary reads delayed its event record.
    correlator.observe(snapshot(11, 100, 101));
    correlator.observe(snapshot(12, 105, 106));
    const window = onlyWindow(records);
    const item = (window.probes as Record<string, unknown>[])[0]!;
    assert.equal(
      (item.before as Record<string, unknown>).collection_sequence,
      11,
    );
    assert.equal(
      (window.after as Record<string, unknown>).collection_sequence,
      12,
    );
  });

  it("uses monotonic bounds when wall time steps backward during the probe", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(snapshot(1, 90, 91, true, 90_000, 91_000));
    // Launch at wall 100s / monotonic 100000ms; completion is wall 41s / monotonic 101000ms.
    correlator.observe(
      probe(
        7,
        100,
        41,
        { completion_wall_offset_ms: -59_000 },
        100_000,
        101_000,
      ),
    );
    // A subsequent request starts and completes after the probe, despite lower UTC timestamps.
    correlator.observe(snapshot(2, 42, 43, true, 102_000, 103_000));
    const window = onlyWindow(records);
    assert.equal(window.status, "retrieval_bracket");
    const item = (window.probes as Record<string, unknown>[])[0]!;
    assert.equal(
      (item.before as Record<string, unknown>).collection_sequence,
      1,
    );
    assert.equal(
      (window.after as Record<string, unknown>).collection_sequence,
      2,
    );
    assert.equal(item.bracket_complete, true);
    assert.equal(item.wall_clock_uncertain, true);
  });

  it("does not claim a complete bracket when monotonic bounds are missing", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    const missingBounds = probe(1, 100, 101);
    delete missingBounds.launched_monotonic_ms;
    delete missingBounds.completed_monotonic_ms;
    correlator.observe(snapshot(1, 90, 95));
    correlator.observe(missingBounds);
    correlator.observe(snapshot(2, 110, 112));
    assert.equal(
      records.some(
        (r) =>
          r.event === "xb3_loss_correlation_window" &&
          (r.probes as Record<string, unknown>[]).some(
            (p) => p.bracket_complete === true,
          ),
      ),
      false,
    );
  });

  it("retains a failed gateway sequence and attaches a later usable snapshot", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(snapshot(20, 90, 95));
    correlator.observe(probe(6, 100, 101));
    correlator.observe(snapshot(21, 105, 107, false));
    correlator.observe(snapshot(22, 110, 112));
    const item = (onlyWindow(records).probes as Record<string, unknown>[])[0]!;
    assert.equal(item.bracket_complete, true);
    assert.deepEqual(item.failed_gateway_sequences, [21]);
  });

  it("expires pending evidence at the 120 second monotonic deadline", () => {
    const { records, write } = outputCollector();
    let now = 0;
    const correlator = new Correlator(write, () => now, 120_000);
    correlator.observe(snapshot(1, 90, 95));
    correlator.observe(probe(1, 100, 101));
    now = 119_999;
    correlator.expire();
    assert.equal(records.length, 0);
    now = 120_000;
    correlator.expire();
    const window = onlyWindow(records);
    assert.equal(window.status, "no_usable_after_within_deadline");
    const item = (window.probes as Record<string, unknown>[])[0]!;
    assert.equal(item.bracket_complete, false);
    assert.equal(
      (item.before as Record<string, unknown>).collection_sequence,
      1,
    );
  });

  it("flags wall-clock uncertainty while still correlating by monotonic bounds", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(snapshot(1, 90, 95));
    correlator.observe(probe(1, 100, 101, { wall_offset_ms: 251 }));
    correlator.observe(snapshot(2, 110, 112));
    correlator.observe(probe(2, 120, 121, { wall_offset_ms: 300 }));
    correlator.observe(snapshot(3, 130, 132));
    const windows = records.filter(
      (r) => r.event === "xb3_loss_correlation_window",
    );
    assert.equal(windows.length, 2);
    for (const window of windows) {
      assert.equal(window.status, "retrieval_bracket");
      const item = (window.probes as Record<string, unknown>[])[0]!;
      assert.equal(item.bracket_complete, true);
      assert.equal(item.wall_clock_uncertain, true);
    }
  });

  it("does not let a post-expiry usable snapshot rescue a completed no-after window", () => {
    const { records, write } = outputCollector();
    let now = 0;
    const correlator = new Correlator(write, () => now, 120_000);
    correlator.observe(snapshot(1, 90, 95));
    correlator.observe(probe(1, 100, 101));
    now = 120_000;
    correlator.expire();
    correlator.observe(snapshot(2, 110, 112));
    assert.equal(
      records.filter((r) => r.event === "xb3_loss_correlation_window").length,
      1,
    );
    assert.equal(onlyWindow(records).status, "no_usable_after_within_deadline");
  });

  it("links retrieval sequence metadata without copying counter payloads or claiming deltas", () => {
    const { records, write } = outputCollector();
    const correlator = new Correlator(write);
    correlator.observe(snapshot(1, 90, 95));
    correlator.observe(probe(1, 100, 101));
    correlator.observe(snapshot(2, 110, 112));
    const window = onlyWindow(records);
    const encoded = JSON.stringify(window);
    assert.match(encoded, /collection_sequence/);
    assert.doesNotMatch(
      encoded,
      /unerrored|correctable|uncorrectable|123|caused_by|delta/i,
    );
    assert.match(String(window.interpretation), /retrieval bracket only/);
  });
});
