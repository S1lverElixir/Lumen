/**
 * Юнит-проверки чистых функций Workers-релея. Сеть не нужна: fetch и резолвер
 * DNS подменяются. Запуск: node --test worker/worker.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildForwardHeaders,
  buildResponseHeaders,
  handleReadPage,
  handleRequest,
  isPrivateAddress,
  PROXY_AUTH_HEADER,
  resolveReadTarget,
  resolveTarget,
  warnOnTruncation,
} from "./worker.ts";

const SECRET = "x".repeat(40);

test("target resolution keeps the allowlist and path rules", () => {
  const ok = resolveTarget("/fetch/api.telegram.org/bot123/getMe", "");
  assert.equal(ok.ok, true);
  assert.equal(resolveTarget("/fetch/evil.example/x", "").ok, false);
  assert.equal(resolveTarget("/fetch/api.telegram.org/notbot", "").ok, false);
  assert.equal(resolveTarget("/fetch/api.cloudflare.com/client/v4/accounts/1/r2", "").ok, false);
  assert.equal(resolveTarget("/fetch/api.cloudflare.com/client/v4/accounts/1/ai/run/@cf/x/y", "").ok, true);
  assert.equal(resolveTarget("/fetch/api.cloudflare.com/client/v4/accounts/1/ai/models/search", "?limit=1").ok, true);
  assert.equal(resolveTarget("/fetch/api.cloudflare.com/client/v4/accounts/1/ai/models", "").ok, false);
});

test("private addresses cover the usual ranges", () => {
  // R6-05: special-purpose внутри 2000::/3 из IANA registry.
  for (const ip of ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.1.1", "::1", "fc00::1", "fe80::1", "2002::1", "64:ff9b::1", "2001:db8::1", "2001:2::1", "2001:10::1", "2001:20::1", "2001:1::1", "3000::1", "2620:4f80::1"]) {
    assert.equal(isPrivateAddress(ip), true, ip);
  }
  assert.equal(isPrivateAddress("93.184.216.34"), false);
  assert.equal(isPrivateAddress("2606:2800:220:1::1"), false);
});

test("read target rejects literals, ports and private DNS answers", async () => {
  assert.equal((await resolveReadTarget("http://example.com/")).ok, false);
  assert.equal((await resolveReadTarget("https://127.0.0.1/")).ok, false);
  assert.equal((await resolveReadTarget("https://example.com:8443/")).ok, false);
  const privateDns = async () => ["10.0.0.5"];
  assert.equal((await resolveReadTarget("https://example.com/", privateDns)).ok, false);
  const publicDns = async () => ["93.184.216.34"];
  assert.equal((await resolveReadTarget("https://example.com/", publicDns)).ok, true);
});

test("missing or wrong secret is rejected before any upstream call", async () => {
  let called = 0;
  const fakeFetch = (async () => {
    called += 1;
    return new Response("ok");
  }) as typeof fetch;
  const noSecret = await handleRequest(new Request("https://proxy/fetch/api.telegram.org/bot1/x"), fakeFetch, undefined);
  assert.equal(noSecret.status, 503);
  const wrongSecret = "y".repeat(40);
  assert.notEqual(wrongSecret, SECRET);
  const wrong = await handleRequest(
    new Request("https://proxy/fetch/api.telegram.org/bot1/x", {
      headers: { [PROXY_AUTH_HEADER]: wrongSecret },
    }),
    fakeFetch, SECRET,
  );
  assert.equal(wrong.status, 401);
  assert.equal(called, 0);
});

test("forward headers drop hop-by-hop and the secret", () => {
  const headers = buildForwardHeaders(new Headers({
    host: "proxy", connection: "keep-alive", [PROXY_AUTH_HEADER]: "secret", "content-length": "5", "x-keep": "1",
  }));
  assert.equal(headers.has("host"), false);
  assert.equal(headers.has(PROXY_AUTH_HEADER), false);
  assert.equal(headers.get("x-keep"), "1");
});

test("truncation closes the stream in close mode without an error", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("abcdef"));
      controller.enqueue(new TextEncoder().encode("ghij"));
      controller.close();
    },
  });
  const capped = warnOnTruncation(stream, 6, "example.com", "close");
  const text = await new Response(capped).text();
  assert.equal(text, "abcdef");
});

// ── Паритет с Deno-релеем (ревью 17.11.2026): контракт один и тот же. ──

test("buildResponseHeaders drops content-encoding and length, keeps the rest", () => {
  const headers = buildResponseHeaders(new Headers({
    "content-encoding": "gzip", "content-length": "5", "x-keep": "1",
  }));
  assert.equal(headers.has("content-encoding"), false);
  assert.equal(headers.has("content-length"), false);
  assert.equal(headers.get("x-keep"), "1");
});

test("secret is checked before routing: 401 wins over 403 and 404", async () => {
  let called = 0;
  const fakeFetch = (async () => {
    called += 1;
    return new Response("ok");
  }) as typeof fetch;
  const forbidden = await handleRequest(new Request("https://proxy/fetch/evil.example/x"), fakeFetch, SECRET);
  assert.equal(forbidden.status, 401);
  const missingRoute = await handleRequest(new Request("https://proxy/nope"), fakeFetch, SECRET);
  assert.equal(missingRoute.status, 401);
  assert.equal(called, 0);
});

test("upstream 3xx is refused as 502 instead of followed", async () => {
  const fakeFetch = (async () => new Response("", { status: 302, headers: { location: "https://evil.example/" } })) as typeof fetch;
  const resp = await handleRequest(
    new Request("https://proxy/fetch/api.telegram.org/bot1/getMe", { headers: { [PROXY_AUTH_HEADER]: SECRET } }),
    fakeFetch, SECRET,
  );
  assert.equal(resp.status, 502);
});

test("HEAD returns an empty body and keeps the upstream status", async () => {
  const fakeFetch = (async () => new Response("payload", { status: 200 })) as typeof fetch;
  const resp = await handleRequest(
    new Request("https://proxy/fetch/api.telegram.org/bot1/getMe", { method: "HEAD", headers: { [PROXY_AUTH_HEADER]: SECRET } }),
    fakeFetch, SECRET,
  );
  assert.equal(resp.status, 200);
  assert.equal(await resp.text(), "");
});

test("declared oversized body is 413 before any fetch", async () => {
  let called = 0;
  const fakeFetch = (async () => {
    called += 1;
    return new Response("ok");
  }) as typeof fetch;
  const resp = await handleRequest(
    new Request("https://proxy/fetch/api.telegram.org/bot1/sendMessage", {
      method: "POST", headers: { [PROXY_AUTH_HEADER]: SECRET, "content-length": "100" }, body: "x",
    }),
    fakeFetch, SECRET, 10,
  );
  assert.equal(resp.status, 413);
  assert.equal(called, 0);
});

test("chunked oversized request body is 413", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("1234567890"));
      controller.close();
    },
  });
  const fakeFetch = (async (_url: string, init: RequestInit) => {
    const reader = (init.body as ReadableStream<Uint8Array>).getReader();
    while (true) {
      const { done } = await reader.read();
      if (done) break;
    }
    return new Response("ok");
  }) as typeof fetch;
  const req = new Request("https://proxy/fetch/api.telegram.org/bot1/sendMessage", {
    method: "POST", headers: { [PROXY_AUTH_HEADER]: SECRET }, body: stream, duplex: "half",
  } as RequestInit);
  const resp = await handleRequest(req, fakeFetch, SECRET, 4);
  assert.equal(resp.status, 413);
});

test("upstream timeout is 504", async () => {
  const fakeFetch = (async () => {
    throw new DOMException("timed out", "TimeoutError");
  }) as unknown as typeof fetch;
  const resp = await handleRequest(
    new Request("https://proxy/fetch/api.telegram.org/bot1/getMe", { headers: { [PROXY_AUTH_HEADER]: SECRET } }),
    fakeFetch, SECRET,
  );
  assert.equal(resp.status, 504);
});

test("response body over the cap is cut with an error", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      // Кап режет по границам чанков, как Deno-релей: первый чанк ровно 4.
      controller.enqueue(new TextEncoder().encode("abcd"));
      controller.enqueue(new TextEncoder().encode("efgh"));
      controller.close();
    },
  });
  const fakeFetch = (async () => new Response(stream, { status: 200 })) as typeof fetch;
  const resp = await handleRequest(
    new Request("https://proxy/fetch/api.telegram.org/bot1/getMe", { headers: { [PROXY_AUTH_HEADER]: SECRET } }),
    fakeFetch, SECRET, 4,
  );
  // Ревью 26.11.2026: /fetch у обоих релеев рвётся ошибкой (mode "error"),
  // обрезка close осталась только у /read.
  assert.equal(resp.status, 200);
  await assert.rejects(() => resp.text(), /exceeds 4 byte cap/);
});

test("read route: method, content type, status and redirect are enforced", async () => {
  const publicDns = async () => ["93.184.216.34"];
  const makeReq = (method = "GET") => new Request("https://proxy/read?url=https://example.com/", {
    method, headers: { [PROXY_AUTH_HEADER]: SECRET },
  });
  const html = (async () => new Response("<html>ok</html>", { status: 200, headers: { "content-type": "text/html" } })) as typeof fetch;
  assert.equal((await handleRequest(makeReq("POST"), html, SECRET, undefined, publicDns)).status, 405);

  const pdf = (async () => new Response("%PDF", { status: 200, headers: { "content-type": "application/pdf" } })) as typeof fetch;
  assert.equal((await handleRequest(makeReq(), pdf, SECRET, undefined, publicDns)).status, 415);

  const boom = (async () => new Response("err", { status: 500, headers: { "content-type": "text/html" } })) as typeof fetch;
  assert.equal((await handleRequest(makeReq(), boom, SECRET, undefined, publicDns)).status, 502);

  const redirect = (async () => new Response("", { status: 302, headers: { "content-type": "text/html", location: "https://evil.example/" } })) as typeof fetch;
  assert.equal((await handleRequest(makeReq(), redirect, SECRET, undefined, publicDns)).status, 502);

  const ok = await handleRequest(makeReq(), html, SECRET, undefined, publicDns);
  assert.equal(ok.status, 200);
  assert.equal(await ok.text(), "<html>ok</html>");
});

test("full and mapped IPv6 forms are private", () => {
  for (const ip of [
    "0000:0000:0000:0000:0000:0000:0000:0001",
    "0000:0000:0000:0000:0000:ffff:7f00:0001",
    "feff::1",
    "fed0::1",
    "2001::1",
    "2606:4700:4700::1111",
  ]) {
    const expected = ip !== "2606:4700:4700::1111";
    assert.equal(isPrivateAddress(ip), expected, ip);
  }
});

// ── Паритет I18 (аудит 26.11.2026): R6-02/03/04 в /read. ──

test("read: кап отменяет апстрим, а не досчитывает его до EOF", async () => {
  // R6-02: раньше остаток после капа вычитывался весь — поток тянул трафик
  // и время, пока апстрим сам не закроется.
  let cancelled = false;
  let pulled = 0;
  const src = new ReadableStream<Uint8Array>({
    pull(c) {
      pulled += 1;
      if (pulled > 512) { c.close(); return; }
      c.enqueue(new Uint8Array(1024));
    },
    cancel() { cancelled = true; },
  });
  const capped = warnOnTruncation(src, 4 * 1024, "example.com", "close");
  const body = await new Response(capped).arrayBuffer();
  assert.ok(body.byteLength <= 5 * 1024, "тело не перерастает кап больше чем на чанк");
  assert.ok(cancelled, "апстрим обязан быть отменён на капе");
  assert.ok(pulled < 512, "остаток не должен вычитываться досуха: pulls=" + pulled);
});

test("read: отказ по типу, статусу и редиректу отменяет непрочитанное тело", async () => {
  // R6-03: тело отказного ответа раньше оставалось неоткрытым на соединении.
  // У workers redirect:"manual", поэтому ветка 3xx здесь тоже своя.
  const mkSpy = (status: number, contentType: string) => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(c) { c.enqueue(new Uint8Array(16)); },
      cancel() { cancelled = true; },
    });
    return {
      res: () => Promise.resolve(new Response(stream, { status, headers: { "content-type": contentType } })),
      isCancelled: () => cancelled,
    };
  };
  const u = new URL("https://proxy.test/read?url=" + encodeURIComponent("https://example.com/"));
  const req = new Request(u);
  const pubDns = async () => ["93.184.216.34"];
  const png = mkSpy(200, "image/png");
  assert.equal((await handleReadPage(req, u, png.res as typeof fetch, pubDns)).status, 415);
  assert.ok(png.isCancelled(), "415 обязан отменить тело апстрима");
  const err500 = mkSpy(500, "text/html");
  assert.equal((await handleReadPage(req, u, err500.res as typeof fetch, pubDns)).status, 502);
  assert.ok(err500.isCancelled(), "нестандовый статус обязан отменить тело апстрима");
  const redir = mkSpy(302, "text/html");
  assert.equal((await handleReadPage(req, u, redir.res as typeof fetch, pubDns)).status, 502);
  assert.ok(redir.isCancelled(), "редирект обязан отменить тело апстрима");
});

test("read: дедлайн тикает и во время DNS", async () => {
  // R6-04: AbortSignal.timeout создавался после резолва — медленный DNS не
  // ограничивался ничем. Теперь дедлайн начинается с начала запроса.
  const u = new URL("https://proxy.test/read?url=" + encodeURIComponent("https://example.com/"));
  let fetched = false;
  const slowDns = () => new Promise<string[]>((res) => setTimeout(() => res(["93.184.216.34"]), 60));
  const fetchImpl = (async () => {
    fetched = true;
    return new Response("<p>x</p>", { headers: { "content-type": "text/html" } });
  }) as typeof fetch;
  const res = await handleReadPage(new Request(u), u, fetchImpl, slowDns, 20);
  assert.equal(res.status, 504);
  assert.ok(!fetched, "fetch не должен стартовать после истечения дедлайна");
});

test("read: зависший DNS отсекается дедлайном, а не дожидается резолвера", async () => {
  // Ревью 26.11.2026 (R6-04): без гонки DNS-и-дедлайн висячий резолв
  // переживал readTimeoutMs; здесь резолвер не возвращается никогда.
  const u = new URL("https://proxy.test/read?url=" + encodeURIComponent("https://example.com/"));
  let fetched = false;
  const hangDns = () => new Promise<string[]>(() => {});
  const fetchImpl = (async () => {
    fetched = true;
    return new Response("<p>x</p>", { headers: { "content-type": "text/html" } });
  }) as typeof fetch;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const guard = new Promise<"hang">((res) => { timer = setTimeout(() => res("hang"), 500); });
  const raced = await Promise.race([handleReadPage(new Request(u), u, fetchImpl, hangDns, 20), guard]);
  clearTimeout(timer);
  assert.notEqual(raced, "hang", "зависший резолв не должен переживать дедлайн");
  assert.equal((raced as Response).status, 504);
  assert.ok(!fetched);
});
