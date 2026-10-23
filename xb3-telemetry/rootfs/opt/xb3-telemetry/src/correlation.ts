type Event = Record<string, unknown>;
interface Snapshot {
  sequence: number;
  started: number;
  completed: number;
  startedMono: number;
  completedMono: number;
  usable: boolean;
}
interface Pending {
  record: Event;
  before: Snapshot | null;
  queuedMono: number;
}

/** Retrieval brackets are evidence links, not backend acquisition times or causality. */
export class Correlator {
  private readonly history: Snapshot[] = [];
  private pending: Pending[] = [];
  constructor(
    private readonly write: (record: Event) => void,
    private readonly nowMonotonicMs = () => performance.now(),
    private readonly maxWaitMs = 120_000,
  ) {}
  observe(record: Event): void {
    if (record.event === "xb3_gateway_snapshot") {
      const page = record.network as
        | {
            started_at_seconds?: number;
            observed_at_seconds?: number;
            started_monotonic_ms?: number;
            completed_monotonic_ms?: number;
            data?: { codewords?: unknown };
          }
        | undefined;
      if (
        !page ||
        typeof page.started_at_seconds !== "number" ||
        typeof page.observed_at_seconds !== "number" ||
        typeof page.started_monotonic_ms !== "number" ||
        typeof page.completed_monotonic_ms !== "number" ||
        typeof record.collection_sequence !== "number"
      )
        return;
      const snapshot: Snapshot = {
        sequence: record.collection_sequence,
        started: page.started_at_seconds,
        completed: page.observed_at_seconds,
        startedMono: page.started_monotonic_ms,
        completedMono: page.completed_monotonic_ms,
        usable: Array.isArray(page.data?.codewords),
      };
      this.history.push(snapshot);
      if (this.history.length > 32) this.history.shift();
      if (snapshot.usable) {
        const ready = this.pending.filter(
          (p) =>
            this.nowMonotonicMs() - p.queuedMono < this.maxWaitMs &&
            typeof p.record.completed_monotonic_ms === "number" &&
            p.record.completed_monotonic_ms <= snapshot.startedMono,
        );
        this.pending = this.pending.filter((p) => !ready.includes(p));
        this.emitGroups(ready, snapshot, "retrieval_bracket");
      }
    } else if (
      record.event === "xb3_probe_outcome" &&
      record.outcome === "no_reply_by_deadline"
    ) {
      if (
        typeof record.launched_at !== "string" ||
        typeof record.completed_at !== "string"
      )
        return;
      const launched = record.launched_monotonic_ms;
      if (
        typeof launched !== "number" ||
        !Number.isFinite(launched) ||
        typeof record.completed_monotonic_ms !== "number" ||
        !Number.isFinite(record.completed_monotonic_ms) ||
        !Number.isFinite(Date.parse(record.completed_at))
      )
        return;
      const before =
        [...this.history]
          .reverse()
          .find((x) => x.usable && x.completedMono <= launched) ?? null;
      const pending = { record, before, queuedMono: this.nowMonotonicMs() };
      this.pending.push(pending);
      if (this.pending.length > 1000)
        this.emitGroups([this.pending.shift()!], null, "pending_limit");
    }
    this.expire();
  }
  expire(): void {
    const now = this.nowMonotonicMs();
    const expired = this.pending.filter(
      (p) => now - p.queuedMono >= this.maxWaitMs,
    );
    this.pending = this.pending.filter(
      (p) => now - p.queuedMono < this.maxWaitMs,
    );
    this.emitGroups(expired, null, "no_usable_after_within_deadline");
  }
  close(): void {
    const pending = this.pending;
    this.pending = [];
    this.emitGroups(pending, null, "process_stopping");
  }
  private emitGroups(
    items: Pending[],
    after: Snapshot | null,
    status: string,
  ): void {
    const groups = new Map<string, Pending[]>();
    for (const item of items) {
      const key = JSON.stringify([
        item.record.mode,
        item.record.validation_id ?? null,
      ]);
      const group = groups.get(key) ?? [];
      group.push(item);
      groups.set(key, group);
    }
    for (const group of groups.values()) {
      const sample = group[0]!.record;
      // Auxiliary reads can delay snapshot publication. Choose the nearest
      // completed-before retrieval now that those delayed records are known.
      for (const p of group) {
        const launched = Number(p.record.launched_monotonic_ms);
        const nearest = [...this.history]
          .reverse()
          .find((s) => s.usable && s.completedMono <= launched);
        if (
          nearest &&
          (!p.before || nearest.completedMono > p.before.completedMono)
        )
          p.before = nearest;
      }
      this.write({
        event: "xb3_loss_correlation_window",
        startup_id: sample.startup_id,
        startup_seconds: sample.startup_seconds,
        mode: sample.mode,
        synthetic: sample.synthetic,
        validation_id: sample.validation_id ?? null,
        status,
        interpretation:
          "HTTP retrieval bracket only; backend acquisition times and causality unknown",
        after: after
          ? {
              collection_sequence: after.sequence,
              started_at_seconds: after.started,
              completed_at_seconds: after.completed,
              started_monotonic_ms: after.startedMono,
              completed_monotonic_ms: after.completedMono,
            }
          : null,
        probes: group.map((p) => ({
          target: p.record.target,
          sequence: p.record.sequence,
          scheduled_at: p.record.scheduled_at,
          launched_at: p.record.launched_at,
          completed_at: p.record.completed_at,
          launched_monotonic_ms: p.record.launched_monotonic_ms,
          completed_monotonic_ms: p.record.completed_monotonic_ms,
          wall_clock_uncertain: [
            p.record.wall_offset_ms,
            p.record.completion_wall_offset_ms,
          ].some((v) => typeof v === "number" && Math.abs(v) > 250),
          before: p.before
            ? {
                collection_sequence: p.before.sequence,
                started_at_seconds: p.before.started,
                completed_at_seconds: p.before.completed,
                started_monotonic_ms: p.before.startedMono,
                completed_monotonic_ms: p.before.completedMono,
              }
            : null,
          bracket_complete: after !== null && p.before !== null,
          failed_gateway_sequences: this.history
            .filter(
              (s) =>
                !s.usable &&
                s.completedMono >= (p.before?.completedMono ?? 0) &&
                s.startedMono <= (after?.startedMono ?? Infinity),
            )
            .map((s) => s.sequence),
        })),
      });
    }
  }
}
