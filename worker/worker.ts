/**
 * Cloudflare Workers-версия релея Lumen: тот же контракт, что proxy/proxy.ts.
 * Перенос с Deno Deploy (закрывается в апреле 2027, а классический deployctl
 * больше не принимает токены новой консоли). XMLHttp-пути, секрет, капы и /read
 * совпадают с Deno-версией: Python-код менять не нужно, только базовый URL.
 */

export const PROXY_AUTH_HEADER = "X-Lumen-Proxy-Secret";

export const ALLOWED_HOSTS = new Set([
  "api.telegram.org",
  "www.tikwm.com",
  "tikwm.com",
  "api.cloudflare.com",
]);

export const MAX_REQUEST_BODY_BYTES = 100 * 1024 * 1024;
export const MIN_PROXY_SECRET_LENGTH = 32;
export const UPSTREAM_FETCH_TIMEOUT_MS = 60_000;
export const READ_PAGE_MAX_BYTES = 2 * 1024 * 1024;
export const READ_PAGE_TIMEOUT_MS = 15_000;

const READ_ALLOWED_TYPES = new Set(["text/html", "text/plain", "application/xhtml+xml"]);

const HOP_BY_HOP_REQUEST_HEADERS = [
  "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
];
const HOP_BY_HOP_RESPONSE_HEADERS = [
  "content-encoding", "content-length", "connection", "transfer-encoding",
  "keep-alive", "trailer", "upgrade",
];

export class BodyTooLargeError extends Error {}

export function warnOnTruncation(
  stream: ReadableStream<Uint8Array> | null,
  maxBytes: number,
  host: string,
  mode: "error" | "close" = "error",
): ReadableStream<Uint8Array> | null {
  if (!stream) return stream;
  let total = 0;
  const reader = stream.getReader();
  // Зеркало proxy/proxy.ts (R6-02, аудит 26.11.2026): на пределе апстрим
  // отменяется, а не досчитывается до EOF.
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          controller.close();
          return;
        }
        total += value.byteLength;
        if (total > maxBytes) {
          console.warn(`[worker] ${host} response truncated at ${maxBytes} bytes`);
          await reader.cancel();
          if (mode === "error") controller.error(new BodyTooLargeError(`Response exceeds ${maxBytes} byte cap`));
          else controller.close();
          return;
        }
        controller.enqueue(value);
      } catch (err) {
        await reader.cancel().catch(() => {});
        controller.error(err);
      }
    },
    cancel(reason) {
      return reader.cancel(reason);
    },
  });
}

export function limitStreamBytes(
  stream: ReadableStream<Uint8Array> | null,
  maxBytes: number,
): ReadableStream<Uint8Array> | null {
  if (!stream) return stream;
  let total = 0;
  return stream.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      total += chunk.byteLength;
      if (total > maxBytes) controller.error(new BodyTooLargeError(`Body exceeds ${maxBytes} byte cap`));
      else controller.enqueue(chunk);
    },
  }));
}

export type TargetResolution =
  | { ok: true; url: string }
  | { ok: false; status: 404 | 403; message: string };

export function resolveTarget(pathname: string, search: string): TargetResolution {
  const parts = pathname.split("/");
  if (parts.length < 3 || parts[1] !== "fetch" || parts[2] === "") {
    return { ok: false, status: 404, message: "Not found" };
  }
  const host = parts[2];
  if (!ALLOWED_HOSTS.has(host)) return { ok: false, status: 403, message: "Host not allowed" };
  const path = "/" + parts.slice(3).join("/");
  if (host === "api.telegram.org" && !isTelegramBotPath(path)) {
    return { ok: false, status: 404, message: "Not found" };
  }
  if (host === "api.cloudflare.com" && !isCloudflareAiPath(path)) {
    return { ok: false, status: 404, message: "Not found" };
  }
  return { ok: true, url: `https://${host}${path}${search}` };
}

function isTelegramBotPath(path: string): boolean {
  return /^\/bot[^/]+\/.+/.test(path) || /^\/file\/bot[^/]+\/.+/.test(path);
}

function isCloudflareAiPath(path: string): boolean {
  // Генерация и каталог моделей (каталог зовёт /diag); остальное поддерево — нет.
  return /^\/client\/v4\/accounts\/[^/]+\/ai\/(run|models)\//.test(path);
}

export function buildForwardHeaders(reqHeaders: Headers): Headers {
  const headers = new Headers(reqHeaders);
  for (const name of HOP_BY_HOP_REQUEST_HEADERS) headers.delete(name);
  headers.delete("content-length");
  headers.delete(PROXY_AUTH_HEADER);
  return headers;
}

export function buildResponseHeaders(upstreamHeaders: Headers): Headers {
  const headers = new Headers(upstreamHeaders);
  for (const name of HOP_BY_HOP_RESPONSE_HEADERS) headers.delete(name);
  return headers;
}

function isPrivateV4(a: number, b: number): boolean {
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 100 && b >= 64 && b <= 127) return true;
  if (a === 198 && (b === 18 || b === 19)) return true;
  if (a === 192 && b === 0) return true;
  if (a === 198 && b === 51) return true;
  if (a === 203 && b === 0) return true;
  return a >= 224;
}

function ipv6ToBigInt(ip: string): bigint | null {
  let s = ip.split("%")[0].toLowerCase();
  // Встроенный IPv4-хвост: ::ffff:1.2.3.4 и 2002::1.2.3.4.
  const v4tail = s.match(/^(.*:)(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (v4tail) {
    const a = Number(v4tail[2]), b = Number(v4tail[3]), c = Number(v4tail[4]), d = Number(v4tail[5]);
    if ([a, b, c, d].some((n) => n > 255)) return null;
    s = v4tail[1] + ((a << 8) | b).toString(16) + ":" + ((c << 8) | d).toString(16);
  }
  const halves = s.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  if (halves.length === 1 && head.length !== 8) return null;
  if (halves.length === 2 && head.length + tail.length > 8) return null;
  const fill = 8 - head.length - tail.length;
  const all = [...head, ...Array(halves.length === 2 ? fill : 0).fill("0"), ...tail];
  if (all.length !== 8) return null;
  let n = 0n;
  for (const h of all) {
    if (!/^[0-9a-f]{1,4}$/.test(h)) return null;
    n = (n << 16n) | BigInt(parseInt(h, 16));
  }
  return n;
}

export function isPrivateAddress(ip: string): boolean {
  const v4 = ip.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (v4) {
    const parts = [v4[1], v4[2], v4[3], v4[4]].map(Number);
    if (parts.some((n) => n > 255)) return true;
    return isPrivateV4(parts[0], parts[1]);
  }
  // Числовые диапазоны, а не строковые префиксы: полная форма с ведущими
  // нулями, IPv4-mapped в полной форме, Teredo и site-local проходили как
  // публичные (ревью 17.11.2026).
  const n = ipv6ToBigInt(ip);
  if (n === null) return false;
  if (n === 0n || n === 1n) return true; // :: и ::1
  const top96 = n >> 32n;
  const lowV4 = Number(n & 0xffffffffn);
  if (top96 === 0xffffn || top96 === 0n) {
    // IPv4-mapped ::ffff:a.b.c.d и compatible ::a.b.c.d
    return isPrivateV4(lowV4 >>> 24, (lowV4 >>> 16) & 255);
  }
  if (top96 === 0x64ff9b0000000000000000n) return true; // 64:ff9b::/96 NAT64
  if ((n >> 96n) === 0x20010000n) return true; // Teredo 2001::/32
  if ((n >> 112n) === 0x2002n) return true; // 6to4 2002::/16
  if ((n >> 121n) === 0x7en) return true; // fc00::/7 unique local
  const site = n >> 118n;
  if (site === 0x3fan || site === 0x3fbn) return true; // fe80::/10 и fec0::/10
  if ((n >> 120n) === 0xffn) return true; // ff00::/8 multicast
  if ((n >> 64n) === 0x100n) return true; // 100::/64 discard
  // R6-05: special-purpose внутри 2000::/3 (IANA IPv6 Address Registry) — раньше
  // проходили как публичные и пропускали SSRF-проверку /read.
  if ((n >> 96n) === 0x20010db8n) return true; // 2001:db8::/32 documentation
  if ((n >> 80n) === 0x200100020000n) return true; // 2001:2::/48 benchmarking
  if ((n >> 100n) === 0x2001001n) return true; // 2001:10::/28 ORCHID (RFC 7285)
  if ((n >> 100n) === 0x2001002n) return true; // 2001:20::/28 ORCHIDv2 (RFC 9375, ревью 26.11.2026)
  if (n === 0x20010001000000000000000000000001n) return true; // 2001:1::1/128 RTBH
  if ((n >> 124n) === 0b0011n) return true; // 3000::-3fff:: (IANA GSE, зарезервирован)
  if ((n >> 96n) === 0x26204f80n) return true; // 2620:4f80::/32 DCTP (ревью 26.11.2026)
  return false;
}

export type ReadTarget =
  | { ok: true; url: string }
  | { ok: false; status: 400 | 403 | 502; message: string };

// Workers не умеет Deno.resolveDns, зато умеет DNS-over-HTTPS. Оба семейства
// проверяем до fetch: имя может указывать на приватный адрес без литерала.
export async function resolveViaDoh(host: string, type: "A" | "AAAA"): Promise<string[]> {
  const resp = await fetch(
    `https://cloudflare-dns.com/dns-query?name=${encodeURIComponent(host)}&type=${type}`,
    { headers: { accept: "application/dns-json" }, signal: AbortSignal.timeout(3_000) },
  );
  if (!resp.ok) return [];
  const data = await resp.json() as { Answer?: Array<{ type: number; data: string }> };
  const wanted = type === "A" ? 1 : 28;
  return (data.Answer ?? []).filter((a) => a.type === wanted).map((a) => a.data);
}

export async function resolveReadTarget(
  raw: string,
  resolveDnsImpl: (name: string, type: "A" | "AAAA") => Promise<string[]> = resolveViaDoh,
): Promise<ReadTarget> {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return { ok: false, status: 400, message: "Bad URL" };
  }
  if (parsed.protocol !== "https:") return { ok: false, status: 403, message: "Only https is allowed" };
  if (parsed.username || parsed.password) return { ok: false, status: 403, message: "Credentials are not allowed" };
  if (parsed.port && parsed.port !== "443") return { ok: false, status: 403, message: "Only the default https port is allowed" };
  const host = parsed.hostname.toLowerCase();
  if (!host || host === "localhost" || !host.includes(".") || host.endsWith(".local") || host.endsWith(".internal")) {
    return { ok: false, status: 403, message: "Host is not a public name" };
  }
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || host.includes(":")) {
    return { ok: false, status: 403, message: "IP literals are not allowed" };
  }
  const addrs: string[] = [];
  for (const type of ["A", "AAAA"] as const) {
    try {
      addrs.push(...await resolveDnsImpl(host, type));
    } catch {
      // Нет записей этого типа — не ошибка.
    }
  }
  if (addrs.length === 0) return { ok: false, status: 502, message: "Host did not resolve" };
  if (addrs.some(isPrivateAddress)) return { ok: false, status: 403, message: "Host resolves to a private address" };
  return { ok: true, url: parsed.toString() };
}

export async function handleReadPage(
  req: Request,
  url: URL,
  fetchImpl: typeof fetch,
  resolveDnsImpl?: (name: string, type: "A" | "AAAA") => Promise<string[]>,
  readTimeoutMs: number = READ_PAGE_TIMEOUT_MS,
): Promise<Response> {
  if (req.method !== "GET") return new Response("Method not allowed", { status: 405 });
  // R6-04 (аудит 26.11.2026): дедлайн считается с начала запроса, включая DNS.
  const deadline = AbortSignal.timeout(readTimeoutMs);
  // R6-01: DNS-ответ здесь и резолв внутри fetch не скреплены — workers не
  // умеют pin; окно ребиндинга остаётся и задокументировано в отчёте аудита.
  if (deadline.aborted) return new Response("Read timed out", { status: 504 });
  const target = await Promise.race<ReadTarget | "timeout">([
    resolveReadTarget(url.searchParams.get("url") ?? "", resolveDnsImpl),
    // Ревью 26.11.2026 (R6-04): проверка после резолва не спасала бы от
    // зависшего DoH — гонка DNS и дедлайна обязательна.
    new Promise<"timeout">((res) => deadline.addEventListener("abort", () => res("timeout"), { once: true })),
  ]);
  if (target === "timeout") return new Response("Read timed out", { status: 504 });
  if (!target.ok) return new Response(target.message, { status: target.status });
  let upstream: Response;
  try {
    upstream = await fetchImpl(target.url, {
      method: "GET",
      headers: { "user-agent": "Lumen/1.0", "accept": "text/html,text/plain;q=0.9,*/*;q=0.1" },
      // Workers не принимает redirect:"error" (только follow/manual): manual и
      // явный отказ на 3xx даёт ту же семантику — за проверенный адрес не уходим.
      redirect: "manual",
      signal: deadline,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "TimeoutError") {
      return new Response("Read timed out", { status: 504 });
    }
    return new Response("Read failed", { status: 502 });
  }
  // R6-03: любой отказ закрывает непрочитанное тело апстрима.
  if (upstream.status >= 300 && upstream.status < 400) {
    await upstream.body?.cancel();
    return new Response("Redirect not followed", { status: 502 });
  }
  const rawType = upstream.headers.get("content-type") ?? "";
  const type = rawType.split(";")[0].trim().toLowerCase();
  if (!READ_ALLOWED_TYPES.has(type)) {
    await upstream.body?.cancel();
    return new Response("Unsupported content type", { status: 415 });
  }
  if (upstream.status !== 200) {
    await upstream.body?.cancel();
    return new Response("Upstream status " + upstream.status, { status: 502 });
  }
  return new Response(
    warnOnTruncation(upstream.body, READ_PAGE_MAX_BYTES, new URL(target.url).host, "close"),
    { status: 200, headers: { "content-type": rawType || type } },
  );
}

export async function handleRequest(
  req: Request,
  fetchImpl: typeof fetch = fetch,
  proxySecret: string | undefined = undefined,
  maxBodyBytes: number = MAX_REQUEST_BODY_BYTES,
  resolveReadDnsImpl?: (name: string, type: "A" | "AAAA") => Promise<string[]>,
): Promise<Response> {
  if (!proxySecret || proxySecret.length < MIN_PROXY_SECRET_LENGTH || !/^[\x21-\x7e]+$/.test(proxySecret)) {
    return new Response("Proxy authentication unavailable", { status: 503 });
  }
  const suppliedSecret = req.headers.get(PROXY_AUTH_HEADER) ?? "";
  const encoder = new TextEncoder();
  const [expected, supplied] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(proxySecret)),
    crypto.subtle.digest("SHA-256", encoder.encode(suppliedSecret)),
  ]);
  const expectedBytes = new Uint8Array(expected);
  const suppliedBytes = new Uint8Array(supplied);
  let difference = 0;
  for (let i = 0; i < expectedBytes.length; i++) difference |= expectedBytes[i] ^ suppliedBytes[i];
  if (!suppliedSecret || difference !== 0) return new Response("Unauthorized", { status: 401 });

  const url = new URL(req.url);
  if (url.pathname === "/read") return await handleReadPage(req, url, fetchImpl, resolveReadDnsImpl);
  const target = resolveTarget(url.pathname, url.search);
  if (!target.ok) return new Response(target.message, { status: target.status });

  const declaredLength = Number(req.headers.get("content-length") ?? "0");
  if (Number.isFinite(declaredLength) && declaredLength > maxBodyBytes) {
    return new Response("Request body too large", { status: 413 });
  }

  const forwardHeaders = buildForwardHeaders(req.headers);
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const cappedRequestBody = hasBody ? limitStreamBytes(req.body, maxBodyBytes) : undefined;
  let upstreamResp: Response;
  try {
    upstreamResp = await fetchImpl(target.url, {
      method: req.method,
      headers: forwardHeaders,
      redirect: "manual",
      body: cappedRequestBody,
      signal: AbortSignal.timeout(UPSTREAM_FETCH_TIMEOUT_MS),
    });
  } catch (err) {
    if (err instanceof BodyTooLargeError) return new Response("Request body too large", { status: 413 });
    if (err instanceof DOMException && err.name === "TimeoutError") {
      return new Response("Upstream fetch timed out", { status: 504 });
    }
    return new Response("Upstream fetch failed", { status: 502 });
  }

  if (upstreamResp.status >= 300 && upstreamResp.status < 400) {
    return new Response("Upstream redirect not followed", { status: 502 });
  }
  const respHeaders = buildResponseHeaders(upstreamResp.headers);
  const cappedBody = req.method === "HEAD"
    ? null
    // error — как у Deno-релея (proxy.ts:497, ревью 26.11.2026): ответ /fetch
    // сверх капа рвётся ошибкой; обрезка close осталась только у /read.
    : warnOnTruncation(upstreamResp.body, maxBodyBytes, new URL(target.url).host);
  return new Response(cappedBody, { status: upstreamResp.status, headers: respHeaders });
}

export default {
  async fetch(req: Request, env: { LUMEN_PROXY_SECRET?: string }): Promise<Response> {
    return handleRequest(req, fetch, env.LUMEN_PROXY_SECRET);
  },
};
