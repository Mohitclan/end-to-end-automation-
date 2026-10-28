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

## Full local stack with Docker (recommended)

This runs the same four services as the staging server: `web`, `worker`, `redis` and
`crawl4ai` (the Crawl4AI browser crawler). You need Docker Desktop with Compose v2. Python is
only needed to run the tests.

### 1. Get the code

```bash
git clone <repo-url> growthstack
cd growthstack
```

### 2. Create your `.env`

Copy the template, then fill in the values below. Only `SECRET_KEY` and `CRAWL4AI_TOKEN`
are needed to start.

```powershell
Copy-Item .env.example .env        # Windows PowerShell
```
```bash
cp .env.example .env               # macOS / Linux
```

Generate `SECRET_KEY` and `CRAWL4AI_TOKEN` with this command, once for each (they are
passwords for your own containers, so generate your own):

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

Set these in `.env`:

| Setting | Value |
|---|---|
| `SECRET_KEY` | the first generated value |
| `CRAWL4AI_TOKEN` | the second generated value |
| `LANGGRAPH_CHECKPOINT_PATH` | `/data/langgraph-checkpoints.sqlite` (the container path; the template default does not work in Docker) |
| `ENVIRONMENT` | `development` |
| `LLM_PROVIDER` | `fake` (keep until you have an Anthropic key) |
| `EMAIL_DRY_RUN` | `true` (nothing is sent) |

Leave every other key empty until you have a real account for it. Template lines have
comments after the `=`. Delete any comment text so the value is empty, or the comment
becomes the value. See [Troubleshooting](#troubleshooting).

`.env` is git-ignored. Never commit it or send it in chat. Share third-party keys
(Anthropic, Companies House, Hunter, PostHog, Google) only through a password manager.

### 3. Start the stack

Run these from the `deploy/` folder. The `--env-file ../.env` flag is required, because
Compose otherwise reads `deploy/.env`, which does not exist.

```bash
cd deploy
docker compose --env-file ../.env up -d --build
docker compose --env-file ../.env ps
```

All four services should show `Up`, and `crawl4ai` should show `(healthy)` after about a
minute. The first build can take several minutes.

### 4. Create the database and demo data

```bash
docker compose --env-file ../.env run --rm web growthstack migrate
docker compose --env-file ../.env run --rm web growthstack seed-demo
```

`seed-demo` adds a demo mailbox and a 3-step outreach sequence. Research fails without the
sequence, so run it once.

### 5. Create your login and sign in

```bash
docker compose --env-file ../.env run --rm web growthstack create-user you@company.com
```

It prompts for a password. Then open http://localhost:8000 and sign in.

Check the configuration. It should print no `PROBLEM` lines:

```bash
docker compose --env-file ../.env run --rm web growthstack check-config
```

### Daily commands

Run these from `deploy/`:

| Task | Command |
|---|---|
| Start | `docker compose --env-file ../.env up -d` |
| Stop (keeps data) | `docker compose --env-file ../.env down` |
| Logs | `docker compose --env-file ../.env logs -f web worker` |
| Apply code changes | `docker compose --env-file ../.env up -d --build web worker` |
| Run a periodic job now | `docker compose --env-file ../.env exec worker growthstack run-job psi_run` (also `gsc_pull`, `posthog_pull`) |
| Re-run research for a lead | `docker compose --env-file ../.env exec worker growthstack run-job research <LEAD_ID>` |

Data lives in the Docker volume `data`. `down` keeps it. Only `docker compose down -v` deletes it.

### Crawl4AI anti-bot setting

`deploy/crawl4ai-config.yml` and `deploy/docker-compose.override.yml` mount a Crawl4AI server
config with `simulate_user: false`. The requirements (S2.1) say stealth and anti-bot modes
stay off. The Crawl4AI 0.9.2 server rejects those settings when a client sends them, so
the setting lives in the server config. Do not delete these two files.

### Try the lead workflow

1. **Import:** open the **Import** page and upload a CSV. Columns `email`, `first_name`,
   `last_name`, `company` and `website` are recognised, with common alternative names. Use
   addresses you control (for example `you+acme@yourcompany.com`) so nothing reaches a stranger.
2. **Research:** on the **Leads** page, click **Research new leads**. Crawl4AI reads each
   website, respecting `robots.txt`. A lead that scores below `MIN_SCORE_TO_DRAFT` (30 by
   default) gets no draft.
3. **Approvals:** drafts appear here. With the fake LLM they contain `[DEV DRAFT - fake LLM]`.
   Nothing is sent until someone approves it, and `EMAIL_DRY_RUN=true` keeps sending off.

### Run the tests on Postgres (optional)

The default tests use SQLite. To test against Postgres, start a separate database container.
Port 5433 is used because 5432 is often taken.

```powershell
docker run -d --name gs-pg -e POSTGRES_PASSWORD=dev -p 5433:5432 postgres:16-alpine
docker exec gs-pg psql -U postgres -c "CREATE DATABASE growthstack_test;"
$env:TEST_DATABASE_URL = "postgresql+psycopg://postgres:dev@localhost:5433/growthstack_test"
.venv\Scripts\python.exe -m pytest -q
```

The tests drop and recreate tables in `TEST_DATABASE_URL`. Use a dedicated database like
`growthstack_test`, never one with data you want to keep.

### Windows note

`scripts/dev.sh` is a Bash script. On Windows, use the Docker setup above, or run it from
Git Bash. The Docker route needs no Python setup.

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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `required variable CRAWL4AI_TOKEN is missing a value` | Compose ran without `--env-file ../.env` from `deploy/` | Use the exact commands in the Docker section |
| `No such file or directory: '# path to the service account JSON...'` or a PageSpeed call with `# comma separated...` in the URL | A template comment became the value of a key in `.env` | Find them with `Select-String -Path .env -Pattern '=\s*#'` (PowerShell) and delete the text after `=` |
| Worker log: `unable to open database file` and research stays `queued` | `LANGGRAPH_CHECKPOINT_PATH` is a relative path such as `./dev-data/...` | Set it to `/data/langgraph-checkpoints.sqlite`, then `up -d --force-recreate web worker` |
| Research errors: `Crawl4AI returned HTTP 400` / `Rejected config: field 'simulate_user'` | The Crawl4AI config override is not mounted | Check that `deploy/docker-compose.override.yml` and `deploy/crawl4ai-config.yml` exist, then `up -d --force-recreate crawl4ai` |
| Research errors: `no active sequence with steps configured` | No outreach sequence exists yet | Run `growthstack seed-demo` |
| Import reports `skipped N duplicates` | Those leads already exist from an earlier import | Check the **Leads** page. Nothing is wrong |
| Approvals page is empty after research | Research is still running, or the leads scored below `MIN_SCORE_TO_DRAFT` (30) | Wait a few minutes, check the lead pages for their scores, then check the worker logs |
| Starting the test database fails because port 5432 is in use | Another local database container uses port 5432 | Use port 5433 for the test database, as above |
| Web container exits with `Refusing to start` | A production-only check failed | Run `check-config` and fix each listed setting |

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
