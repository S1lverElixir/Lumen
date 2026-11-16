# Security Policy

Found a vulnerability? Please report it privately — open a GitHub issue
without exploit details in the public text, or message the bot owner directly.

Helpful reports include: where (file and line), what an attacker could do,
and how to reproduce it in a test environment. Never send live keys or tokens.

Secrets in `.env` and environment variables are never committed (see
`.gitignore`). If a key has leaked, say so directly and it will be rotated.

Scope: the Telegram bot process, the admin endpoints on the Hugging Face Space
(`/admin_keys`, `/export_state`, `/devtrace`, `/webhook_url`, `/diag` — never
call them from outside; they expose keys or chat content when authorized) and
the Deno relay. A leaked `LUMEN_PROXY_SECRET` is equivalent to full access to the
bot's Telegram API through the relay (the token rides in the request path), so it
must be rotated on both relay projects and the Space together. The relay's
`/read` route is an SSRF surface on purpose: it accepts public https pages only,
checks every resolved address and follows no redirects; server-side request
forgery or a redirect-based bypass is exactly the class of report wanted here.
