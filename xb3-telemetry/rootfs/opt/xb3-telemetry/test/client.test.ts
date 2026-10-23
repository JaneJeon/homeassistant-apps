import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { GatewayClient, GatewayError } from "../src/client.js";

type SeenRequest = {
  path: string;
  method: string;
  headers: Headers;
  body: string;
};

function response(
  body = "",
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(body, { status, headers });
}

function responseAt(
  url: string | URL,
  body = "",
  status = 200,
  headers: Record<string, string> = {},
): Response {
  const result = response(body, status, headers);
  Object.defineProperty(result, "url", { value: String(url) });
  return result;
}

function seen(url: string | URL, init?: RequestInit): SeenRequest {
  const body =
    init?.body instanceof URLSearchParams
      ? init.body.toString()
      : typeof init?.body === "string"
        ? init.body
        : "";
  return {
    path: new URL(String(url)).pathname,
    method: init?.method ?? "GET",
    headers: new Headers(init?.headers),
    body,
  };
}

function successfulLoginFetcher(requests: SeenRequest[]) {
  return async (url: string | URL, init?: RequestInit): Promise<Response> => {
    const request = seen(url, init);
    requests.push(request);
    if (request.path === "/")
      return responseAt(url, "<html>router sign in</html>", 200, {
        "set-cookie": "csrfp_token=csrf-value; Path=/",
      });
    if (request.path === "/check.php" && request.method === "POST") {
      return responseAt(url, "", 302, {
        location: "/home_loggedout.php",
        "set-cookie": "router_session=authenticated; Path=/",
      });
    }
    if (request.path === "/home_loggedout.php")
      return response('<a href="home_loggedout.php">Logout</a>');
    if (request.path === "/network_setup.php")
      return response("<html>captured network page</html>");
    if (request.path === "/hardware.php")
      return response("<html>hardware</html>");
    if (request.path === "/lan.php") return response("<html>lan</html>");
    if (request.path === "/troubleshooting_logs.php")
      return response("<html>logs page</html>");
    if (request.path === "/actionHandler/ajax_troubleshooting_logs.php")
      return response("[]");
    return response("not found", 404);
  };
}

describe("GatewayClient", () => {
  it("submits the existing login POST, follows its same-origin 302, and reuses the cookie jar", async () => {
    const requests: SeenRequest[] = [];
    const client = new GatewayClient(
      "http://router.test",
      "test-user",
      "secret-password",
      1000,
      successfulLoginFetcher(requests),
    );
    assert.equal(await client.network(), "<html>captured network page</html>");

    const check = requests.find((request) => request.path === "/check.php");
    assert.ok(check);
    assert.equal(check.method, "POST");
    assert.match(
      check.headers.get("content-type") ?? "",
      /application\/x-www-form-urlencoded/,
    );
    assert.match(check.body, /username=test-user/);
    assert.match(check.body, /password=secret-password/);
    assert.match(check.headers.get("cookie") ?? "", /csrfp_token=csrf-value/);
    assert.ok(
      requests.some((request) => request.path === "/home_loggedout.php"),
    );
    const protectedRead = requests.find(
      (request) => request.path === "/network_setup.php",
    );
    assert.ok(protectedRead);
    assert.match(
      protectedRead.headers.get("cookie") ?? "",
      /router_session=authenticated/,
    );
  });

  it("recognizes a protected-page HTTP 200 login form and performs one bounded reauthentication", async () => {
    const requests: SeenRequest[] = [];
    let networkReads = 0;
    let loginPosts = 0;
    const fetcher = async (
      url: string | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const request = seen(url, init);
      requests.push(request);
      if (request.path === "/") return response("<html>router</html>");
      if (request.path === "/check.php") {
        loginPosts += 1;
        return responseAt(url, "", 302, {
          location: "/home_loggedout.php",
          "set-cookie": `session=s${loginPosts}; Path=/`,
        });
      }
      if (request.path === "/home_loggedout.php")
        return response('<a href="home_loggedout.php">Logout</a>');
      if (request.path === "/network_setup.php") {
        networkReads += 1;
        return networkReads === 1
          ? response('<html><title>Login</title><input name="password"></html>')
          : response("<html>valid protected response</html>");
      }
      return response("not found", 404);
    };
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "p",
      1000,
      fetcher,
    );
    assert.equal(
      await client.network(),
      "<html>valid protected response</html>",
    );
    assert.equal(networkReads, 2);
    assert.equal(loginPosts, 2);
    assert.equal(requests.filter((request) => request.path === "/").length, 2);
  });

  it("recognizes the captured JavaScript session guard and performs one bounded reauthentication", async () => {
    const guard =
      '<script type="text/javascript">alert("Please Login First!"); location.href="home_loggedout.php";</script>';
    let loginPosts = 0;
    let networkReads = 0;
    const fetcher = async (
      url: string | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const request = seen(url, init);
      if (request.path === "/") return response("<html>router</html>");
      if (request.path === "/check.php") {
        loginPosts += 1;
        return response('<a href="home_loggedout.php">continue</a>');
      }
      if (request.path === "/network_setup.php") {
        networkReads += 1;
        return networkReads === 1
          ? response(guard)
          : response("<html>valid protected response</html>");
      }
      return response("not found", 404);
    };
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "p",
      1000,
      fetcher,
    );
    assert.equal(
      await client.network(),
      "<html>valid protected response</html>",
    );
    assert.equal(loginPosts, 2);
    assert.equal(networkReads, 2);
  });

  it("rejects the captured session guard as a login response instead of trusting its home_loggedout redirect", async () => {
    const guard =
      '<script type="text/javascript">alert("Please Login First!"); location.href="home_loggedout.php";</script>';
    const requests: SeenRequest[] = [];
    const fetcher = async (
      url: string | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const request = seen(url, init);
      requests.push(request);
      if (request.path === "/") return response("<html>router</html>");
      if (request.path === "/check.php") return response(guard);
      return response("unexpected", 200);
    };
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "p",
      1000,
      fetcher,
    );
    await assert.rejects(
      client.network(),
      (error: unknown) =>
        error instanceof GatewayError && error.code === "auth_failed",
    );
    assert.equal(
      requests.filter((request) => request.path === "/check.php").length,
      1,
    );
    assert.equal(
      requests.some((request) => request.path === "/network_setup.php"),
      false,
    );
  });

  it("backs off across subsequent page reads when the protected page still rejects a successful reauthentication", async () => {
    const requests: SeenRequest[] = [];
    let loginPosts = 0;
    const fetcher = async (
      url: string | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const request = seen(url, init);
      requests.push(request);
      if (request.path === "/") return response("<html>router</html>");
      if (request.path === "/check.php") {
        loginPosts += 1;
        return response('<a href="home_loggedout.php">Logout</a>');
      }
      if (["/network_setup.php", "/hardware.php"].includes(request.path)) {
        return response('<html><input name="password"></html>');
      }
      return response("not found", 404);
    };
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "p",
      1000,
      fetcher,
    );
    await assert.rejects(
      client.network(),
      (error: unknown) =>
        error instanceof GatewayError && error.code === "auth_failed",
    );
    await assert.rejects(
      client.hardware(),
      (error: unknown) =>
        error instanceof GatewayError && error.code === "auth_backoff",
    );
    assert.equal(loginPosts, 2);
    assert.equal(requests.filter((request) => request.path === "/").length, 2);
  });

  it("backs off after an HTTP 200 login rejection without submitting the password again", async () => {
    const requests: SeenRequest[] = [];
    const fetcher = async (
      url: string | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const request = seen(url, init);
      requests.push(request);
      if (request.path === "/") return response("<html>router portal</html>");
      if (request.path === "/check.php")
        return response('<script>location.href="/index.php"</script>');
      return response("unexpected", 200);
    };
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "p",
      1000,
      fetcher,
    );
    await assert.rejects(
      client.network(),
      (error: unknown) =>
        error instanceof GatewayError && error.code === "auth_failed",
    );
    await assert.rejects(
      client.network(),
      (error: unknown) =>
        error instanceof GatewayError && error.code === "auth_backoff",
    );
    assert.equal(
      requests.filter((request) => request.path === "/check.php").length,
      1,
    );
    assert.equal(requests.filter((request) => request.path === "/").length, 1);
  });

  it("rejects unsafe 307 POST and 302 read redirects before fetching their destinations", async () => {
    for (const [status, location, unsafePath] of [
      [307, "/unexpected-write-handler.php", "/unexpected-write-handler.php"],
      [
        302,
        "/actionHandler/ajax_troubleshooting_logs.php",
        "/actionHandler/ajax_troubleshooting_logs.php",
      ],
      [302, "/network_setup.php?redirect=unsafe", "/network_setup.php"],
    ] as const) {
      const requests: SeenRequest[] = [];
      const fetcher = async (
        url: string | URL,
        init?: RequestInit,
      ): Promise<Response> => {
        const request = seen(url, init);
        requests.push(request);
        if (request.path === "/") return response("<html>router</html>");
        if (request.path === "/check.php")
          return responseAt(url, "", status, { location });
        return response("unexpected destination response");
      };
      const client = new GatewayClient(
        "http://router.test",
        "u",
        "p",
        1000,
        fetcher,
      );
      await assert.rejects(
        client.network(),
        (error: unknown) =>
          error instanceof GatewayError && error.code === "http_failed",
      );
      assert.equal(
        requests.filter((request) => request.path === "/check.php").length,
        1,
      );
      assert.equal(
        requests.some((request) => request.path === unsafePath),
        false,
      );
    }
  });

  it("returns malformed protected HTML without mistaking it for expired authentication", async () => {
    const requests: SeenRequest[] = [];
    const fetcher = async (
      url: string | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const request = seen(url, init);
      requests.push(request);
      if (request.path === "/") return response("<html>router</html>");
      if (request.path === "/check.php")
        return response('<a href="home_loggedout.php">Logout</a>');
      if (request.path === "/network_setup.php")
        return response("<html><body>partial but not a login page");
      return response("not found", 404);
    };
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "p",
      1000,
      fetcher,
    );
    assert.equal(
      await client.network(),
      "<html><body>partial but not a login page",
    );
    assert.equal(
      requests.filter((request) => request.path === "/check.php").length,
      1,
    );
    assert.equal(
      requests.filter((request) => request.path === "/network_setup.php")
        .length,
      1,
    );
  });

  it("sends the csrf cookie value on the known firewall log POST endpoint", async () => {
    const requests: SeenRequest[] = [];
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "p",
      1000,
      successfulLoginFetcher(requests),
    );
    assert.equal(await client.logs("firewall", "Today"), "[]");

    const logPost = requests.find(
      (request) =>
        request.path === "/actionHandler/ajax_troubleshooting_logs.php",
    );
    assert.ok(logPost);
    assert.equal(logPost.method, "POST");
    assert.equal(logPost.headers.get("csrfp_token"), "csrf-value");
    assert.match(logPost.headers.get("cookie") ?? "", /csrfp_token=csrf-value/);
    assert.match(logPost.body, /mode=firewall/);
    assert.match(logPost.body, /timef=Today/);
    const allowed = new Set([
      "/",
      "/check.php",
      "/home_loggedout.php",
      "/troubleshooting_logs.php",
      "/actionHandler/ajax_troubleshooting_logs.php",
    ]);
    assert.ok(requests.every((request) => allowed.has(request.path)));
  });

  it("treats login-guard text inside a JSON log record as data", async () => {
    const requests: SeenRequest[] = [];
    const payload = JSON.stringify([
      {
        time: "2000/01/02 00:00:00",
        Des: "Please Login First!",
        Count: "1",
        Type: "Firewall Blocked",
      },
    ]);
    const base = successfulLoginFetcher(requests);
    const fetcher = async (
      url: string | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      if (
        new URL(String(url)).pathname ===
        "/actionHandler/ajax_troubleshooting_logs.php"
      ) {
        requests.push(seen(url, init));
        return response(payload);
      }
      return base(url, init);
    };
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "p",
      1000,
      fetcher,
    );
    assert.equal(await client.logs("firewall", "Today"), payload);
  });

  it("maps request timeouts to a bounded error without leaking credentials or response text", async () => {
    const fetcher = async (
      url: string | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const path = new URL(String(url)).pathname;
      if (path === "/") return response("<html>router</html>");
      if (path === "/check.php")
        return response('<a href="home_loggedout.php">Logout</a>');
      if (path === "/network_setup.php") {
        return new Promise<Response>((_, reject) => {
          const keepAlive = setTimeout(() => {}, 1000);
          init?.signal?.addEventListener(
            "abort",
            () => {
              clearTimeout(keepAlive);
              reject(new Error("secret-password: private response body"));
            },
            { once: true },
          );
        });
      }
      return response("not found", 404);
    };
    const client = new GatewayClient(
      "http://router.test",
      "u",
      "secret-password",
      25,
      fetcher,
    );
    await assert.rejects(client.network(), (error: unknown) => {
      assert.ok(error instanceof GatewayError);
      assert.equal(error.code, "timeout");
      assert.equal(error.message, "timeout");
      assert.equal(error.message.includes("secret-password"), false);
      assert.equal(error.message.includes("private response body"), false);
      return true;
    });
  });
});
