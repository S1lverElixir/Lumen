# Security Policy

Found a vulnerability? Please use GitHub's private vulnerability reporting
(**Security → Report a vulnerability**). If private reporting is unavailable,
contact the maintainer directly. Do not post exploit details in a public issue.

Helpful reports include: where (file and line), what an attacker could do,
and how to reproduce it in a test environment. Never send live keys or tokens.

Secrets in `.env` and environment variables are never committed (see
`.gitignore`). If a key has leaked, say so directly and it will be rotated.

Scope: the Telegram bot process, the admin endpoints on the Hugging Face Space
(`/admin_keys`, `/export_state`, `/devtrace`, `/webhook_url`, `/diag` — never
call them from outside; they expose keys or chat content when authorized) and
the Deno relay. A leaked `LUMEN_PROXY_SECRET` grants authenticated relay access
and can enable abuse of its allowlisted upstreams; it does not by itself reveal
the Telegram bot token, which the caller still supplies. Rotate it on both relay
projects and the Space together. The relay's `/read` route is an SSRF surface on
purpose: it accepts public https pages only, checks every resolved address and
follows no redirects; server-side request forgery or a redirect-based bypass is
exactly the class of report wanted here. Known residual risk: the check resolves
the name and the connection resolves it again, so a domain that flips its DNS
answer between the two lookups (rebinding, TTL 0) could still reach an internal
address from the relay; reports that turn this into a working bypass are wanted.
