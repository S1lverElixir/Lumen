/**
 * Юнит-проверки чистых функций Workers-релея. Сеть не нужна: fetch и резолвер
 * DNS подменяются. Запуск: node --test worker/worker.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildForwardHeaders,
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
  assert.equal(resolveTarget("/fetch/api.cloudflare.com/client/v4/accounts/1/ai/run", "").ok, true);
});

test("private addresses cover the usual ranges", () => {
  for (const ip of ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.1.1", "::1", "fc00::1", "fe80::1", "2002::1", "64:ff9b::1"]) {
    assert.equal(isPrivateAddress(ip), true, ip);
  }
  assert.equal(isPrivateAddress("93.184.216.34"), false);
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
  const wrong = await handleRequest(new Request("https://proxy/fetch/api.telegram.org/bot1/x"), fakeFetch, SECRET);
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
