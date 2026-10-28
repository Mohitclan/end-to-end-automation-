# Connecting the integrations

All settings are environment variables; [.env.example](../.env.example) lists every one.
An integration with empty settings is off: its periodic job reports `skipped` on the
dashboard's *Data freshness* panel, and the Setup page shows which are connected.

Run `growthstack check-config` after changes.

## Sending: Resend (Stage 4)

1. Add and verify your sending domain in Resend (it gives you the SPF and DKIM records).
2. `RESEND_API_KEY=re_...`
3. Webhook: in Resend, add `https://YOUR_BASE_URL/webhooks/resend` with events
   `email.delivered`, `email.bounced`, `email.complained`, `email.received` (if you use
   Resend inbound). Put the signing secret in `RESEND_WEBHOOK_SECRET=whsec_...`.
4. Add a mailbox under **Setup** for each sender address, with its daily cap and window.
   Start low (20-30/day per mailbox) on a new domain.
5. When everything looks right: `EMAIL_DRY_RUN=false`.

Free tier: 3,000 emails/month, 100/day. Past that the document's plan is Amazon SES
(not implemented yet; the swap point is `outreach/sender.py:send`).

## Reply detection

Either or both:
- **IMAP** (works with any mailbox): set IMAP host/user on the mailbox and put the password
  in the env var you name there (e.g. `IMAP_PASSWORD_SALES`). For Google Workspace use an
  app password. Polled every `REPLY_POLL_SECONDS`.
- **Resend inbound**: route replies to a Resend receiving address and enable the
  `email.received` webhook. This matches by sender address. The webhook carries no body, so
  "please remove me" phrases are only detected via IMAP.

## Sending domain health (Stage 3)

`SENDING_DOMAIN=yourdomain.com`. Checked daily with checkdmarc. You need SPF, DKIM (from
Resend) and a DMARC record, e.g. `_dmarc.yourdomain.com TXT "v=DMARC1; p=none; rua=mailto:dmarc@yourdomain.com"`.
Start at `p=none`; move to `quarantine` once reports look clean (parsedmarc, Phase 3, reads them).

## AI (Stage 2)

- Claude: `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...`, `LLM_MODEL=claude-opus-5-5`,
  `LLM_EFFORT=medium`. Requests use structured outputs and the server-side refusal fallback.
- Local/free: run Ollama, `LLM_PROVIDER=ollama`, `OLLAMA_MODEL=llama3.1` (any model that
  supports structured outputs).
- Tune `ICP_*`, `PRODUCT_PITCH`, `SENDER_FIRST_NAME`, `MIN_SCORE_TO_DRAFT`.

## Website research

- Crawl4AI (self-hosted, see `deploy/docker-compose.yml`): `CRAWL4AI_URL`, `CRAWL4AI_TOKEN`.
- Firecrawl backup (used only if Crawl4AI fails for a non-robots reason): `FIRECRAWL_API_KEY`.
- Neither set: a built-in fetcher (robots.txt aware, HTML only, 2 MB cap) is used; fine for
  small volumes.
- Set `CRAWLER_USER_AGENT` to something that identifies you with a contact address.

## CRM: Twenty (Stage 6)

Run Twenty from its official Docker Compose, unchanged (needs 2 GB+ RAM, Postgres, Redis).
In Twenty: Settings → APIs → create a key. Then `TWENTY_API_URL=https://crm.yourdomain.com/rest`
and `TWENTY_API_KEY=...`. Every 5 minutes new leads are pushed as People (with their
Company), and a reply opens an Opportunity at stage `SCREENING`. Twenty's API allows 100
requests/minute; the job works in batches of 50, and the client paces itself to 90
requests a minute (it waits out one 429 of up to 60 s, then leaves the rest for the next run).

The client was checked against Twenty v2.52.0's server code. Company domains are matched
on the bare host Twenty stores (`https://www.Acme.com/about` is saved as `acme.com`), and
emails in lower case, so a second run finds the same records instead of creating
duplicates. If the workspace renamed the default stages, use its real stage value in
`growthstack/jobs/tasks.py`.

Check the client against the live API (it runs every method twice on test records,
then deletes them; `--keep` leaves them):

    docker compose exec worker python -m growthstack.integrations.live_check twenty

## Analytics: PostHog Cloud (Stage 7)

Our project is on **US Cloud** (`POSTHOG_REGION=us`). To check which cloud a `phc_`
key belongs to: `https://us-assets.i.posthog.com/array/<key>/config` answers 200 for a US
key and 404 otherwise (same with `eu-assets` for EU).

1. Install [site-snippet/posthog-consent.html](../site-snippet/posthog-consent.html) on
   every page of the marketing site, just before `</body>`. It already contains the
   official loader, our project key and the US host. Before a visitor chooses there are no
   cookies and no recordings; Accept turns both on; Decline stops capturing. The choice is
   remembered. Test it locally with `site-snippet/test-page.html` (instructions inside).
2. Send `user_signed_up` from the website's **backend** after a real signup:
   [docs/posthog-signup-event.md](posthog-signup-event.md). Test without the website using
   `scripts/send_test_signup.py`.
3. For the dashboard and scoring: a personal API key (`phx_`) with **Query Read** only →
   `POSTHOG_PERSONAL_API_KEY`, plus `POSTHOG_PROJECT_ID`. `POSTHOG_SIGNUP_EVENT` must
   match the event name the site sends. Check everything with
   `.venv/bin/python scripts/check_posthog.py`; if it answers 403, also give the key
   **Person: Read**.
4. Query limits: 240 queries a minute and 2,400 an hour per project (older projects:
   120 an hour). The hourly `posthog_pull` job uses about 6 queries a run.

## Search Console + PageSpeed (Stage 5)

- Search Console: create a Google Cloud service account, enable the *Search Console API*,
  download its JSON key, and add the service account's email as a user on the property.
  `GSC_SITE_URL=sc-domain:yourdomain.com` (domain property) or `https://yourdomain.com/`,
  `GSC_SERVICE_ACCOUNT_FILE=/path/key.json`. Google's data lags 2-3 days.
- PageSpeed Insights: `PSI_URLS=https://yourdomain.com/,https://yourdomain.com/pricing`
  and `PSI_API_KEY`. The key is effectively **required**: Google's docs call it optional,
  but keyless requests currently fail with 429 (the shared anonymous quota is 0). Create
  it in Google Cloud and restrict it to the PageSpeed Insights API. Each page is scored
  mobile + desktop daily.
- Lighthouse CI on every deploy of the marketing site: copy `deploy/site-ci/` into that repo.

## Lead sources (Stage 1)

- Companies House: free key from developer.company-information.service.gov.uk →
  `COMPANIES_HOUSE_API_KEY`. Imports active companies by name/SIC/location plus current
  directors (the register has no emails).
- Hunter: `HUNTER_API_KEY` (free plan 50 credits/month). Finds public emails for a company
  domain from the Import page.

## Newsletters: listmonk (Phase 3)

Run listmonk unchanged as its own service (single binary + Postgres), using the latest
stable release (v6.2.0 at the time of writing, not a beta). Create an API user,
then `LISTMONK_URL`, `LISTMONK_API_USER`, `LISTMONK_API_TOKEN`. Hourly, every
do-not-contact address is blocklisted in listmonk, and listmonk's unsubscribes and
blocklist come back into the do-not-contact list.

In Users → User roles, create a role with exactly `subscribers:get_all`,
`subscribers:manage` and `lists:get_all`, and give it to the API user. All three are needed.
`subscribers:get_all` lets the client see subscribers on no list, such as the ones
growthstack blocklists. `lists:get_all` lets it blocklist an existing subscriber who is on
no list; without it listmonk answers `403 Permission denied: subscriber: <id>`. It doesn't
need `subscribers:sql_query`, because the client never sends a SQL `query`. To read
unsubscribes, the client pages through every subscriber and reads each one's `status` and
per-list `subscription_status`. listmonk ignores `subscription_status` unless a list is
also given, so filtering on it alone would return everyone. An address that left any list
counts as an unsubscribe.

See what the next sync would pull (read-only):

    docker compose exec worker python -m growthstack.integrations.live_check listmonk

A campaign template with the required `{{ template "content" . }}` and
`{{ UnsubscribeURL }}` tags is in
[deploy/listmonk/newsletter-template.html](../deploy/listmonk/newsletter-template.html).
