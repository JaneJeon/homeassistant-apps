import makeFetchCookie from "fetch-cookie";

export type LogKind = "system" | "event" | "firewall";
export class GatewayError extends Error {
  constructor(
    public readonly code:
      | "auth_failed"
      | "auth_backoff"
      | "http_failed"
      | "timeout"
      | "transport_failed"
      | "response_too_large",
    message = code,
  ) {
    super(message);
  }
}

const isLogin = (body: string) =>
  /<title[^>]*>\s*Login\b|<input\b[^>]*(?:name|id)=["']password["']|alert\(\s*["']Please\s+Login\s+First!["']\s*\)|location(?:\.href)?\s*=\s*["'](?:\.\.\/)*\/?index\.php/i.test(
    body,
  );
const authenticatedPage = (body: string) =>
  /<a\b[^>]*href=["']\/?home_loggedout\.php["']/i.test(body);
const readPaths = new Set([
  "/",
  "/index.php",
  "/at_a_glance.php",
  "/home_loggedout.php",
  "/network_setup.php",
  "/hardware.php",
  "/lan.php",
  "/troubleshooting_logs.php",
]);

/** Deliberately exposes only diagnostic reads and the existing sign-in flow. */
export class GatewayClient {
  private readonly jar = new makeFetchCookie.toughCookie.CookieJar();
  private readonly fetcher: (
    url: string | URL,
    init?: RequestInit,
  ) => Promise<Response>;
  private authenticated = false;
  private loginFailures = 0;
  private loginAllowedAt = 0;
  readonly origin: string;

  constructor(
    baseUrl: string,
    private readonly username: string,
    private readonly password: string,
    private readonly timeoutMs = 15_000,
    fetcher: typeof fetch = fetch,
  ) {
    const url = new URL(baseUrl);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.pathname !== "/" ||
      url.search ||
      url.hash
    )
      throw new Error("gateway_url must be an HTTP(S) origin");
    this.origin = url.origin;
    this.fetcher = makeFetchCookie(fetcher, this.jar);
  }

  private async request(
    path: string,
    body?: URLSearchParams,
    timeoutMs = this.timeoutMs,
    parentSignal?: AbortSignal,
  ): Promise<string> {
    const allowed = [
      "/",
      "/check.php",
      "/network_setup.php",
      "/hardware.php",
      "/lan.php",
      "/troubleshooting_logs.php",
      "/actionHandler/ajax_troubleshooting_logs.php",
    ];
    if (
      !allowed.includes(path) ||
      (body &&
        ![
          "/check.php",
          "/actionHandler/ajax_troubleshooting_logs.php",
        ].includes(path))
    )
      throw new Error("Endpoint not permitted");
    let url = new URL(path, this.origin);
    let method = body ? "POST" : "GET";
    const headers: Record<string, string> = {
      Accept: "text/html,application/json",
      Referer: this.origin + "/",
      "User-Agent": "xb3-telemetry/0.1",
    };
    if (body) headers["Content-Type"] = "application/x-www-form-urlencoded";
    if (path.includes("ajax_troubleshooting_logs")) {
      const csrf = (await this.jar.getCookies(this.origin)).find(
        (c) => c.key === "csrfp_token",
      );
      if (!csrf) throw new GatewayError("auth_failed");
      headers.csrfp_token = csrf.value;
      headers["X-Requested-With"] = "XMLHttpRequest";
      headers.Referer = this.origin + "/troubleshooting_logs.php";
    }
    const signal = AbortSignal.any([
      AbortSignal.timeout(timeoutMs),
      ...(parentSignal ? [parentSignal] : []),
    ]);
    try {
      for (let hop = 0; hop < 5; hop++) {
        const response = await this.fetcher(url, {
          method,
          headers,
          body: method === "POST" ? body : undefined,
          redirect: "manual",
          signal,
        });
        if ([301, 302, 303, 307, 308].includes(response.status)) {
          const location = response.headers.get("location");
          await response.body?.cancel();
          if (!location) throw new GatewayError("http_failed");
          const next = new URL(location, url);
          const nextMethod = [301, 302, 303].includes(response.status)
            ? "GET"
            : method;
          if (
            next.origin !== this.origin ||
            next.username ||
            next.password ||
            next.search ||
            next.hash
          )
            throw new GatewayError("http_failed");
          if (nextMethod === "GET" && !readPaths.has(next.pathname))
            throw new GatewayError("http_failed");
          // A 307/308 must never replay credentials or a query to another path.
          if (nextMethod === "POST" && next.pathname !== url.pathname)
            throw new GatewayError("http_failed");
          url = next;
          method = nextMethod;
          continue;
        }
        if (!response.ok) {
          await response.body?.cancel();
          throw new GatewayError(
            response.status === 401 || response.status === 403
              ? "auth_failed"
              : "http_failed",
          );
        }
        // Never persist entire authenticated HTML or include it in an error.
        const reader = response.body?.getReader();
        if (!reader) throw new GatewayError("http_failed");
        const chunks: Uint8Array[] = [];
        let bytes = 0;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          bytes += value.byteLength;
          if (bytes > 2 * 1024 * 1024) {
            await reader.cancel();
            throw new GatewayError("response_too_large");
          }
          chunks.push(value);
        }
        return Buffer.concat(chunks).toString("utf8");
      }
      throw new GatewayError("http_failed");
    } catch (error) {
      if (error instanceof GatewayError) throw error;
      throw new GatewayError(signal.aborted ? "timeout" : "transport_failed");
    }
  }

  private async login(signal?: AbortSignal): Promise<void> {
    if (Date.now() < this.loginAllowedAt)
      throw new GatewayError("auth_backoff");
    this.authenticated = false;
    await this.jar.removeAllCookies();
    try {
      await this.request("/", undefined, this.timeoutMs, signal);
      const result = await this.request(
        "/check.php",
        new URLSearchParams({
          username: this.username,
          password: this.password,
        }),
        this.timeoutMs,
        signal,
      );
      if (isLogin(result) || !authenticatedPage(result))
        throw new GatewayError("auth_failed");
      this.authenticated = true;
      this.loginFailures = 0;
      this.loginAllowedAt = 0;
    } catch (error) {
      // Protect the gateway's password lockout and avoid repeatedly submitting
      // credentials while its management backend is unavailable.
      this.loginFailures++;
      this.loginAllowedAt =
        Date.now() +
        Math.min(900_000, 60_000 * 2 ** Math.min(this.loginFailures - 1, 4));
      throw error;
    }
  }

  private async read(path: string, signal?: AbortSignal): Promise<string> {
    if (!this.authenticated) await this.login(signal);
    let result: string;
    try {
      result = await this.request(path, undefined, this.timeoutMs, signal);
    } catch (error) {
      if (!(error instanceof GatewayError) || error.code !== "auth_failed")
        throw error;
      await this.login(signal);
      return this.afterReauthentication(path, signal);
    }
    if (isLogin(result)) {
      await this.login(signal); // One bounded recovery for recognizable session loss.
      return this.afterReauthentication(path, signal);
    }
    return result;
  }

  private async afterReauthentication(
    path: string,
    signal?: AbortSignal,
  ): Promise<string> {
    try {
      const result = await this.request(
        path,
        undefined,
        this.timeoutMs,
        signal,
      );
      if (isLogin(result)) throw new GatewayError("auth_failed");
      return result;
    } catch (error) {
      if (error instanceof GatewayError && error.code === "auth_failed") {
        this.authenticated = false;
        this.loginAllowedAt = Date.now() + 60_000;
      }
      throw error;
    }
  }

  network(signal?: AbortSignal): Promise<string> {
    return this.read("/network_setup.php", signal);
  }
  hardware(signal?: AbortSignal): Promise<string> {
    return this.read("/hardware.php", signal);
  }
  lan(signal?: AbortSignal): Promise<string> {
    return this.read("/lan.php", signal);
  }

  async logs(
    kind: LogKind,
    timeFrame:
      | "Today"
      | "Yesterday"
      | "Last week"
      | "Last month"
      | "Last 90 days" = "Today",
    timeoutMs = 30_000,
  ): Promise<string> {
    await this.read("/troubleshooting_logs.php");
    const result = await this.request(
      "/actionHandler/ajax_troubleshooting_logs.php",
      new URLSearchParams({ mode: kind, timef: timeFrame }),
      timeoutMs,
    );
    // Treat JSON log text as data even if a message happens to quote a login
    // guard. Other malformed payloads remain parse failures, not bad passwords.
    if (!result.trimStart().startsWith("[") && isLogin(result)) {
      this.authenticated = false;
      throw new GatewayError("auth_failed");
    }
    return result;
  }
}
