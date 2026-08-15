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
 * bot.py дальше сам достраивает нужные пути (/bot<token>/<method>,
 * /file/bot<token>/<path>, /api/?url=...) поверх этой базы — никаких других
 * изменений в Python-коде для смены адреса прокси не требуется.
 */

export const ALLOWED_HOSTS = new Set([
  "api.telegram.org",
  "www.tikwm.com",
  "tikwm.com",
]);

// Заголовки, которые нельзя слепо пробрасывать дальше как есть — Host/Connection
// в запросе относятся к соединению с ЭТИМ (Deno) сервером, а не с реальным
// апстримом, апстрим сам выставит правильные. Content-Encoding/Content-Length в
// ОТВЕТЕ — fetch() в Deno уже сам распаковывает gzip/br к моменту, когда тело
// становится нам доступно, поэтому исходный Content-Encoding больше не описывает
// реальное тело — клиент, попытавшийся распаковать уже распакованное, получил бы
// битые данные. Content-Length по той же причине может не совпадать с реальным
// размером — рантайм сам выставит корректный Transfer-Encoding для стрима.
const HOP_BY_HOP_REQUEST_HEADERS = ["host", "connection"];
const HOP_BY_HOP_RESPONSE_HEADERS = ["content-encoding", "content-length", "connection", "transfer-encoding"];

// Разложено на чистые, независимо тестируемые функции (resolveTarget/
// buildForwardHeaders/buildResponseHeaders) вместо одного большого обработчика —
// тот же принцип, что и в остальном проекте Lumen (см. lumen_tiktok.py и др.):
// маршрутизацию и фильтрацию заголовков можно проверить юнит-тестами без единого
// реального сетевого вызова, а сама сетевая часть (handleRequest) тестируется
// отдельно через подмену fetch.

export type TargetResolution =
  | { ok: true; host: string; url: string }
  | { ok: false; status: 404 | 403; message: string };

export function resolveTarget(pathname: string, search: string): TargetResolution {
  // pathname всегда начинается с "/", поэтому после split("/") первый элемент —
  // всегда пустая строка, а реальные сегменты — начиная с индекса 1. Намеренно
  // НЕ фильтруем пустые сегменты через .filter(Boolean) (как было в первой
  // версии) — НАЙДЕНО ПРИ ТЕСТИРОВАНИИ: filter(Boolean) съедал завершающий "/"
  // у путей вида "/fetch/host/api/" (пустой хвостовой сегмент после join
  // как раз и восстанавливает эту же завершающую "/"), из-за чего запрос
  // TikWM вида ".../api/?url=..." ушёл бы к апстриму как ".../api?url=..."
  // без слеша — ровно тот класс "почти правильного, но не совсем" URL, из-за
  // которого уже был потрачен не один час отладки в этом проекте.
  const parts = pathname.split("/");
  if (parts.length < 3 || parts[1] !== "fetch" || parts[2] === "") {
    return { ok: false, status: 404, message: "Not found — ожидаемый формат пути: /fetch/<host>/<путь>" };
  }
  const host = parts[2];
  if (!ALLOWED_HOSTS.has(host)) {
    return { ok: false, status: 403, message: "Host not allowed" };
  }
  const path = "/" + parts.slice(3).join("/");
  return { ok: true, host, url: `https://${host}${path}${search}` };
}

export function buildForwardHeaders(reqHeaders: Headers): Headers {
  const headers = new Headers(reqHeaders);
  for (const name of HOP_BY_HOP_REQUEST_HEADERS) headers.delete(name);
  return headers;
}

export function buildResponseHeaders(upstreamHeaders: Headers): Headers {
  const headers = new Headers(upstreamHeaders);
  for (const name of HOP_BY_HOP_RESPONSE_HEADERS) headers.delete(name);
  return headers;
}

// fetchImpl — точка подмены для тестов (тот же приём, что bot._get_http_session
// и т.п. в Python-части проекта) — реальная сеть не нужна ни одному юнит-тесту.
export async function handleRequest(req: Request, fetchImpl: typeof fetch = fetch): Promise<Response> {
  const url = new URL(req.url);
  const target = resolveTarget(url.pathname, url.search);
  if (!target.ok) {
    return new Response(target.message, { status: target.status });
  }

  const forwardHeaders = buildForwardHeaders(req.headers);
  let upstreamResp: Response;
  try {
    upstreamResp = await fetchImpl(target.url, {
      method: req.method,
      headers: forwardHeaders,
      // GET/HEAD не могут иметь тело запроса (fetch бросит исключение, если
      // передать body для них) — для остальных методов пробрасываем тело
      // напрямую как поток, не буферизуя целиком в памяти (важно для
      // multipart file-загрузок в Telegram, см. докстринг выше).
      body: (req.method === "GET" || req.method === "HEAD") ? undefined : req.body,
      // @ts-ignore — Deno требует duplex:"half" для потокового тела запроса
      // (часть стандарта WHATWG fetch для body типа ReadableStream).
      duplex: "half",
    });
  } catch (e) {
    return new Response(`Upstream fetch failed: ${e}`, { status: 502 });
  }

  const respHeaders = buildResponseHeaders(upstreamResp.headers);
  return new Response(upstreamResp.body, {
    status: upstreamResp.status,
    headers: respHeaders,
  });
}

// Реальный сервер стартует только при прямом запуске файла (deno run/deploy),
// не при импорте из proxy_test.ts — иначе тесты пытались бы забиндить порт.
if (import.meta.main) {
  Deno.serve((req) => handleRequest(req));
}
