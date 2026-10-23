import type { Collection, PageResult } from "./collector.js";
import { Collector } from "./collector.js";

export interface SamplerClock {
  nowMonotonicMs(): number;
  nowWallSeconds(): number;
  sleepUntil(deadlineMs: number, stop: AbortSignal): Promise<void>;
}

export interface GatewaySamplerOptions {
  networkIntervalMs?: number;
  auxiliaryIntervalMs?: number;
  startupId: string;
  startupSeconds: number;
  onObservation?: (collection: Collection, sequence: number) => void;
  write?: (event: Record<string, unknown>) => void;
  clock?: SamplerClock;
}

export interface GatewaySamplerState {
  collection: Collection | null;
  sequence: number;
  completedSequence: number;
  pageSequences: { network: number; hardware: number; lan: number };
  inProgress: boolean;
  lastStartedSeconds: number;
  lastCompletedSeconds: number;
  skippedTicks: number;
  cooldownUntilSeconds: number;
}

const NETWORK_INTERVAL_MS = 20_000;
const AUXILIARY_INTERVAL_MS = 60_000;
const AMBIGUOUS_FAILURE_COOLDOWN_MS = 60_000;

const systemClock: SamplerClock = {
  nowMonotonicMs: () => performance.now(),
  nowWallSeconds: () => Date.now() / 1000,
  sleepUntil(deadlineMs, stop) {
    return new Promise((resolve) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const done = () => {
        if (timer !== undefined) clearTimeout(timer);
        stop.removeEventListener("abort", done);
        resolve();
      };
      if (stop.aborted) {
        done();
        return;
      }
      timer = setTimeout(done, Math.max(0, deadlineMs - performance.now()));
      stop.addEventListener("abort", done, { once: true });
    });
  },
};

function validateInterval(name: string, value: number): number {
  if (!Number.isFinite(value) || value <= 0)
    throw new Error(`${name} must be a positive finite number`);
  return value;
}

function snapshotPage<T>(result: PageResult<T>): Record<string, unknown> {
  return {
    data: result.data,
    error: result.error,
    started_at_seconds: result.startedAtSeconds ?? null,
    observed_at_seconds: result.observedAtSeconds,
    duration_seconds: result.durationSeconds,
    started_monotonic_ms: result.startedAtMonotonicMs ?? null,
    completed_monotonic_ms: result.observedAtMonotonicMs ?? null,
  };
}

function mergePage<T>(
  previous: PageResult<T> | undefined,
  current: PageResult<T>,
): PageResult<T> {
  if (current.error === "not_scheduled" && previous) return previous;
  return current;
}

function mergeCollection(
  previous: Collection | null,
  current: Collection,
): Collection {
  return {
    network: current.network,
    hardware: mergePage(previous?.hardware, current.hardware),
    lan: mergePage(previous?.lan, current.lan),
    durationSeconds: current.durationSeconds,
  };
}

export class GatewaySampler {
  private readonly networkIntervalMs: number;
  private readonly auxiliaryIntervalMs: number;
  private readonly startupId: string;
  private readonly startupSeconds: number;
  private readonly onObservation?: GatewaySamplerOptions["onObservation"];
  private readonly write: NonNullable<GatewaySamplerOptions["write"]>;
  private readonly clock: SamplerClock;
  private runPromise?: Promise<void>;
  private cooldownUntilMonotonicMs = 0;
  private currentState: GatewaySamplerState = {
    collection: null,
    sequence: 0,
    completedSequence: 0,
    pageSequences: { network: 0, hardware: 0, lan: 0 },
    inProgress: false,
    lastStartedSeconds: 0,
    lastCompletedSeconds: 0,
    skippedTicks: 0,
    cooldownUntilSeconds: 0,
  };

  constructor(
    private readonly collector: Collector,
    options: GatewaySamplerOptions,
  ) {
    this.networkIntervalMs = validateInterval(
      "networkIntervalMs",
      options.networkIntervalMs ?? NETWORK_INTERVAL_MS,
    );
    this.auxiliaryIntervalMs = validateInterval(
      "auxiliaryIntervalMs",
      options.auxiliaryIntervalMs ?? AUXILIARY_INTERVAL_MS,
    );
    this.startupId = options.startupId;
    this.startupSeconds = options.startupSeconds;
    this.onObservation = options.onObservation;
    this.write =
      options.write ?? ((event) => console.log(JSON.stringify(event)));
    this.clock = options.clock ?? systemClock;
  }

  get state(): GatewaySamplerState {
    return { ...this.currentState };
  }

  run(stop: AbortSignal): Promise<void> {
    if (!this.runPromise)
      this.runPromise = this.loop(stop).finally(() => {
        this.runPromise = undefined;
      });
    return this.runPromise;
  }

  private async loop(stop: AbortSignal): Promise<void> {
    const startMonotonicMs = this.clock.nowMonotonicMs();
    let nextNetworkDeadlineMs = startMonotonicMs;
    let nextAuxiliaryDeadlineMs = startMonotonicMs;
    const nextAuxiliaryAfter = (deadlineMs: number) =>
      startMonotonicMs +
      (Math.floor((deadlineMs - startMonotonicMs) / this.auxiliaryIntervalMs) +
        1) *
        this.auxiliaryIntervalMs;

    while (!stop.aborted) {
      try {
        await this.clock.sleepUntil(nextNetworkDeadlineMs, stop);
      } catch (error) {
        if (stop.aborted) return;
        throw error;
      }
      if (stop.aborted) return;

      const scheduledAtMs = nextNetworkDeadlineMs;
      const nowMonotonicMs = this.clock.nowMonotonicMs();
      if (nowMonotonicMs - scheduledAtMs >= this.networkIntervalMs) {
        while (nextNetworkDeadlineMs <= nowMonotonicMs) {
          const missedAtMs = nextNetworkDeadlineMs;
          if (missedAtMs >= nextAuxiliaryDeadlineMs)
            nextAuxiliaryDeadlineMs = nextAuxiliaryAfter(missedAtMs);
          this.recordSkipped(missedAtMs, this.skipReason(missedAtMs));
          nextNetworkDeadlineMs += this.networkIntervalMs;
        }
        continue;
      }

      const auxiliaryDue = scheduledAtMs >= nextAuxiliaryDeadlineMs;
      if (auxiliaryDue)
        nextAuxiliaryDeadlineMs = nextAuxiliaryAfter(scheduledAtMs);

      if (nowMonotonicMs < this.cooldownUntilMonotonicMs) {
        this.recordSkipped(scheduledAtMs, "cooldown");
        nextNetworkDeadlineMs += this.networkIntervalMs;
        continue;
      }

      const scheduledAtSeconds = this.wallAtMonotonic(scheduledAtMs);
      const sequence = this.currentState.sequence + 1;
      const startedAtSeconds = this.clock.nowWallSeconds();
      this.currentState = {
        ...this.currentState,
        sequence,
        inProgress: true,
        lastStartedSeconds: startedAtSeconds,
      };

      let raw: Collection;
      try {
        raw = await this.collector.collect({ auxiliary: auxiliaryDue });
      } finally {
        this.currentState = { ...this.currentState, inProgress: false };
      }

      const completedAtSeconds = this.clock.nowWallSeconds();
      const completedAtMonotonicMs = this.clock.nowMonotonicMs();
      const composed = mergeCollection(this.currentState.collection, raw);
      this.currentState = {
        ...this.currentState,
        collection: composed,
        completedSequence: sequence,
        pageSequences: {
          network: sequence,
          hardware:
            raw.hardware.error === "not_scheduled"
              ? this.currentState.pageSequences.hardware
              : sequence,
          lan:
            raw.lan.error === "not_scheduled"
              ? this.currentState.pageSequences.lan
              : sequence,
        },
        lastCompletedSeconds: completedAtSeconds,
      };

      this.write({
        event: "xb3_gateway_snapshot",
        startup_id: this.startupId,
        startup_seconds: this.startupSeconds,
        collection_sequence: sequence,
        scheduled_at: scheduledAtSeconds,
        started_at: startedAtSeconds,
        completed_at: completedAtSeconds,
        network: snapshotPage(raw.network),
        hardware: snapshotPage(raw.hardware),
        lan: snapshotPage(raw.lan),
      });
      try {
        this.onObservation?.(composed, sequence);
      } catch {
        this.write({
          event: "xb3_gateway_observation_callback_failed",
          startup_id: this.startupId,
          startup_seconds: this.startupSeconds,
          collection_sequence: sequence,
          observed_at: completedAtSeconds,
        });
      }

      const failedPages = [
        ["network", raw.network],
        ["hardware", raw.hardware],
        ["lan", raw.lan],
      ].filter(
        ([, page]) =>
          page &&
          (page as PageResult<unknown>).error !== null &&
          ["timeout", "transport_failed"].includes(
            (page as PageResult<unknown>).error ?? "",
          ),
      ) as Array<[string, PageResult<unknown>]>;
      if (failedPages.length > 0) {
        this.cooldownUntilMonotonicMs = Math.max(
          this.cooldownUntilMonotonicMs,
          completedAtMonotonicMs + AMBIGUOUS_FAILURE_COOLDOWN_MS,
        );
        this.currentState = {
          ...this.currentState,
          cooldownUntilSeconds:
            completedAtSeconds + AMBIGUOUS_FAILURE_COOLDOWN_MS / 1000,
        };
        this.write({
          event: "xb3_gateway_collection_cooldown",
          startup_id: this.startupId,
          startup_seconds: this.startupSeconds,
          collection_sequence: sequence,
          started_at: startedAtSeconds,
          completed_at: completedAtSeconds,
          cooldown_until: this.currentState.cooldownUntilSeconds,
          failed_pages: failedPages.map(([page, result]) => ({
            page,
            reason: result.error,
          })),
        });
      }

      nextNetworkDeadlineMs = scheduledAtMs + this.networkIntervalMs;
      while (nextNetworkDeadlineMs <= completedAtMonotonicMs) {
        const missedAtMs = nextNetworkDeadlineMs;
        if (missedAtMs >= nextAuxiliaryDeadlineMs)
          nextAuxiliaryDeadlineMs = nextAuxiliaryAfter(missedAtMs);
        this.recordSkipped(missedAtMs, "missed_deadline");
        nextNetworkDeadlineMs += this.networkIntervalMs;
      }
    }
  }

  private skipReason(deadlineMs: number): "cooldown" | "missed_deadline" {
    return deadlineMs < this.cooldownUntilMonotonicMs
      ? "cooldown"
      : "missed_deadline";
  }

  private wallAtMonotonic(deadlineMs: number): number {
    return (
      this.clock.nowWallSeconds() +
      (deadlineMs - this.clock.nowMonotonicMs()) / 1000
    );
  }

  private recordSkipped(
    scheduledAtMs: number,
    reason: "cooldown" | "missed_deadline",
  ): void {
    this.currentState = {
      ...this.currentState,
      skippedTicks: this.currentState.skippedTicks + 1,
    };
    this.write({
      event: "xb3_gateway_schedule_skipped",
      startup_id: this.startupId,
      startup_seconds: this.startupSeconds,
      scheduled_at: this.wallAtMonotonic(scheduledAtMs),
      reason,
      skipped_ticks: this.currentState.skippedTicks,
      cooldown_until: this.currentState.cooldownUntilSeconds,
    });
  }
}
