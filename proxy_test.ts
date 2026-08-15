// proxy_test.ts — юнит-тесты на proxy.ts. Намеренно БЕЗ внешних зависимостей
// (никаких jsr:/npm: импортов) — только простые ручные assert-функции ниже.
// Так `deno test` работает даже без доступа в интернет и не тянет lock-файл.
//
// Запуск:
//   deno test proxy_test.ts

import {
  ALLOWED_HOSTS,
  buildForwardHeaders,
  buildResponseHeaders,
  handleRequest,
  resolveTarget,
} from "./proxy.ts";

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

function assertEquals<T>(actual: T, expected: T, message?: string): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    throw new Error(
      message ?? `Ожидалось ${JSON.stringify(expected)}, получено ${JSON.stringify(actual)}`,
    );
  }
}

// ─────────────────────────── resolveTarget ───────────────────────────

Deno.test("resolveTarget строит корректный URL для разрешённого хоста", () => {
  const result = resolveTarget("/fetch/api.telegram.org/bot123/getMe", "");
  assert(result.ok, "ожидался ok=true");
  if (result.ok) {
    assertEquals(result.host, "api.telegram.org");
    assertEquals(result.url, "https://api.telegram.org/bot123/getMe");
  }
});

Deno.test("resolveTarget сохраняет query-строку как есть", () => {
  const result = resolveTarget("/fetch/www.tikwm.com/api/", "?url=https://x&hd=1");
  assert(result.ok, "ожидался ok=true");
  if (result.ok) {
    assertEquals(result.url, "https://www.tikwm.com/api/?url=https://x&hd=1");
  }
});

Deno.test("resolveTarget не теряет завершающий слеш в пути (регрессия)", () => {
  // РЕГРЕССИЯ: первая версия использовала .filter(Boolean) при разборе пути,
  // который съедал пустой хвостовой сегмент после последнего "/" — путь вида
  // "/fetch/host/api/" превращался в "https://host/api" (без слеша). Именно
  // такой формат URL реально нужен TikWM (см. bot.py: ".../api/?url=...").
  const withSlash = resolveTarget("/fetch/www.tikwm.com/api/", "");
  assert(withSlash.ok, "ожидался ok=true");
  if (withSlash.ok) assertEquals(withSlash.url, "https://www.tikwm.com/api/");

  const withoutSlash = resolveTarget("/fetch/www.tikwm.com/api", "");
  assert(withoutSlash.ok, "ожидался ok=true");
  if (withoutSlash.ok) assertEquals(withoutSlash.url, "https://www.tikwm.com/api");
});

Deno.test("resolveTarget корректно строит вложенный путь (Telegram file API)", () => {
  // Регрессия на реальный формат вызова из bot.py (_download_telegram_file_bytes):
  // {TELEGRAM_API_BASE_URL}/file/bot{TOKEN}/{file_path}
  const result = resolveTarget("/fetch/api.telegram.org/file/bot123/photos/file_1.jpg", "");
  assert(result.ok, "ожидался ok=true");
  if (result.ok) {
    assertEquals(result.url, "https://api.telegram.org/file/bot123/photos/file_1.jpg");
  }
});

Deno.test("resolveTarget отклоняет неразрешённый хост с 403 и не строит URL на его основе", () => {
  const result = resolveTarget("/fetch/evil.example.com/steal", "");
  assert(!result.ok, "ожидался ok=false");
  if (!result.ok) {
    assertEquals(result.status, 403);
  }
});

Deno.test("resolveTarget отклоняет некорректный формат пути с 404", () => {
  assertEquals(resolveTarget("/", "").ok, false);
  assertEquals(resolveTarget("/fetch", "").ok, false);
  assertEquals(resolveTarget("/fetch/", "").ok, false);
  assertEquals(resolveTarget("/wrong-prefix/api.telegram.org/getMe", "").ok, false);
});

Deno.test("ALLOWED_HOSTS содержит ровно те хосты, что реально нужны боту", () => {
  assertEquals(ALLOWED_HOSTS.has("api.telegram.org"), true);
  assertEquals(ALLOWED_HOSTS.has("www.tikwm.com"), true);
  assertEquals(ALLOWED_HOSTS.has("tikwm.com"), true);
  assertEquals(ALLOWED_HOSTS.size, 3);
});

// ─────────────────────────── buildForwardHeaders / buildResponseHeaders ───────────────────────────

Deno.test("buildForwardHeaders убирает hop-by-hop заголовки запроса, остальные сохраняет", () => {
  const original = new Headers({
    "Host": "proxy.example.com",
    "Connection": "keep-alive",
    "User-Agent": "test-ua",
  });
  const forwarded = buildForwardHeaders(original);
  assertEquals(forwarded.has("host"), false);
  assertEquals(forwarded.has("connection"), false);
  assertEquals(forwarded.get("user-agent"), "test-ua");
});

Deno.test("buildResponseHeaders убирает Content-Encoding/Content-Length (см. докстринг про распаковку)", () => {
  // РЕГРЕССИЯ: fetch() в Deno сам распаковывает gzip/br до того, как тело
  // становится доступно — пробросить исходный Content-Encoding было бы багом
  // (клиент попытался бы распаковать уже распакованные данные).
  const original = new Headers({
    "Content-Encoding": "gzip",
    "Content-Length": "1234",
    "Content-Type": "application/json",
  });
  const cleaned = buildResponseHeaders(original);
  assertEquals(cleaned.has("content-encoding"), false);
  assertEquals(cleaned.has("content-length"), false);
  assertEquals(cleaned.get("content-type"), "application/json");
});

// ─────────────────────────── handleRequest (с подменённым fetch) ───────────────────────────

Deno.test("handleRequest пробрасывает GET без тела и возвращает статус/тело апстрима", async () => {
  const fakeFetch: typeof fetch = (url, init) => {
    assertEquals(String(url), "https://www.tikwm.com/api/?url=x");
    assertEquals(init?.method, "GET");
    assertEquals(init?.body, undefined);
    return Promise.resolve(new Response(JSON.stringify({ code: 0 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  };
  const req = new Request("https://proxy.example/fetch/www.tikwm.com/api/?url=x", { method: "GET" });
  const resp = await handleRequest(req, fakeFetch);
  assertEquals(resp.status, 200);
  const body = await resp.json();
  assertEquals(body, { code: 0 });
});

Deno.test("handleRequest возвращает 403 для неразрешённого хоста, не вызывая fetch вообще", async () => {
  let fetchCalled = false;
  const fakeFetch: typeof fetch = () => {
    fetchCalled = true;
    return Promise.resolve(new Response("не должно случиться"));
  };
  const req = new Request("https://proxy.example/fetch/evil.example.com/x", { method: "GET" });
  const resp = await handleRequest(req, fakeFetch);
  assertEquals(resp.status, 403);
  assertEquals(fetchCalled, false, "fetch не должен был вызываться для неразрешённого хоста");
});

Deno.test("handleRequest возвращает 404 для пути без /fetch/ префикса", async () => {
  const req = new Request("https://proxy.example/something-else", { method: "GET" });
  const unusedFetch: typeof fetch = () => Promise.resolve(new Response("unused"));
  const resp = await handleRequest(req, unusedFetch);
  assertEquals(resp.status, 404);
});

Deno.test("handleRequest пробрасывает метод и тело для POST (Telegram sendMessage)", async () => {
  let capturedMethod: string | undefined;
  let capturedBodyIsStream = false;
  const fakeFetch: typeof fetch = (_url, init) => {
    capturedMethod = init?.method;
    capturedBodyIsStream = init?.body instanceof ReadableStream;
    return Promise.resolve(new Response('{"ok":true}', { status: 200 }));
  };
  const req = new Request("https://proxy.example/fetch/api.telegram.org/bot123/sendMessage", {
    method: "POST",
    body: JSON.stringify({ chat_id: 1, text: "hi" }),
    headers: { "Content-Type": "application/json" },
  });
  const resp = await handleRequest(req, fakeFetch);
  assertEquals(resp.status, 200);
  assertEquals(capturedMethod, "POST");
  assert(capturedBodyIsStream, "тело POST-запроса должно передаваться апстриму как поток (без буферизации целиком)");
});

Deno.test("handleRequest возвращает 502, если апстрим-fetch упал с исключением", async () => {
  const fakeFetch: typeof fetch = () => {
    throw new Error("network unreachable");
  };
  const req = new Request("https://proxy.example/fetch/api.telegram.org/getMe", { method: "GET" });
  const resp = await handleRequest(req, fakeFetch);
  assertEquals(resp.status, 502);
});
