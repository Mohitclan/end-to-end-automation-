# Deployment and operations

> Status: the Dockerfile and compose file are written but were **not built or run** in
> the development environment (no Docker there). The app itself was run and verified with
> SQLite + Redis 7.4; Postgres was not exercised. Treat the first deploy as a test.

## Production checklist

- `ENVIRONMENT=production`. The web app refuses to start unless `SECRET_KEY` is 32+
  random chars, `BASE_URL` is https, `SESSION_COOKIE_SECURE=true`, `COMPANY_POSTAL_ADDRESS`
  is set and `LLM_PROVIDER` is not `fake`.
- HTTPS reverse proxy in front of `web` (unsubscribe links and webhooks must be https).
  Pass the real client IP (`X-Forwarded-For`); uvicorn runs with `proxy_headers`.
- Postgres for `DATABASE_URL` (`postgresql+psycopg://…`, install with `pip install ".[postgres]"`).
- Redis 6.2+ with `maxmemory-policy noeviction` and AOF on (BullMQ jobs must not be evicted).
- `growthstack migrate` on every deploy, before starting the new processes.
- One `web` process (the login throttle is in-memory per process) and one or more `worker`
  processes (per-mailbox locks make several workers safe).
- Keep `EMAIL_DRY_RUN=true` until the mailbox, DNS and webhook checks below pass.

## With Docker Compose

```bash
cd deploy && cp ../.env.example .env
```

Edit `.env` (also add `POSTGRES_PASSWORD` and `CRAWL4AI_TOKEN`), then:

```bash
docker compose up -d --build
```

```bash
docker compose run --rm web growthstack migrate
```

```bash
docker compose run --rm web growthstack create-user you@yourdomain.com
```

Run alongside, from their own official compose files and unchanged (the document says
not to modify them): **Twenty CRM** and **listmonk**. Optional: **Postiz** for social
posts (Phase 3; it is not integrated with growthstack and needs its own developer app per
network: X charges per post, LinkedIn company posting needs app review).

## DNS for the sending domain

| Record | Purpose |
|---|---|
| SPF (`TXT`), from Resend | Authorises Resend to send for the domain |
| DKIM (`TXT`/`CNAME`), from Resend | Signs mail; must cover the List-Unsubscribe headers |
| `_dmarc` `TXT` `v=DMARC1; p=none; rua=mailto:…` | Required by Gmail/Yahoo for bulk senders |

The dashboard's *Sending domain* panel shows the daily checkdmarc result.

## Backups

- Postgres: daily `pg_dump`. This holds leads, provenance, the do-not-contact list
  (legally important: never lose opt-outs), messages and metrics.
- `DATA_DIR`: the LangGraph checkpoint file (drafts waiting for approval can still be
  decided without it) and the refreshed disposable-domain list (rebuilt weekly).
- Redis needs no backup: the dispatcher rebuilds missing jobs from the database.

## Monitoring

- Dashboard → *Data freshness*: the last run, result and message of every periodic job.
- Logs are JSON lines on stdout (both processes). Failed jobs are logged with a traceback.
- `GET /healthz` checks the database.

## Troubleshooting

| Symptom | Check |
|---|---|
| Approved emails never send | Worker running? `REDIS_URL` same for web and worker? Mailbox active and inside its window (UTC)? Lead page shows "held by mailbox limits"? |
| "mailbox busy; will retry" in logs | Another worker holds that mailbox's lock (normal under load); BullMQ retries |
| Drafts are placeholder text | `LLM_PROVIDER=fake`; set `anthropic` or `ollama` |
| Research fails with "robots.txt disallows" | The site opted out; this is intentional and is not retried with another crawler |
| Webhook returns 401 | `RESEND_WEBHOOK_SECRET` mismatch, or server clock off by more than 5 minutes |
| Search numbers end 3 days ago | Normal: Search Console data lags 2-3 days |
| A job shows `skipped` | Its integration settings are empty |
