import { GatewayClient, GatewayError } from "./client.js";
import { parseHardware, parseLan, parseNetwork } from "./parser.js";
import type {
  HardwareSnapshot,
  LanSnapshot,
  NetworkSnapshot,
} from "./types.js";

export interface PageResult<T> {
  data: T | null;
  durationSeconds: number;
  observedAtSeconds: number;
  startedAtSeconds?: number;
  startedAtMonotonicMs?: number;
  observedAtMonotonicMs?: number;
  error: string | null;
}
export interface Collection {
  network: PageResult<NetworkSnapshot>;
  hardware: PageResult<HardwareSnapshot>;
  lan: PageResult<LanSnapshot>;
  durationSeconds: number;
}

export interface CollectOptions {
  /** Whether to fetch hardware and LAN pages during this sequence. Defaults true. */
  auxiliary?: boolean;
}

export class Collector {
  private inFlight?: Promise<Collection>;
  constructor(
    private readonly client: Pick<
      GatewayClient,
      "network" | "hardware" | "lan"
    >,
  ) {}

  /** Coalesce concurrent Prometheus scrapes. Only one passive read sequence runs. */
  collect(options: CollectOptions = {}): Promise<Collection> {
    if (!this.inFlight)
      this.inFlight = this.run(options.auxiliary !== false).finally(() => {
        this.inFlight = undefined;
      });
    return this.inFlight;
  }

  private async page<T>(
    fetchPage: () => Promise<string>,
    parse: (html: string) => T,
  ): Promise<PageResult<T>> {
    const start = performance.now();
    const startedAtSeconds = Date.now() / 1000;
    try {
      const data = parse(await fetchPage());
      return {
        data,
        durationSeconds: (performance.now() - start) / 1000,
        observedAtSeconds: Date.now() / 1000,
        startedAtSeconds,
        startedAtMonotonicMs: start,
        observedAtMonotonicMs: performance.now(),
        error: null,
      };
    } catch (error) {
      return {
        data: null,
        durationSeconds: (performance.now() - start) / 1000,
        observedAtSeconds: Date.now() / 1000,
        startedAtSeconds,
        startedAtMonotonicMs: start,
        observedAtMonotonicMs: performance.now(),
        error: error instanceof GatewayError ? error.code : "parse_failed",
      };
    }
  }

  private notScheduled<T>(): PageResult<T> {
    return {
      data: null,
      durationSeconds: 0,
      observedAtSeconds: 0,
      error: "not_scheduled",
    };
  }

  private skippedAfterAmbiguousFailure<T>(): PageResult<T> {
    return {
      data: null,
      durationSeconds: 0,
      observedAtSeconds: 0,
      error: "skipped_after_ambiguous_failure",
    };
  }

  private async run(auxiliary: boolean): Promise<Collection> {
    const start = performance.now();
    const signal = AbortSignal.timeout(45_000);
    // Same-session PHP requests can serialize. Avoid competing requests and
    // preserve each page's own result instead of failing the whole scrape.
    const network = await this.page(
      () => this.client.network(signal),
      parseNetwork,
    );
    const ambiguous = (result: PageResult<unknown>) =>
      result.error === "timeout" || result.error === "transport_failed";
    // Client cancellation does not establish server completion. Sacrifice the
    // remaining observations in this scrape rather than queueing more work
    // in the same PHP session. The next ordinary scrape can try again.
    const hardware = !auxiliary
      ? this.notScheduled<HardwareSnapshot>()
      : ambiguous(network)
        ? this.skippedAfterAmbiguousFailure<HardwareSnapshot>()
        : await this.page(() => this.client.hardware(signal), parseHardware);
    const lan = !auxiliary
      ? this.notScheduled<LanSnapshot>()
      : ambiguous(network) || ambiguous(hardware)
        ? this.skippedAfterAmbiguousFailure<LanSnapshot>()
        : await this.page(() => this.client.lan(signal), parseLan);
    return {
      network,
      hardware,
      lan,
      durationSeconds: (performance.now() - start) / 1000,
    };
  }
}
