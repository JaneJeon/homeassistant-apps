import { setTimeout as delay } from "node:timers/promises";

export type PortalGateOwner = "metrics" | "logs";

export interface PortalGateLease {
  readonly owner: PortalGateOwner;
  release(): void;
}

export interface PortalGateState {
  owner: PortalGateOwner | null;
  cooldownUntilSeconds: number;
  cooldownRemainingSeconds: number;
  logsWaiting: boolean;
}

export interface PortalGateClock {
  monotonicMs(): number;
  wallMs(): number;
  sleep(ms: number, stop?: AbortSignal): Promise<void>;
}

export interface PortalGateOptions {
  clock?: PortalGateClock;
}

const realClock: PortalGateClock = {
  monotonicMs: () => performance.now(),
  wallMs: () => Date.now(),
  sleep: async (ms, stop) => {
    await delay(ms, undefined, stop ? { signal: stop } : undefined);
  },
};

interface Waiter {
  readonly id: symbol;
}

/** Serializes the gateway's shared PHP session and preserves a cooldown across callers. */
export class PortalGate {
  private readonly clock: PortalGateClock;
  private activeOwner: PortalGateOwner | null = null;
  private activeToken: symbol | null = null;
  private cooldownDeadline = 0;
  private readonly waiters: Waiter[] = [];

  constructor(options: PortalGateOptions = {}) {
    this.clock = options.clock ?? realClock;
  }

  private remainingMs(): number {
    return Math.max(0, this.cooldownDeadline - this.clock.monotonicMs());
  }

  get state(): PortalGateState {
    const remaining = this.remainingMs();
    return {
      owner: this.activeOwner,
      cooldownUntilSeconds:
        remaining > 0 ? (this.clock.wallMs() + remaining) / 1000 : 0,
      cooldownRemainingSeconds: remaining / 1000,
      logsWaiting: this.waiters.length > 0,
    };
  }

  tryAcquireMetrics(): PortalGateLease | null {
    if (
      this.activeOwner !== null ||
      this.waiters.length > 0 ||
      this.remainingMs() > 0
    )
      return null;
    return this.acquire("metrics");
  }

  async acquireLogs(stop?: AbortSignal): Promise<PortalGateLease | null> {
    if (stop?.aborted) return null;
    if (
      this.activeOwner === null &&
      this.waiters.length === 0 &&
      this.remainingMs() === 0
    )
      return this.acquire("logs");

    const waiter: Waiter = { id: Symbol("portal-log-waiter") };
    this.waiters.push(waiter);
    while (!stop?.aborted) {
      if (
        this.waiters[0]?.id === waiter.id &&
        this.activeOwner === null &&
        this.remainingMs() === 0
      ) {
        this.waiters.shift();
        return this.acquire("logs");
      }
      const waitMs = Math.max(1, Math.min(1000, this.remainingMs() || 1000));
      try {
        await this.clock.sleep(waitMs, stop);
      } catch (error) {
        if (stop?.aborted) break;
        throw error;
      }
    }
    const index = this.waiters.findIndex((queued) => queued.id === waiter.id);
    if (index >= 0) this.waiters.splice(index, 1);
    return null;
  }

  cooldown(ms = 60_000): void {
    if (!Number.isFinite(ms) || ms < 0)
      throw new RangeError("Cooldown must be a finite non-negative duration");
    this.cooldownDeadline = Math.max(
      this.cooldownDeadline,
      this.clock.monotonicMs() + ms,
    );
  }

  private acquire(owner: PortalGateOwner): PortalGateLease {
    const token = Symbol(`portal-${owner}-lease`);
    this.activeOwner = owner;
    this.activeToken = token;
    let released = false;
    return {
      owner,
      release: () => {
        if (released) return;
        released = true;
        if (this.activeToken !== token) return;
        this.activeOwner = null;
        this.activeToken = null;
      },
    };
  }
}
