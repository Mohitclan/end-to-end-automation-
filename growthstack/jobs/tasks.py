"""Periodic jobs. Each is a plain synchronous function the worker runs in a thread;
`growthstack run-job <name>` runs one by hand. Jobs whose integration is not
configured return a 'skipped' message instead of failing."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from growthstack import dnc
from growthstack.config import get_settings
from growthstack.db import session_scope
from growthstack.models import (
    Company,
    DncReason,
    DoNotContact,
    JobRun,
    Lead,
    LeadSource,
    LeadStage,
    Mailbox,
    PageScore,
    ProductMetric,
    SearchMetric,
    utcnow,
)

log = logging.getLogger(__name__)


def _upsert_metric(db: Session, model, keys: dict, values: dict) -> None:
    row = db.scalar(select(model).filter_by(**keys))
    if row is None:
        db.add(model(**keys, **values))
    else:
        for k, v in values.items():
            setattr(row, k, v)


# --- Stage 5: get found ---------------------------------------------------------
def gsc_pull() -> str:
    from growthstack.integrations.search_console import SearchConsoleClient

    client = SearchConsoleClient.from_settings()
    if client is None:
        return "skipped: Search Console not configured"
    end = date.today() - timedelta(days=3)  # Search Console data lags 2-3 days
    start = end - timedelta(days=89)
    daily = client.daily_totals(start, end)
    queries = client.top_queries(end - timedelta(days=27), end, limit=50)
    with session_scope() as db:
        for row in daily:
            _upsert_metric(
                db,
                SearchMetric,
                {"kind": "daily", "day": row["day"], "query": None},
                {k: row[k] for k in ("clicks", "impressions", "ctr", "position")},
            )
        for row in queries:
            _upsert_metric(
                db,
                SearchMetric,
                {"kind": "query", "day": end, "query": row["query"][:500]},
                {k: row[k] for k in ("clicks", "impressions", "ctr", "position")},
            )
    return f"{len(daily)} days, {len(queries)} queries"


def psi_run() -> str:
    from growthstack.integrations.pagespeed import PageSpeedClient

    urls = get_settings().psi_url_list
    if not urls:
        return "skipped: PSI_URLS not set"
    client = PageSpeedClient.from_settings()
    done = 0
    for url in urls:
        for strategy in ("MOBILE", "DESKTOP"):
            with session_scope() as db:
                try:
                    r = client.analyze(url, strategy=strategy)
                    db.add(
                        PageScore(
                            url=url,
                            strategy=strategy.lower(),
                            performance=r["performance"],
                            seo=r["seo"],
                            accessibility=r["accessibility"],
                            best_practices=r["best_practices"],
                            top_opportunities=r["top_opportunities"],
                        )
                    )
                    done += 1
                except Exception as exc:  # one bad page must not stop the others
                    db.add(PageScore(url=url, strategy=strategy.lower(), error=str(exc)[:1000]))
    return f"{done}/{len(urls) * 2} page scores"


# --- Stage 3: verify & protect sending -------------------------------------------
def domain_check() -> str:
    from growthstack.verify.domain_check import check_domain, save_domain_check

    domain = get_settings().sending_domain
    if not domain:
        return "skipped: SENDING_DOMAIN not set"
    health = check_domain(domain)
    with session_scope() as db:
        save_domain_check(db, health)
    return f"SPF {'ok' if health.spf_valid else 'FAIL'}, DMARC {'ok' if health.dmarc_valid else 'FAIL'}"


def refresh_disposable() -> str:
    from growthstack.verify.email_verifier import refresh_disposable_list

    count = refresh_disposable_list()
    return f"{count} disposable domains" if count is not None else "refreshed"


# --- Stage 7: measure -------------------------------------------------------------
def posthog_pull() -> str:
    from growthstack.integrations.posthog_client import PostHogQueryClient

    client = PostHogQueryClient.from_settings()
    if client is None:
        return "skipped: PostHog query API not configured"
    s = get_settings()
    signups = client.daily_event_counts(s.posthog_signup_event, days=90)
    visitors = client.daily_unique_visitors(days=90)
    since = utcnow() - timedelta(days=2)
    new_signups = client.recent_signups(s.posthog_signup_event, since)
    from growthstack.leads.importer import ImportReport, get_or_create_company, normalize_domain, upsert_lead

    with session_scope() as db:
        for day, n in signups.items():
            _upsert_metric(db, ProductMetric, {"day": day, "name": "signups"}, {"value": n})
        for day, n in visitors.items():
            _upsert_metric(db, ProductMetric, {"day": day, "name": "visitors"}, {"value": n})
        report = ImportReport()
        for su in new_signups:
            if not su.get("email"):
                continue
            # Inbound signups are leads too; they asked to hear from us (contract basis).
            domain = normalize_domain(dnc.email_domain(su["email"]))
            lead = upsert_lead(
                db,
                report,
                email=su["email"],
                source=LeadSource.POSTHOG_SIGNUP,
                source_detail="signed up on our website",
                lawful_basis="contract",
                first_name=su.get("first_name"),
                last_name=su.get("last_name"),
                company=get_or_create_company(db, name=None, domain=domain) if domain else None,
            )
            # Signup events come from the browser and can be forged (the phc_ key is
            # public), so they never change a lead that is already being worked; the
            # team marks those converted from the lead page.
            if lead and lead.stage in (LeadStage.NEW, LeadStage.REJECTED):
                lead.stage = LeadStage.CONVERTED

        # Refresh engagement for leads that are still being worked.
        active = db.scalars(
            select(Lead).where(
                Lead.email.is_not(None),
                Lead.stage.not_in([LeadStage.DO_NOT_CONTACT, LeadStage.DISQUALIFIED, LeadStage.REJECTED]),
            ).limit(500)
        ).all()
        for lead in active:
            lead.posthog_events_30d = client.person_event_count(lead.email, days=30)
    return f"{len(signups)} signup days, {report.created} new signup leads, {len(active)} leads refreshed"


def rescore() -> str:
    from growthstack.scoring import apply_score

    with session_scope() as db:
        leads = db.scalars(select(Lead).where(Lead.stage != LeadStage.DO_NOT_CONTACT)).all()
        for lead in leads:
            apply_score(lead)
    return f"{len(leads)} leads rescored"


# --- Stage 6: CRM -------------------------------------------------------------
def twenty_sync(batch: int = 50) -> str:
    """Push new leads and companies into Twenty, and open an opportunity when a lead
    replies. Twenty's API allows 100 requests/minute, so this works in small batches."""
    from growthstack.integrations.twenty import TwentyClient

    client = TwentyClient.from_settings()
    if client is None:
        return "skipped: Twenty not configured"
    pushed = opps = 0
    with session_scope() as db:
        leads = db.scalars(
            select(Lead).where(Lead.twenty_person_id.is_(None), Lead.email.is_not(None)).limit(batch)
        ).all()
        for lead in leads:
            company_id = None
            if lead.company:
                if not lead.company.twenty_id:
                    lead.company.twenty_id = client.upsert_company(lead.company.name, lead.company.domain)
                company_id = lead.company.twenty_id
            lead.twenty_person_id = client.upsert_person(
                lead.email, lead.first_name, lead.last_name, lead.title, company_id
            )
            pushed += 1
        replied = db.scalars(
            select(Lead).where(
                Lead.stage == LeadStage.REPLIED,
                Lead.twenty_person_id.is_not(None),
                Lead.twenty_opportunity_id.is_(None),
            ).limit(batch)
        ).all()
        for lead in replied:
            name = f"{lead.company.name if lead.company else lead.full_name} - outreach reply"
            lead.twenty_opportunity_id = client.create_opportunity(
                name, "SCREENING", lead.company.twenty_id if lead.company else None, lead.twenty_person_id
            )
            opps += 1
    return f"{pushed} people pushed, {opps} opportunities opened"


# --- Phase 3: newsletters -----------------------------------------------------------
def listmonk_sync() -> str:
    """Keep listmonk and our do-not-contact list in step, in both directions."""
    from growthstack.integrations.listmonk import ListmonkClient

    client = ListmonkClient.from_settings()
    if client is None:
        return "skipped: listmonk not configured"
    pulled = pushed = 0
    with session_scope() as db:
        for email in set(client.blocklisted_emails()) | set(client.unsubscribed_emails()):
            if not dnc.is_blocked(db, email):
                dnc.add(db, email=email, reason=DncReason.LISTMONK, channel="newsletter")
                pulled += 1
        pending = db.scalars(
            select(DoNotContact).where(
                DoNotContact.synced_to_listmonk.is_(False), DoNotContact.email.is_not(None)
            )
        ).all()
        for entry in pending:
            client.blocklist_email(entry.email)
            entry.synced_to_listmonk = True
            pushed += 1
    return f"{pulled} pulled into do-not-contact, {pushed} blocklisted in listmonk"


# --- replies ----------------------------------------------------------------------
def poll_replies() -> str:
    from growthstack.outreach.replies import poll_mailbox

    total, errors = 0, []
    with session_scope() as db:
        mailboxes = db.scalars(select(Mailbox).where(Mailbox.imap_host.is_not(None))).all()
        ids = [m.id for m in mailboxes]
    for mailbox_id in ids:
        try:
            with session_scope() as db:
                total += poll_mailbox(db, db.get(Mailbox, mailbox_id))
        except Exception as exc:  # keep polling the other mailboxes
            errors.append(f"mailbox {mailbox_id}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return f"{total} replies from {len(ids)} mailboxes"


# --- lead sources (run on demand from the import page) ------------------------------
def import_companies_house(query: dict, imported_by: str, with_officers: bool = True) -> dict:
    from growthstack.leads.companies_house import CompaniesHouseClient
    from growthstack.leads.importer import ImportReport, get_or_create_company, upsert_lead

    client = CompaniesHouseClient.from_settings()
    if client is None:
        raise RuntimeError("COMPANIES_HOUSE_API_KEY is not set")
    companies = client.search_companies(**query)
    report = ImportReport()
    with session_scope() as db:
        for item in companies:
            addr = item.get("registered_office_address") or {}
            company = get_or_create_company(
                db,
                name=item["company_name"],
                domain=None,
                companies_house_number=item["company_number"],
                sic_codes=item.get("sic_codes") or [],
                country=addr.get("country") or "United Kingdom",
            )
            if not with_officers:
                continue
            for officer in client.active_directors(item["company_number"]):
                upsert_lead(
                    db,
                    report,
                    email=None,
                    source=LeadSource.COMPANIES_HOUSE,
                    source_detail=f"Companies House register, company {item['company_number']}",
                    imported_by=imported_by,
                    first_name=officer.get("first_name"),
                    last_name=officer.get("last_name"),
                    title=(officer.get("officer_role") or "director").replace("-", " ").title(),
                    company=company,
                    country="United Kingdom",
                )
    return {"companies": len(companies), "people": report.created, "updated": report.updated}


def import_hunter(company_id: int, imported_by: str, limit: int = 10) -> dict:
    """Find public email addresses for a company's domain (uses Hunter credits)."""
    from growthstack.leads.hunter import HunterClient
    from growthstack.leads.importer import ImportReport, upsert_lead

    client = HunterClient.from_settings()
    if client is None:
        raise RuntimeError("HUNTER_API_KEY is not set")
    with session_scope() as db:
        company = db.get(Company, company_id)
        if not company or not company.domain:
            raise RuntimeError("company has no website domain to search")
        report = ImportReport()
        for found in client.domain_search(company.domain, limit=limit):
            uri = (found.get("source_uris") or [None])[0]
            upsert_lead(
                db,
                report,
                email=found["email"],
                source=LeadSource.HUNTER,
                source_detail=f"published at {uri}" if uri else f"public web pages of {company.domain}",
                imported_by=imported_by,
                first_name=found.get("first_name"),
                last_name=found.get("last_name"),
                title=found.get("position"),
                company=company,
                website=f"https://{company.domain}",
            )
        return {"created": report.created, "skipped_dnc": report.skipped_dnc}


PERIODIC: dict[str, tuple[Callable[[], str], int]] = {
    # name: (function, interval in seconds)
    "poll_replies": (poll_replies, get_settings().reply_poll_seconds),
    "posthog_pull": (posthog_pull, 3600),
    "twenty_sync": (twenty_sync, 300),
    "listmonk_sync": (listmonk_sync, 3600),
    "gsc_pull": (gsc_pull, 6 * 3600),
    "psi_run": (psi_run, 24 * 3600),
    "domain_check": (domain_check, 24 * 3600),
    "rescore": (rescore, 24 * 3600),
    "refresh_disposable": (refresh_disposable, 7 * 24 * 3600),
}


def run_tracked(name: str, fn: Callable[[], str]) -> str:
    """Run a job and record the outcome in job_runs (shown on the dashboard)."""
    with session_scope() as db:
        run = db.scalar(select(JobRun).where(JobRun.name == name)) or JobRun(name=name)
        run.last_started_at = utcnow()
        db.add(run)
    ok, message = True, ""
    try:
        message = fn() or "ok"
        return message
    except Exception as exc:
        ok, message = False, f"{type(exc).__name__}: {exc}"
        log.error("job %s failed\n%s", name, traceback.format_exc())
        raise
    finally:
        with session_scope() as db:
            run = db.scalar(select(JobRun).where(JobRun.name == name))
            run.last_finished_at = utcnow()
            run.last_ok = ok
            run.last_message = message[:2000]
