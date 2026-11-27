/**
 * proxy.ts — единая точка выхода для запросов, которые сервер бота (HF Spaces)
 * не может сделать напрямую, потому что датацентровые IP HF Spaces блокируются
 * некоторыми сервисами на уровне сети (см. README проекта Lumen):
 *   - Telegram Bot API (полностью заблокирован на уровне TLS-handshake — без
 *     этого прокси бот вообще не может ходить в Telegram).
 *   - TikWM (стабильно отвечает HTTP 403 с пустым телом на исходящие запросы
 *     с IP HF Spaces — см. историю отладки, /logs с тегом [tikwm][diag]).
 *
 * ЗАМЕНЯЕТ собой прежний tg-proxy (умел проксировать только Telegram Bot API
 * по формату /bot<token>/<method>) — переименован и обобщён. Лимиты Deno
 * Deploy (free-тариф: 1 млн запросов/мес, 20 ГБ исходящего трафика/мес, 15ч
 * CPU/мес) общие на ВЕСЬ АККАУНТ, а не на отдельный проект — значит держать
 * два раздельных Deno-приложения (одно под Telegram, другое под TikWM) не
 * даёт вообще никакой отдельной квоты, только лишняя сущность для поддержки.
 * Один универсальный прокси проще: один домен, один секрет, один деплой.
 *
 * ── Формат ──
 * GET/POST/... https://<домен>/fetch/<host>/<путь...>?<query>
 *   -> https://<host>/<путь...>?<query>
 * Метод, заголовки и тело запроса передаются как есть; статус, заголовки и
 * тело ответа — тоже как есть (без буферизации целиком в память — тело
 * стримится напрямую, это важно для больших file-загрузок в Telegram, см.
 * sendVideo/sendPhoto/sendMediaGroup у бота).
 *
 * ── Почему нужен allowlist хостов ──
 * Без ограничения на разрешённые хосты это был бы открытый анонимный релей на
 * ЛЮБОЙ адрес в интернете — кто угодно, узнав домен, мог бы использовать его
 * для проксирования куда захочет, тратя общую квоту трафика аккаунта (те же
 * 20 ГБ/мес) и потенциально привлекая к аккаунту внимание как к источнику
 * абьюза/скана. ALLOWED_HOSTS ниже — единственные хосты, которые реально
 * нужны боту; расширять список нужно только по факту новой подтверждённой
 * необходимости (см. тот же принцип "не добавляй заранее" в остальном проекте).
 *
 * ── Как настроить бота на использование этого прокси ──
 * В HF Spaces secrets/variables:
 *   TELEGRAM_API_BASE_URL = https://<домен>/fetch/api.telegram.org
 *   TIKWM_API_BASE_URL    = https://<домен>/fetch/www.tikwm.com
 *   CLOUDFLARE_AI_BASE_URL= https://<домен>/fetch/api.cloudflare.com/client/v4/accounts
 * bot.py дальше сам достраивает нужные пути (/bot<token>/<method>,
 * /file/bot<token>/<path>, /api/?url=...) поверх этой базы — никаких других
 * изменений в Python-коде для смены адреса прокси не требуется.
 */

export const PROXY_AUTH_HEADER = "X-Lumen-Proxy-Secret";

export const ALLOWED_HOSTS = new Set([
  "api.telegram.org",
  "www.tikwm.com",
  "tikwm.com",
  // Workers AI: api.cloudflare.com молча дропает коннект с IP HF Spaces (прод
  // 09.11.2026: TimeoutError на каждую попытку /draw), тот же класс, что TikWM выше.
  "api.cloudflare.com",
]);

// Кап объявленного тела запроса (аудит 26.09.2026). Telegram сам режет загрузки
// на 50 МБ, а прокси — общий трафик аккаунта, поэтому граница взята с запасом
// под честно большие файлы, но не «без предела».
export const MAX_REQUEST_BODY_BYTES = 100 * 1024 * 1024;

// Короткий секрет перебирается быстро: нижняя граница длины с обеих сторон
// (здесь и в lumen_telegram_transport.py).
export const MIN_PROXY_SECRET_LENGTH = 32;

export class BodyTooLargeError extends Error {}

// Кап ответа с видимым следом: без него обрыв смотрелся как успешный 200 с
// коротким телом (JSON падал при разборе, файл — тихо битый; аудит 16.11.2026).
// В лог уходит только хост и граница, без пути и query (в пути Telegram живёт токен).
export function warnOnTruncation(
  stream: ReadableStream<Uint8Array> | null,
  maxBytes: number,
  host: string,
  // "error" — целостность файла важнее (Telegram-загрузки): обрыв обязан быть виден.
  // "close" — усечённый текст страницы полезнее ошибки: читаем начало и закрываемся.
  mode: "error" | "close" = "error",
): ReadableStream<Uint8Array> | null {
  if (!stream) return stream;
  let total = 0;
  const reader = stream.getReader();
  // Явный reader вместо pipeThrough: на пределе апстрим ОТМЕНЯЕТСЯ, а не
  // досчитывается до EOF — иначе перегруженная страница продолжала жрать
  // трафик и квоту Deno после обрезки (R6-02, аудит 26.11.2026).
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
          console.warn(`[proxy] ${host} response truncated at ${maxBytes} bytes`);
          if (mode === "error") {
            await reader.cancel();
            controller.error(new BodyTooLargeError(`Response exceeds ${maxBytes} byte cap`));
          } else {
            await reader.cancel();
            controller.close();
          }
          return;
        }
        controller.enqueue(value);
      } catch (err) {
        await reader.cancel().catch(() => {});
        controller.error(err);
      }
    },
    cancel(reason) {
      // Клиент ушёл раньше капа — апстрим тоже закрываем.
      return reader.cancel(reason);
    },
  });
}

export function limitStreamBytes(
  stream: ReadableStream<Uint8Array> | null,
  maxBytes: number,
): ReadableStream<Uint8Array> | null {
  // Потоковый кап без буферизации: считаем байты на лету и рвём стрим при
  // превышении. Нужен и для тела запроса без Content-Length, и для тела ответа:
  // иначе объявленный кап обходился chunked-потоком любого размера.
  if (!stream) return stream;
  let total = 0;
  return stream.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      total += chunk.byteLength;
      if (total > maxBytes) {
        controller.error(new BodyTooLargeError(`Body exceeds ${maxBytes} byte cap`));
      } else {
        controller.enqueue(chunk);
      }
    },
  }));
}

// Апстрим без таймаута держал бы запрос вечно, жря квоту Deno: висящий апстрим — 504.
export const UPSTREAM_FETCH_TIMEOUT_MS = 60_000;

// Заголовки, которые нельзя слепо пробрасывать дальше как есть — Host/Connection
// в запросе относятся к соединению с ЭТИМ (Deno) сервером, а не с реальным
// апстримом, апстрим сам выставит правильные. Content-Encoding/Content-Length в
// ОТВЕТЕ — fetch() в Deno уже сам распаковывает gzip/br к моменту, когда тело
// становится нам доступно, поэтому исходный Content-Encoding больше не описывает
// реальное тело — клиент, попытавшийся распаковать уже распакованное, получил бы
// битые данные. Content-Length по той же причине может не совпадать с реальным
// размером — рантайм сам выставит корректный Transfer-Encoding для стрима.
const HOP_BY_HOP_REQUEST_HEADERS = [
  "host",
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];
const HOP_BY_HOP_RESPONSE_HEADERS = [
  "content-encoding",
  "content-length",
  "connection",
  "transfer-encoding",
  "keep-alive",
  "trailer",
  "upgrade",
];

// Разложено на чистые тестируемые функции вместо одного большого обработчика:
// маршрутизацию и заголовки проверяют юниты без сети, сетевую часть —
// handleRequest через подмену fetch.

export type TargetResolution =
  | { ok: true; url: string }
  | { ok: false; status: 404 | 403; message: string };

export function resolveTarget(pathname: string, search: string): TargetResolution {
  // pathname всегда начинается с "/", поэтому после split("/") первый элемент —
  // всегда пустая строка, а реальные сегменты — начиная с индекса 1.
  // Пустые сегменты намеренно не фильтруем: filter(Boolean) съедал завершающий "/"
  // у путей вида "/fetch/host/api/", и запрос TikWM уходил без слеша (дорогая
  // отладка в истории проекта).
  const parts = pathname.split("/");
  if (parts.length < 3 || parts[1] !== "fetch" || parts[2] === "") {
    return { ok: false, status: 404, message: "Not found — ожидаемый формат пути: /fetch/<host>/<путь>" };
  }
  const host = parts[2];
  if (!ALLOWED_HOSTS.has(host)) {
    return { ok: false, status: 403, message: "Host not allowed" };
  }
  const path = "/" + parts.slice(3).join("/");
  if (host === "api.telegram.org" && !isTelegramBotPath(path)) {
    return { ok: false, status: 404, message: "Not found — путь Telegram обязан иметь форму /bot<token>/<метод> или /file/bot<token>/<путь>" };
  }
  if (host === "api.cloudflare.com" && !isCloudflareAiPath(path)) {
    return { ok: false, status: 404, message: "Not found — разрешён только путь /client/v4/accounts/<account>/ai/..." };
  }
  return { ok: true, url: `https://${host}${path}${search}` };
}

// Telegram Bot API без токена в пути не вызывается: без формы исчезает целый
// класс чужеродного использования (релей произвольных путей при наличии секрета).
function isTelegramBotPath(path: string): boolean {
  return /^\/bot[^/]+\/.+/.test(path) || /^\/file\/bot[^/]+\/.+/.test(path);
}

// Authorization пробрасывается наверх как есть, поэтому релей в Cloudflare закрыт
// до ветки ai: иначе у любого, кто завладел секретом прокси, появился бы весь API
// аккаунта (DNS, Workers, R2), а не только генерация картинок.
function isCloudflareAiPath(path: string): boolean {
  // Генерация и каталог моделей (каталог зовёт /diag); остальное поддерево — нет
  // (ревью 17.11.2026, проход №5: /diag без каталога краснел).
  return /^\/client\/v4\/accounts\/[^/]+\/ai\/(run|models)\//.test(path);
}

export function buildForwardHeaders(reqHeaders: Headers): Headers {
  const headers = new Headers(reqHeaders);
  for (const name of HOP_BY_HOP_REQUEST_HEADERS) headers.delete(name);
  // Тело заменено capped-стримом, исходный length врёт (рантайм выставит сам).
  headers.delete("content-length");
  headers.delete(PROXY_AUTH_HEADER);
  return headers;
}

export function buildResponseHeaders(upstreamHeaders: Headers): Headers {
  const headers = new Headers(upstreamHeaders);
  for (const name of HOP_BY_HOP_RESPONSE_HEADERS) headers.delete(name);
  return headers;
}

// ── Чтение страниц (/read) ──
// Отдельный маршрут, а не запись в ALLOWED_HOSTS: allowlist не масштабируется на
// «любой публичный сайт», а открытый релей отдал бы аккаунт под абьюз (аудит 16.11.2026).
// Правила повторяют эталон Claude: только https, только публичные имена, без
// кредов и нестандартных портов, без редиректов, только текст.
export const READ_PAGE_MAX_BYTES = 2 * 1024 * 1024;
export const READ_PAGE_TIMEOUT_MS = 15_000;
const READ_ALLOWED_TYPES = new Set(["text/html", "text/plain", "application/xhtml+xml"]);

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

export async function resolveReadTarget(
  raw: string,
  resolveDnsImpl: (name: string, type: "A" | "AAAA") => Promise<string[]> = (n, t) => Deno.resolveDns(n, t),
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
  // Резолвим оба семейства и проверяем КАЖДЫЙ адрес: имя может указать на
  // приватный адрес и без литерала (защита от подмены после резолва).
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
  // R6-04 (аудит 26.11.2026): дедлайн считается с начала запроса — раньше
  // AbortSignal.timeout создавался после DNS, и медленный резолв не ограничивался.
  const deadline = AbortSignal.timeout(readTimeoutMs);
  // R6-01: резолв здесь и резолв внутри fetchImpl — два разных ответа; честный
  // pin (fetch на проверенный IP с Host/SNI по имени) Deno fetch не умеет, а
  // ручной HTTP-клиент поверх connectTls несоразмерен задаче — окно ребиндинга
  // остаётся узким (между проверкой и коннектом) и задокументирован в отчёте.
  if (deadline.aborted) return new Response("Read timed out", { status: 504 });
  const target = await Promise.race<ReadTarget | "timeout">([
    resolveReadTarget(url.searchParams.get("url") ?? "", resolveDnsImpl),
    // Ревью 26.11.2026 (R6-04): проверка после резолва не спасает от
    // зависшего Deno.resolveDns — гонка DNS и дедлайна обязательна.
    new Promise<"timeout">((res) => deadline.addEventListener("abort", () => res("timeout"), { once: true })),
  ]);
  if (target === "timeout") return new Response("Read timed out", { status: 504 });
  if (!target.ok) return new Response(target.message, { status: target.status });
  let upstream: Response;
  try {
    upstream = await fetchImpl(target.url, {
      method: "GET",
      // Никаких куки и авторизации: страница читается как анонимный посетитель.
      headers: { "user-agent": "Lumen/1.0", "accept": "text/html,text/plain;q=0.9,*/*;q=0.1" },
      // Редирект мог бы вывести за пределы проверенного адреса.
      redirect: "error",
      signal: deadline,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "TimeoutError") {
      return new Response("Read timed out", { status: 504 });
    }
    return new Response("Read failed", { status: 502 });
  }
  const rawType = upstream.headers.get("content-type") ?? "";
  const type = rawType.split(";")[0].trim().toLowerCase();
  if (!READ_ALLOWED_TYPES.has(type)) {
    // R6-03: отказ обязан закрывать апстрим — иначе непрочитанное тело висело
    // на соединении и держало квоту Deno.
    await upstream.body?.cancel();
    return new Response("Unsupported content type", { status: 415 });
  }
  if (upstream.status !== 200) {
    await upstream.body?.cancel();
    return new Response("Upstream status " + upstream.status, { status: 502 });
  }
  // charset пробрасывается: страницы в windows-1251 иначе декодировались угадыванием.
  return new Response(
    warnOnTruncation(upstream.body, READ_PAGE_MAX_BYTES, new URL(target.url).host, "close"),
    {
      status: 200,
      headers: { "content-type": rawType || type },
    },
  );
}

// fetchImpl — точка подмены для тестов: реальная сеть юнитам не нужна.
export async function handleRequest(
  req: Request,
  fetchImpl: typeof fetch = fetch,
  proxySecret: string | undefined = undefined,
  maxBodyBytes: number = MAX_REQUEST_BODY_BYTES,
  resolveReadDnsImpl?: (name: string, type: "A" | "AAAA") => Promise<string[]>,
): Promise<Response> {
  if (
    !proxySecret || proxySecret.length < MIN_PROXY_SECRET_LENGTH ||
    !/^[\x21-\x7e]+$/.test(proxySecret)
  ) {
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
  for (let i = 0; i < expectedBytes.length; i++) {
    difference |= expectedBytes[i] ^ suppliedBytes[i];
  }
  if (!suppliedSecret || difference !== 0) {
    return new Response("Unauthorized", { status: 401 });
  }

  const url = new URL(req.url);
  if (url.pathname === "/read") {
    return await handleReadPage(req, url, fetchImpl, resolveReadDnsImpl);
  }
  const target = resolveTarget(url.pathname, url.search);
  if (!target.ok) {
    return new Response(target.message, { status: target.status });
  }

  // Кап тела запроса по объявленному content-length: без него любой, у кого есть
  // секрет, лил в прокси поток любого размера и съедал общий лимит Deno-аккаунта
  // (ровно то, от чего прокси и защищает — аудит 26.09.2026). Потоковое тело без
  // заголовка тоже капаем на лету, а не пропускаем: иначе кап обходился chunked.
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
      // redirect:"error" осознанно: проброс 3xx Location наружу вывел бы клиент
      // из allowlist, а переезд хоста у TikWM даст честный 502 ниже.
      redirect: "error",
      // GET/HEAD не могут иметь тело запроса (fetch бросит исключение, если
      // передать body для них) — для остальных методов пробрасываем тело
      // напрямую как поток, не буферизуя целиком в памяти (важно для
      // multipart file-загрузок в Telegram, см. докстринг выше).
      body: cappedRequestBody,
      signal: AbortSignal.timeout(UPSTREAM_FETCH_TIMEOUT_MS),
      // @ts-ignore — Deno требует duplex:"half" для потокового тела запроса
      // (часть стандарта WHATWG fetch для body типа ReadableStream).
      duplex: "half",
    });
  } catch (err) {
    if (err instanceof BodyTooLargeError) {
      return new Response("Request body too large", { status: 413 });
    }
    if (err instanceof DOMException && err.name === "TimeoutError") {
      return new Response("Upstream fetch timed out", { status: 504 });
    }
    return new Response("Upstream fetch failed", { status: 502 });
  }

  const respHeaders = buildResponseHeaders(upstreamResp.headers);
  // HEAD обязан ответить без тела; кап ответа оставляет след в логе.
  const cappedBody = req.method === "HEAD"
    ? null
    : warnOnTruncation(upstreamResp.body, maxBodyBytes, new URL(target.url).host);
  return new Response(cappedBody, {
    status: upstreamResp.status,
    headers: respHeaders,
  });
}

// Реальный сервер стартует только при прямом запуске файла (deno run/deploy),
// не при импорте из proxy_test.ts — иначе тесты пытались бы забиндить порт.
if (import.meta.main) {
  const proxySecret = Deno.env.get("LUMEN_PROXY_SECRET");
  Deno.serve((req) => handleRequest(req, fetch, proxySecret));
}
