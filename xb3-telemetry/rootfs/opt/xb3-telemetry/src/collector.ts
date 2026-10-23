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
  error: string | null;
}
export interface Collection {
  network: PageResult<NetworkSnapshot>;
  hardware: PageResult<HardwareSnapshot>;
  lan: PageResult<LanSnapshot>;
  durationSeconds: number;
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
  collect(): Promise<Collection> {
    if (!this.inFlight)
      this.inFlight = this.run().finally(() => {
        this.inFlight = undefined;
      });
    return this.inFlight;
  }

  private async page<T>(
    fetchPage: () => Promise<string>,
    parse: (html: string) => T,
  ): Promise<PageResult<T>> {
    const start = performance.now();
    try {
      const data = parse(await fetchPage());
      return {
        data,
        durationSeconds: (performance.now() - start) / 1000,
        observedAtSeconds: Date.now() / 1000,
        error: null,
      };
    } catch (error) {
      return {
        data: null,
        durationSeconds: (performance.now() - start) / 1000,
        observedAtSeconds: Date.now() / 1000,
        error: error instanceof GatewayError ? error.code : "parse_failed",
      };
    }
  }

  private async run(): Promise<Collection> {
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
    const skipped = <T>(): PageResult<T> => ({
      data: null,
      durationSeconds: 0,
      observedAtSeconds: Date.now() / 1000,
      error: "skipped_after_ambiguous_failure",
    });
    // Client cancellation does not establish server completion. Sacrifice the
    // remaining observations in this scrape rather than queueing more work
    // in the same PHP session. The next ordinary scrape can try again.
    const hardware = ambiguous(network)
      ? skipped<HardwareSnapshot>()
      : await this.page(() => this.client.hardware(signal), parseHardware);
    const lan =
      ambiguous(network) || ambiguous(hardware)
        ? skipped<LanSnapshot>()
        : await this.page(() => this.client.lan(signal), parseLan);
    return {
      network,
      hardware,
      lan,
      durationSeconds: (performance.now() - start) / 1000,
    };
  }
}
