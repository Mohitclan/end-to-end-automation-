# growthstack

The app in the middle of the team's free growth stack (from *"One free stack for getting
real users"*, 28 Oct 2026). It owns the **lead pipeline**, the **outreach sequencer** and the
**shared do-not-contact list**, and calls the chosen tools around it:

| Stage | Tool | How growthstack uses it |
|---|---|---|
| 1 Find leads | CSV, Companies House API, Hunter, PostHog signups | Import with provenance recorded per lead |
| 2 Research | Crawl4AI (Firecrawl backup) + LangGraph + Claude/Ollama | Read site → summarise → score → draft → **wait for a person** |
| 3 Verify & protect | email verifier (AfterShip-style, no SMTP probing), checkdmarc | Check every address before sending; daily SPF/DMARC check |
| 4 Reach out | BullMQ (Redis) + Resend | Sequencer with per-mailbox limits, stop on reply/unsubscribe |
| 5 Get found | Search Console API, PageSpeed Insights | Daily impressions/clicks, page scores |
| 6 Manage | Twenty CRM (via its API) | People, companies, opportunities pushed in |
| 7 Measure | PostHog Cloud | Signups, visitors, per-lead engagement for scoring |

Everything is on one dashboard: leads, replies, signups, search impressions and clicks,
and page scores.

## Quick start (development)

Requires Python 3.11+ and Redis 6.2+.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
cp .env.example .env   # set SECRET_KEY; defaults are dev-safe (EMAIL_DRY_RUN=true, LLM_PROVIDER=fake)
```

```bash
.venv/bin/growthstack migrate && .venv/bin/growthstack seed-demo
```

```bash
.venv/bin/growthstack create-user you@company.com
```

Run the two processes (separate terminals):

```bash
.venv/bin/growthstack web
```

```bash
.venv/bin/growthstack worker
```

Open http://localhost:8000. With the defaults nothing is emailed (dry run) and drafts are
placeholder text from the offline fake LLM; see [docs/configuration.md](docs/configuration.md)
to connect real services.

## Commands

| Command | What it does |
|---|---|
| `growthstack migrate` | Create/upgrade database tables (Alembic) |
| `growthstack create-user EMAIL` | Add a team login or reset its password |
| `growthstack web` | Dashboard, approvals, import, unsubscribe page, webhooks |
| `growthstack worker` | BullMQ workers + job schedulers (sending, research, periodic jobs) |
| `growthstack run-job NAME` | Run one periodic job now (`gsc_pull`, `psi_run`, `posthog_pull`, ...) or `research LEAD_ID` |
| `growthstack check-config` | Report configuration problems (exit 1 if any) |
| `growthstack seed-demo` | Demo mailbox and 3-step sequence |

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/test_queue_redis.py` runs against a real Redis (`TEST_REDIS_URL`, default
`redis://127.0.0.1:6390/0`) and is skipped if none is reachable. Everything else is
offline: HTTP is mocked with respx, DNS with fake resolvers, the LLM with a fake.

## Documentation

- [docs/architecture.md](docs/architecture.md): components, data flow, outreach lifecycle, design decisions
- [docs/configuration.md](docs/configuration.md): connecting each integration
- [docs/deployment.md](docs/deployment.md): production setup, DNS, operations, troubleshooting
- [docs/requirements.md](docs/requirements.md): requirement-by-requirement status against the source document

## Layout

```
growthstack/
  config.py, db.py, models.py   settings, database, schema
  dnc.py                        the shared do-not-contact list
  scoring.py                    lead scoring
  security.py                   passwords, unsubscribe tokens, webhook signatures
  leads/                        CSV import, Companies House, Hunter
  research/crawler.py           Crawl4AI / Firecrawl / basic fetcher (robots.txt, SSRF guard)
  ai/                           LLM providers, LangGraph pipeline with human approval
  verify/                       email verifier (no SMTP), checkdmarc wrapper
  outreach/                     sequencer, sending policy, sender, delivery, reply detection
  integrations/                 Twenty, PostHog, Search Console, PageSpeed, listmonk
  jobs/                         BullMQ worker, dispatcher, periodic jobs
  web/                          FastAPI app, templates, dashboard
alembic/                        migrations
deploy/                         Dockerfile, docker-compose, Lighthouse CI for the marketing site
site-snippet/                   PostHog + cookie banner for the marketing site
```
