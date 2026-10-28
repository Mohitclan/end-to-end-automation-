# Requirements traceability

Source: *"One free stack for getting real users"* (team selection, 28 Oct 2026). Statuses:

- **Verified**: implemented, and an automated test or live run proves it.
- **Implemented**: code complete, tested only against mocks; not run against the real service.
- **Not started / Deferred**: with the reason.

"Live" means run on 28 Oct 2026 in the development environment with the real web and
worker processes, Redis 7.4.2, SQLite and real public DNS/HTTP; email was in dry-run mode.

## Step 5: what we build ourselves

| # | Requirement | Status | Evidence |
|---|---|---|---|
| B1 | Outreach sequencer: steps with delays, per-mailbox limits, auto-stop on reply/unsubscribe, on BullMQ | **Verified** | `test_sequencer.py` (22), `test_queue_redis.py` (real BullMQ: delayed follow-up, cancel + job removal, quota → `moveToDelayed`), live run (window deferral, send, unsubscribe cancelled step 2 and removed `msg-2` from Redis) |
| B2 | Lead scoring from CRM fields + website research + PostHog activity | **Verified** (logic) | `test_pipeline_scoring_import.py`; PostHog activity input is filled by `posthog_pull` (tested with a fake client) |
| B3 | One do-not-contact list for every channel; one-click unsubscribe in every email | **Verified** | RFC 8058 headers + footer tests; `/u/{token}` GET-confirm/POST-unsubscribe tests; bounce/complaint webhooks; opt-out replies; listmonk two-way sync (fake client); live unsubscribe |
| B4 | One dashboard: leads, replies, signups, search impressions/clicks, page scores | **Verified** (rendering + data assembly) | `test_web.py` renders all pages; `test_tasks.py::test_dashboard_with_data`; live render with real domain-check data. Not seen with live GSC/PostHog data (no credentials) |
| B5 | Lead import: CSV + public sources | **Verified** (CSV), **Implemented** (Companies House, Hunter) | CSV via live UI + tests; CH/Hunter via mocked HTTP and fake-client job tests |

## Stage by stage

| # | Requirement | Status | Evidence / notes |
|---|---|---|---|
| S1.1 | Import leads by CSV | **Verified** | Live upload via UI; header aliases, dedupe, DNC skip, formula-injection stripping |
| S1.2 | Capture inbound signups through PostHog | **Implemented** | `posthog_pull` (fake client test); HogQL client mocked. Needs a PostHog project |
| S1.3 | Companies House API | **Implemented** | Mocked HTTP (47 integration tests), job test |
| S1.4 | Hunter free plan top-up | **Implemented** | Mocked HTTP, job test |
| S1.5 | Public business data only; record each lead's source; tell people in the first email (GDPR) | **Verified** | `source`/`source_detail` required on every lead; disclosure in step-1 footer only (tests); the CSV form requires a source description |
| S2.1 | Crawl4AI reads the prospect's site; stealth/anti-bot off; respect robots.txt | **Implemented** (Crawl4AI), **Verified** (robots + built-in fetcher) | Request body asserts `check_robots_txt: true`, `simulate_user/magic/override_navigator: false`; live: robots.txt fetched before python.org page. No Crawl4AI server was run (no Docker) |
| S2.2 | Firecrawl as backup | **Implemented** | Fallback only on non-robots failures (mocked) |
| S2.3 | LangGraph + LLM: summarise, score, draft, stop and wait for a person before anything is sent | **Verified** | Interrupt/resume tests (approve, reject, edits, lost checkpoint); live run produced a draft awaiting approval |
| S2.4 | LLM paid per use or free locally with Ollama | **Implemented** | Claude request shape/refusal handling unit-tested with a fake client; no live Claude or Ollama call (no key / no Ollama) |
| S3.1 | checkdmarc confirms SPF/DMARC on the sending domain | **Verified** | Live check ran from the worker; result on dashboard |
| S3.2 | parsedmarc reads DMARC aggregate reports | **Not started** | Build-order Phase 3. Next step: ingest reports to the `rua` mailbox, show pass rate |
| S3.3 | Email verifier (AfterShip, not Reacher): typos, missing MX, disposable; no SMTP probing | **Verified** (deviation: Python re-implementation) | 53 tests incl. "no socket/SMTP opened"; live MX lookup. 98,543 disposable domains, refreshed weekly |
| S4.1 | Resend sends our emails | **Implemented** | Built payload + headers tested; dry-run verified live; no real Resend call (no key) |
| S4.2 | react-email templates as code | **Deferred** (deviation) | Outreach is plain text by design; react-email belongs with HTML newsletters (needs Node) |
| S4.3 | Switch to Amazon SES past the free limit | **Not started** | Document marks it *Later*; swap point `outreach/sender.py:send` |
| S4.4 | BullMQ: queue each email, safe pace per mailbox, retries, cancel on reply/unsubscribe | **Verified** | See B1; idempotency key per message; 5 attempts with exponential backoff |
| S4.5 | listmonk for opted-in newsletters, run unchanged; not for cold email | **Implemented** (DNC sync) | listmonk itself is deployed separately; sync tested with a fake client and mocked HTTP |
| S4.6 | Postiz for social posts | **Not started** (deploy-only) | Phase 3; runs as its own service, nothing to integrate |
| S5.1 | Search Console API: impressions, clicks, position, queries | **Implemented** | Fake-service tests + job test; needs a service account |
| S5.2 | Lighthouse + PSI scores on a schedule; Lighthouse CI per deploy; Unlighthouse | **Implemented** (PSI), **Provided, untested** (LHCI config) | PSI client mocked + job test; `deploy/site-ci/` for the marketing-site repo |
| S6.1 | Twenty: single list of leads/companies/deals; connect through the API; don't change Twenty | **Implemented** | People/companies/opportunities via REST (mocked); no Twenty instance was run |
| S7.1 | PostHog Cloud: sources, drop-off, retention, A/B tests, flags | **Implemented** (by PostHog) + snippet | Those features are PostHog's; growthstack reads signups/visitors/engagement |
| S7.2 | Session recording only after cookie consent; EU region if EU users | **Provided, untested** | `site-snippet/posthog-consent.html`; not run against a PostHog project |

## Non-functional

| Area | Status | Notes |
|---|---|---|
| Authentication / authorisation | **Verified** | Session login, scrypt hashes, login throttle, every page requires login (tests) |
| CSRF | **Verified** | Token on every authenticated POST (test: POST without token → 403) |
| Webhook authenticity | **Verified** | Svix HMAC, 5-minute tolerance, idempotent by `svix-id` |
| Secrets | **Implemented** | Env vars only; `.env` gitignored; keys never placed in error messages (tests in the integrations suite) |
| SSRF on crawled URLs | **Verified** (mock DNS) | Private/loopback/link-local/metadata IPs rejected on every hop; DNS-rebinding window remains (needs network egress controls) |
| Unsafe production config | **Verified** | App refuses to start (test) |
| Reliability | **Verified** | DB-first dispatch, stale-job requeue, per-mailbox locks, graceful worker shutdown (SIGTERM, exit 0 observed) |
| Postgres compatibility | **Not verified** | Code and migrations are portable, but only SQLite was run |
| Docker deployment | **Not verified** | Files written, never built (no Docker available) |
