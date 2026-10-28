"""The periodic jobs that connect integration clients to the database, run with fake
clients (the clients themselves are tested against mocked HTTP elsewhere)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from growthstack import dnc
from growthstack.db import SessionLocal
from growthstack.jobs import tasks
from growthstack.models import (
    Company,
    DncReason,
    DoNotContact,
    JobRun,
    Lead,
    LeadStage,
    PageScore,
    ProductMetric,
    SearchMetric,
)


def _patch(monkeypatch, module_path: str, cls_name: str, fake):
    import importlib

    mod = importlib.import_module(module_path)
    monkeypatch.setattr(getattr(mod, cls_name), "from_settings", classmethod(lambda cls, settings=None: fake))


def test_jobs_skip_when_not_configured(database):
    for name in ("gsc_pull", "posthog_pull", "twenty_sync", "listmonk_sync", "psi_run", "domain_check"):
        assert tasks.PERIODIC[name][0]().startswith("skipped"), name


def test_gsc_pull_upserts(database, monkeypatch):
    class Fake:
        def daily_totals(self, start, end):
            return [{"day": end - timedelta(days=i), "clicks": 3, "impressions": 100, "ctr": 0.03, "position": 9.5} for i in range(3)]

        def top_queries(self, start, end, limit=50):
            return [{"query": "free crm", "clicks": 5, "impressions": 80, "ctr": 0.06, "position": 4.2}]

    _patch(monkeypatch, "growthstack.integrations.search_console", "SearchConsoleClient", Fake())
    assert tasks.gsc_pull() == "3 days, 1 queries"
    assert tasks.gsc_pull() == "3 days, 1 queries"  # idempotent upsert
    with SessionLocal() as s:
        assert s.query(SearchMetric).filter_by(kind="daily").count() == 3
        assert s.query(SearchMetric).filter_by(kind="query").one().query == "free crm"


def test_psi_run_records_scores_and_errors(database, monkeypatch):
    monkeypatch.setenv("PSI_URLS", "https://a.example/,https://b.example/")
    from growthstack.config import get_settings

    get_settings.cache_clear()

    class Fake:
        def analyze(self, url, strategy="MOBILE"):
            if "b.example" in url:
                raise RuntimeError("PSI 500")
            return {"performance": 91.0, "seo": 100.0, "accessibility": 88.0, "best_practices": 96.0, "top_opportunities": []}

    _patch(monkeypatch, "growthstack.integrations.pagespeed", "PageSpeedClient", Fake())
    assert tasks.psi_run() == "2/4 page scores"
    with SessionLocal() as s:
        assert s.query(PageScore).filter(PageScore.error.is_not(None)).count() == 2


def test_posthog_pull_imports_signups_and_engagement(database, monkeypatch):
    with SessionLocal() as s:
        s.add(Lead(email="active@co.io", source="csv", source_detail="x"))
        s.commit()

    class Fake:
        def daily_event_counts(self, event, days):
            return {date(2026, 10, 27): 4}

        def daily_unique_visitors(self, days):
            return {date(2026, 10, 27): 120}

        def recent_signups(self, event, since):
            return [{"email": "New@Startup.io", "first_name": "Nia", "last_name": None}, {"email": None}]

        def person_event_counts(self, emails, days):
            assert days == 30
            return {e.lower(): (7 if e.lower() == "active@co.io" else 1) for e in emails}

    _patch(monkeypatch, "growthstack.integrations.posthog_client", "PostHogQueryClient", Fake())
    out = tasks.posthog_pull()
    assert "1 new signup leads" in out
    with SessionLocal() as s:
        lead = s.query(Lead).filter_by(email="new@startup.io").one()
        assert (lead.source, lead.lawful_basis, lead.stage) == ("posthog_signup", "contract", LeadStage.CONVERTED)
        assert lead.company.domain == "startup.io"
        assert s.query(Lead).filter_by(email="active@co.io").one().posthog_events_30d == 7
        assert {(m.name, m.value) for m in s.query(ProductMetric)} == {("signups", 4), ("visitors", 120)}


def test_twenty_sync_pushes_people_and_opens_opportunity(database, monkeypatch):
    with SessionLocal() as s:
        co = Company(name="Acme", domain="acme.io")
        s.add_all([
            Lead(email="a@acme.io", first_name="A", company=co, source="csv", source_detail="x"),
            Lead(email="b@acme.io", company=co, source="csv", source_detail="x", stage=LeadStage.REPLIED),
        ])
        s.commit()
    calls = []

    class Fake:
        def upsert_company(self, name, domain):
            calls.append(("company", domain))
            return "co-1"

        def upsert_person(self, email, first, last, title, company_id):
            calls.append(("person", email, company_id))
            return f"p-{email}"

        def create_opportunity(self, name, stage, company_id, person_id):
            calls.append(("opp", stage, person_id))
            return "opp-1"

    _patch(monkeypatch, "growthstack.integrations.twenty", "TwentyClient", Fake())
    assert tasks.twenty_sync() == "2 people pushed, 1 opportunities opened"
    assert calls.count(("company", "acme.io")) == 1  # company created once, reused
    assert ("opp", "SCREENING", "p-b@acme.io") in calls
    assert tasks.twenty_sync() == "0 people pushed, 0 opportunities opened"  # nothing left to do


def test_listmonk_sync_both_directions(database, monkeypatch):
    with SessionLocal() as s:
        dnc.add(s, email="optout@here.io", reason=DncReason.UNSUBSCRIBE)
        s.commit()
    pushed = []

    class Fake:
        def blocklisted_emails(self):
            return ["blocked@list.io"]

        def unsubscribed_emails(self):
            return ["optout@here.io"]  # already on our list

        def blocklist_email(self, email):
            pushed.append(email)

    _patch(monkeypatch, "growthstack.integrations.listmonk", "ListmonkClient", Fake())
    assert tasks.listmonk_sync() == "1 pulled into do-not-contact, 2 blocklisted in listmonk"
    assert set(pushed) == {"optout@here.io", "blocked@list.io"}
    with SessionLocal() as s:
        assert s.query(DoNotContact).filter_by(email="blocked@list.io").one().reason == "listmonk"
    pushed.clear()
    assert tasks.listmonk_sync().startswith("0 pulled") and not pushed


def test_companies_house_and_hunter_imports(database, monkeypatch):
    class FakeCH:
        def search_companies(self, **query):
            return [{"company_name": "Widgets Ltd", "company_number": "01234567", "sic_codes": ["62012"],
                     "registered_office_address": {"locality": "Leeds"}}]

        def active_directors(self, number):
            return [{"first_name": "Sam", "last_name": "Jones", "officer_role": "director"}]

    _patch(monkeypatch, "growthstack.leads.companies_house", "CompaniesHouseClient", FakeCH())
    assert tasks.import_companies_house({"sic_codes": ["62012"]}, imported_by="me@us.com") == {"companies": 1, "people": 1, "updated": 0}
    assert tasks.import_companies_house({}, imported_by="me@us.com")["people"] == 0  # no duplicates
    with SessionLocal() as s:
        sam = s.query(Lead).filter_by(first_name="Sam").one()
        assert sam.email is None and sam.source == "companies_house" and "01234567" in sam.source_detail
        company = sam.company
        company.domain = "widgets.co.uk"
        s.add(DoNotContact(email="blocked@widgets.co.uk", reason="manual"))
        s.commit()
        cid = company.id

    class FakeHunter:
        def domain_search(self, domain, limit=10):
            return [{"email": "sam@widgets.co.uk", "first_name": "Sam", "last_name": "Jones", "source_uris": ["https://widgets.co.uk/team"]},
                    {"email": "blocked@widgets.co.uk"}]

    _patch(monkeypatch, "growthstack.leads.hunter", "HunterClient", FakeHunter())
    assert tasks.import_hunter(cid, imported_by="me@us.com") == {"created": 1, "skipped_dnc": 1}
    with SessionLocal() as s:
        assert s.query(Lead).filter_by(email="sam@widgets.co.uk").one().source_detail == "published at https://widgets.co.uk/team"


def test_run_tracked_records_success_and_failure(database):
    assert tasks.run_tracked("demo", lambda: "fine") == "fine"

    def boom():
        raise RuntimeError("bad")

    with pytest.raises(RuntimeError):
        tasks.run_tracked("demo", boom)
    with SessionLocal() as s:
        run = s.query(JobRun).filter_by(name="demo").one()
        assert run.last_ok is False and "bad" in run.last_message


def test_dashboard_with_data(database):
    from growthstack.web import dashboard

    with SessionLocal() as s:
        today = datetime.now().date()
        for i in range(5):
            s.add(SearchMetric(kind="daily", day=today - timedelta(days=3 + i), clicks=2, impressions=50, ctr=0.04, position=8))
            s.add(ProductMetric(day=today - timedelta(days=i), name="signups", value=1))
        s.add(PageScore(url="https://a.example/", strategy="mobile", performance=72, seo=100, accessibility=95, best_practices=100))
        s.commit()
        d = dashboard.build(s)
    assert d["kpis"]["impressions_28d"] == 250 and d["kpis"]["clicks_28d"] == 10 and d["kpis"]["signups_30d"] == 5
    assert len(d["click_series"]) == 5 and d["page_scores"][0].performance == 72
