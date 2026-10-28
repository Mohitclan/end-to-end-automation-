"""HTTP integrations, all mocked with respx (no real network calls)."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from growthstack.config import Settings
from growthstack.integrations.listmonk import ListmonkClient, ListmonkError, ListmonkRateLimitError
from growthstack.integrations.pagespeed import (
    API_URL as PSI_URL,
)
from growthstack.integrations.pagespeed import (
    PageSpeedClient,
    PageSpeedError,
    PageSpeedRateLimitError,
)
from growthstack.integrations.posthog_client import (
    PostHogError,
    PostHogQueryClient,
    PostHogRateLimitError,
)
from growthstack.integrations.search_console import (
    SearchConsoleClient,
    SearchConsoleError,
    SearchConsoleRateLimitError,
)
from growthstack.integrations.twenty import TwentyClient, TwentyError, TwentyRateLimitError, normalize_domain
from growthstack.leads.companies_house import (
    CompaniesHouseClient,
    CompaniesHouseError,
    CompaniesHouseRateLimitError,
    parse_officer_name,
)
from growthstack.leads.hunter import HunterClient, HunterError, HunterRateLimitError


def make_settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# --------------------------------------------------------------------- Twenty
TW = "https://crm.example.com/rest"


@pytest.fixture
def twenty():
    return TwentyClient(TW, "tw-secret-key")


def test_twenty_from_settings():
    assert TwentyClient.from_settings(make_settings()) is None
    assert TwentyClient.from_settings(make_settings(twenty_api_url=TW)) is None
    c = TwentyClient.from_settings(make_settings(twenty_api_url=TW + "/", twenty_api_key="k"))
    assert isinstance(c, TwentyClient) and c.api_url == TW


def test_normalize_domain():
    assert normalize_domain("Example.com") == "https://example.com"
    assert normalize_domain("http://example.com/about?x=1") == "https://example.com"
    assert normalize_domain("https://example.com/") == "https://example.com"


@respx.mock
def test_twenty_upsert_company_finds_existing(twenty):
    find = respx.get(f"{TW}/companies").respond(json={"data": {"companies": [{"id": "c-1"}]}})
    create = respx.post(f"{TW}/companies")
    assert twenty.upsert_company("Acme", "acme.com") == "c-1"
    assert not create.called
    req = find.calls.last.request
    assert req.url.params["filter"] == 'domainName.primaryLinkUrl[eq]:"https://acme.com"'
    assert req.headers["authorization"] == "Bearer tw-secret-key"


@respx.mock
def test_twenty_upsert_company_creates(twenty):
    respx.get(f"{TW}/companies").respond(json={"data": {"companies": []}})
    create = respx.post(f"{TW}/companies").respond(json={"data": {"createCompany": {"id": "c-2"}}})
    assert twenty.upsert_company("Acme", "www.acme.com/") == "c-2"
    assert json.loads(create.calls.last.request.content) == {
        "name": "Acme",
        "domainName": {"primaryLinkUrl": "https://www.acme.com"},
    }


@respx.mock
def test_twenty_upsert_company_without_domain_creates(twenty):
    find = respx.get(f"{TW}/companies")
    respx.post(f"{TW}/companies").respond(json={"data": {"createCompany": {"id": "c-3"}}})
    assert twenty.upsert_company("No Domain Ltd") == "c-3"
    assert not find.called


@respx.mock
def test_twenty_upsert_person_patches_existing(twenty):
    find = respx.get(f"{TW}/people").respond(json={"data": {"people": [{"id": "p-1"}]}})
    patch = respx.patch(f"{TW}/people/p-1").respond(json={"data": {"updatePerson": {"id": "p-1"}}})
    create = respx.post(f"{TW}/people")
    pid = twenty.upsert_person("Jane@Acme.com", "Jane", "Doe", "CTO", "c-1")
    assert pid == "p-1"
    assert not create.called
    assert find.calls.last.request.url.params["filter"] == 'emails.primaryEmail[eq]:"jane@acme.com"'
    assert json.loads(patch.calls.last.request.content) == {
        "name": {"firstName": "Jane", "lastName": "Doe"},
        "jobTitle": "CTO",
        "companyId": "c-1",
    }


@respx.mock
def test_twenty_upsert_person_creates_when_missing(twenty):
    respx.get(f"{TW}/people").respond(json={"data": {"people": []}})
    patch = respx.patch(url__regex=rf"{TW}/people/.*")
    create = respx.post(f"{TW}/people").respond(json={"data": {"createPerson": {"id": "p-9"}}})
    assert twenty.upsert_person("jane@acme.com", "Jane", "Doe", None, "c-1") == "p-9"
    assert not patch.called
    assert json.loads(create.calls.last.request.content) == {
        "name": {"firstName": "Jane", "lastName": "Doe"},
        "companyId": "c-1",
        "emails": {"primaryEmail": "jane@acme.com"},
    }


@respx.mock
def test_twenty_filter_escapes_double_quotes(twenty):
    find = respx.get(f"{TW}/people").respond(json={"data": {"people": []}})
    respx.post(f"{TW}/people").respond(json={"data": {"createPerson": {"id": "p"}}})
    twenty.upsert_person('a"b\\c@x.com')
    assert find.calls.last.request.url.params["filter"] == 'emails.primaryEmail[eq]:"a\\"b\\\\c@x.com"'


@respx.mock
def test_twenty_create_opportunity(twenty):
    route = respx.post(f"{TW}/opportunities").respond(json={"data": {"createOpportunity": {"id": "o-1"}}})
    assert twenty.create_opportunity("Acme deal", "MEETING", "c-1", "p-1") == "o-1"
    assert json.loads(route.calls.last.request.content) == {
        "name": "Acme deal",
        "stage": "MEETING",
        "companyId": "c-1",
        "pointOfContactId": "p-1",
    }


@respx.mock
def test_twenty_errors_never_include_key(twenty):
    respx.post(f"{TW}/opportunities").respond(401, json={"message": "bad token"})
    with pytest.raises(TwentyError) as ei:
        twenty.create_opportunity("x")
    assert ei.value.status_code == 401
    assert "401" in str(ei.value) and "tw-secret-key" not in str(ei.value)


@respx.mock
def test_twenty_429(twenty):
    respx.get(f"{TW}/people").respond(429, headers={"Retry-After": "30"})
    with pytest.raises(TwentyRateLimitError) as ei:
        twenty.find_person_by_email("a@b.com")
    assert ei.value.retry_after == 30.0 and ei.value.status_code == 429


@respx.mock
def test_twenty_transport_error_wrapped(twenty):
    respx.get(f"{TW}/people").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(TwentyError):
        twenty.find_person_by_email("a@b.com")


# -------------------------------------------------------------------- PostHog
PH = "https://eu.posthog.com"
PH_QUERY = f"{PH}/api/projects/123/query/"


@pytest.fixture
def posthog():
    return PostHogQueryClient(PH, "123", "phx-secret")


def _body(route) -> dict:
    return json.loads(route.calls.last.request.content)


def test_posthog_from_settings():
    assert PostHogQueryClient.from_settings(make_settings()) is None
    assert PostHogQueryClient.from_settings(make_settings(posthog_project_id="1")) is None
    us = PostHogQueryClient.from_settings(make_settings(posthog_project_id="1", posthog_personal_api_key="k"))
    assert us.host == "https://us.posthog.com"
    eu = PostHogQueryClient.from_settings(
        make_settings(posthog_project_id="1", posthog_personal_api_key="k", posthog_region="EU")
    )
    assert eu.host == "https://eu.posthog.com"


@respx.mock
def test_posthog_hogql_zips_columns(posthog):
    route = respx.post(PH_QUERY).respond(json={"columns": ["a", "b"], "results": [[1, "x"], [2, "y"]]})
    assert posthog.hogql("SELECT 1") == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer phx-secret"
    assert _body(route) == {"query": {"kind": "HogQLQuery", "query": "SELECT 1"}}


@respx.mock
def test_posthog_daily_event_counts(posthog):
    route = respx.post(PH_QUERY).respond(
        json={"columns": ["day", "n"], "results": [["2026-10-26", 4], ["2026-10-27", 7]]}
    )
    assert posthog.daily_event_counts("user_signed_up", 7) == {date(2026, 10, 26): 4, date(2026, 10, 27): 7}
    q = _body(route)["query"]
    assert q["values"] == {"event": "user_signed_up"}
    assert "{event}" in q["query"] and "toIntervalDay(7)" in q["query"]


@respx.mock
def test_posthog_daily_unique_visitors(posthog):
    route = respx.post(PH_QUERY).respond(json={"columns": ["day", "n"], "results": [["2026-10-27", 12]]})
    assert posthog.daily_unique_visitors(30) == {date(2026, 10, 27): 12}
    q = _body(route)["query"]["query"]
    assert "$pageview" in q and "DISTINCT person_id" in q


@respx.mock
def test_posthog_person_event_count_uses_placeholder_for_injection(posthog):
    evil = "x' or 1=1 --@evil.com"
    route = respx.post(PH_QUERY).respond(json={"columns": ["email", "n"], "results": [[evil, 3]]})
    assert posthog.person_event_count(evil, 14) == 3
    q = _body(route)["query"]
    # the user value never reaches the query text; it travels as a bound value
    assert evil not in q["query"] and "' or" not in q["query"].lower()
    assert "{e0}" in q["query"]
    assert q["values"] == {"e0": evil}


@respx.mock
def test_posthog_person_event_count_empty(posthog):
    respx.post(PH_QUERY).respond(json={"columns": ["email", "n"], "results": []})
    assert posthog.person_event_count("a@b.com", 7) == 0


@respx.mock
def test_posthog_person_event_counts_batches_and_ignores_case(posthog):
    route = respx.post(PH_QUERY).mock(
        side_effect=[
            httpx.Response(200, json={"columns": ["email", "n"], "results": [["a@x.io", 5], ["c@x.io", 2]]}),
            httpx.Response(200, json={"columns": ["email", "n"], "results": [["e@x.io", 1]]}),
        ]
    )
    emails = ["A@x.io", "b@x.io", "c@x.io ", "d@x.io", "e@x.io", "a@x.io", ""]
    counts = posthog.person_event_counts(emails, 30, chunk=3)
    # 5 unique addresses in chunks of 3 -> 2 queries, not one per lead
    assert route.call_count == 2
    assert counts == {"a@x.io": 5, "b@x.io": 0, "c@x.io": 2, "d@x.io": 0, "e@x.io": 1}
    first = json.loads(route.calls[0].request.content)["query"]
    assert first["values"] == {"e0": "a@x.io", "e1": "b@x.io", "e2": "c@x.io"}
    assert "IN ({e0}, {e1}, {e2})" in first["query"] and "lower(person.properties.email)" in first["query"]


def test_posthog_person_event_counts_empty_list_makes_no_call(posthog):
    assert posthog.person_event_counts([], 30) == {}


def test_posthog_days_must_be_int_and_positive(posthog):
    with pytest.raises(ValueError):
        posthog.daily_event_counts("e", "1) OR (1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        posthog.daily_unique_visitors(0)


@respx.mock
def test_posthog_recent_signups_tolerates_missing_props(posthog):
    cols = ["email", "first_name", "last_name", "name", "signed_up_at", "distinct_id"]
    route = respx.post(PH_QUERY).respond(
        json={
            "columns": cols,
            "results": [
                ["a@x.com", "Ann", "Lee", None, "2026-10-27T10:00:00Z", "d1"],
                ["b@x.com", None, None, "Bob Van Dyke", "2026-10-27T11:00:00+00:00", "d2"],
                ["c@x.com", None, None, None, "2026-10-27T12:00:00Z", "d3"],
                [None, "No", "Email", None, "2026-10-27T12:00:00Z", "d4"],
                ["A@x.com", "Dup", None, None, "2026-10-27T13:00:00Z", "d5"],
            ],
        }
    )
    rows = posthog.recent_signups("user_signed_up", datetime(2026, 10, 27, 9, 0))
    assert rows == [
        {"email": "a@x.com", "first_name": "Ann", "last_name": "Lee",
         "signed_up_at": datetime(2026, 10, 27, 10, 0), "distinct_id": "d1"},
        {"email": "b@x.com", "first_name": "Bob", "last_name": "Van Dyke",
         "signed_up_at": datetime(2026, 10, 27, 11, 0), "distinct_id": "d2"},
        {"email": "c@x.com", "first_name": None, "last_name": None,
         "signed_up_at": datetime(2026, 10, 27, 12, 0), "distinct_id": "d3"},
    ]
    q = _body(route)["query"]
    assert q["values"] == {"event": "user_signed_up", "since": "2026-10-27 09:00:00"}


@respx.mock
def test_posthog_errors(posthog):
    respx.post(PH_QUERY).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(500, text="kaboom")]
    )
    with pytest.raises(PostHogRateLimitError) as ei:
        posthog.hogql("SELECT 1")
    assert ei.value.retry_after == 5.0
    with pytest.raises(PostHogError) as ei:
        posthog.hogql("SELECT 1")
    assert ei.value.status_code == 500 and "phx-secret" not in str(ei.value)


# ------------------------------------------------------------ Search Console
class FakeService:
    def __init__(self, response=None, error=None):
        self.response = response or {}
        self.error = error
        self.calls: list[dict] = []

    def searchanalytics(self):
        return self

    def query(self, siteUrl, body):
        self.calls.append({"siteUrl": siteUrl, "body": body})
        return self

    def execute(self):
        if self.error:
            raise self.error
        return self.response


def _http_error(status: int, headers: dict | None = None):
    from googleapiclient.errors import HttpError

    resp = SimpleNamespace(status=status, reason="nope", get=dict(headers or {}).get)
    return HttpError(resp, b'{"error": {"message": "nope"}}')


def test_gsc_from_settings_not_configured():
    assert SearchConsoleClient.from_settings(make_settings()) is None
    assert SearchConsoleClient.from_settings(make_settings(gsc_site_url="sc-domain:x.com")) is None


def test_gsc_daily_totals():
    svc = FakeService(
        {
            "rows": [
                {"keys": ["2026-10-02"], "clicks": 5, "impressions": 100, "ctr": 0.05, "position": 8.2},
                {"keys": ["2026-10-01"], "clicks": 3, "impressions": 90, "ctr": 0.033, "position": 9.1},
            ]
        }
    )
    client = SearchConsoleClient("sc-domain:x.com", service=svc)
    rows = client.daily_totals(date(2026, 10, 1), date(2026, 10, 2))
    assert [r["day"] for r in rows] == [date(2026, 10, 1), date(2026, 10, 2)]
    assert rows[1] == {"day": date(2026, 10, 2), "clicks": 5, "impressions": 100, "ctr": 0.05, "position": 8.2}
    call = svc.calls[0]
    assert call["siteUrl"] == "sc-domain:x.com"
    assert call["body"] == {
        "startDate": "2026-10-01",
        "endDate": "2026-10-02",
        "dimensions": ["date"],
        "rowLimit": 2,
        "dataState": "final",
    }


def test_gsc_top_queries_and_empty():
    svc = FakeService({"rows": [{"keys": ["crm"], "clicks": 2, "impressions": 40, "ctr": 0.05, "position": 3.0}]})
    client = SearchConsoleClient("https://x.com/", service=svc)
    assert client.top_queries(date(2026, 10, 1), date(2026, 10, 20), limit=10) == [
        {"query": "crm", "clicks": 2, "impressions": 40, "ctr": 0.05, "position": 3.0}
    ]
    assert svc.calls[0]["body"]["dimensions"] == ["query"] and svc.calls[0]["body"]["rowLimit"] == 10
    assert SearchConsoleClient("x", service=FakeService({})).top_queries(date(2026, 1, 1), date(2026, 1, 2)) == []


def test_gsc_errors():
    client = SearchConsoleClient("x", service=FakeService(error=_http_error(429, {"retry-after": "60"})))
    with pytest.raises(SearchConsoleRateLimitError) as ei:
        client.top_queries(date(2026, 1, 1), date(2026, 1, 2))
    assert ei.value.retry_after == 60.0
    client = SearchConsoleClient("x", service=FakeService(error=_http_error(403)))
    with pytest.raises(SearchConsoleError) as ei:
        client.daily_totals(date(2026, 1, 1), date(2026, 1, 2))
    assert ei.value.status_code == 403
    with pytest.raises(ValueError):
        client.daily_totals(date(2026, 1, 2), date(2026, 1, 1))


# ----------------------------------------------------------------- PageSpeed
PSI_BODY = {
    "lighthouseResult": {
        "categories": {
            "performance": {"score": 0.87},
            "seo": {"score": 1},
            "accessibility": {"score": None},
            "best-practices": {"score": 0.5},
        },
        "audits": {
            "a": {"title": "Reduce unused JS", "details": {"type": "opportunity", "overallSavingsMs": 1200}},
            "b": {"title": "Serve modern images", "numericValue": 300, "details": {"type": "opportunity"}},
            "c": {"title": "No savings", "details": {"type": "opportunity", "overallSavingsMs": 0}},
            "d": {"title": "Not an opp", "numericValue": 9999, "details": {"type": "table"}},
            **{
                f"o{i}": {"title": f"Opp {i}", "details": {"type": "opportunity", "overallSavingsMs": 10 * i}}
                for i in range(1, 6)
            },
        },
    }
}


def test_psi_from_settings():
    assert PageSpeedClient.from_settings(make_settings()) is None
    assert isinstance(PageSpeedClient.from_settings(make_settings(psi_urls="https://x.com/")), PageSpeedClient)


@respx.mock
def test_psi_analyze():
    route = respx.get(PSI_URL).respond(json=PSI_BODY)
    result = PageSpeedClient(api_key="psi-key").analyze("https://x.com/", "desktop")
    assert result["performance"] == 87.0
    assert result["seo"] == 100.0
    assert result["accessibility"] is None
    assert result["best_practices"] == 50.0
    assert result["top_opportunities"] == [
        {"title": "Reduce unused JS", "savings_ms": 1200.0},
        {"title": "Serve modern images", "savings_ms": 300.0},
        {"title": "Opp 5", "savings_ms": 50.0},
        {"title": "Opp 4", "savings_ms": 40.0},
        {"title": "Opp 3", "savings_ms": 30.0},
    ]
    params = route.calls.last.request.url.params
    assert params["url"] == "https://x.com/" and params["strategy"] == "DESKTOP" and params["key"] == "psi-key"
    assert params.get_list("category") == ["PERFORMANCE", "SEO", "ACCESSIBILITY", "BEST_PRACTICES"]


@respx.mock
def test_psi_without_key_and_empty_result():
    route = respx.get(PSI_URL).respond(json={})
    result = PageSpeedClient().analyze("https://x.com/")
    assert "key" not in route.calls.last.request.url.params
    assert result == {
        "performance": None, "seo": None, "accessibility": None, "best_practices": None, "top_opportunities": []
    }


@respx.mock
def test_psi_errors_redact_key():
    respx.get(PSI_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(400, text="bad request for key=psi-key"),
        ]
    )
    client = PageSpeedClient(api_key="psi-key")
    with pytest.raises(PageSpeedRateLimitError) as ei:
        client.analyze("https://x.com/")
    assert ei.value.retry_after is None
    with pytest.raises(PageSpeedError) as ei:
        client.analyze("https://x.com/")
    assert ei.value.status_code == 400 and "psi-key" not in str(ei.value)
    with pytest.raises(ValueError):
        client.analyze("https://x.com/", "tablet")


# ------------------------------------------------------------------ listmonk
LM = "https://lists.example.com"


@pytest.fixture
def listmonk():
    return ListmonkClient(LM + "/", "api", "lm-secret")


def test_listmonk_from_settings():
    assert ListmonkClient.from_settings(make_settings()) is None
    assert ListmonkClient.from_settings(make_settings(listmonk_url=LM, listmonk_api_user="api")) is None
    c = ListmonkClient.from_settings(make_settings(listmonk_url=LM, listmonk_api_user="api", listmonk_api_token="t"))
    assert isinstance(c, ListmonkClient)


@respx.mock
def test_listmonk_blocklist_new(listmonk):
    route = respx.post(f"{LM}/api/subscribers").respond(json={"data": {"id": 1}})
    listmonk.blocklist_email("Jo@X.com")
    req = route.calls.last.request
    assert req.headers["authorization"] == "token api:lm-secret"
    assert json.loads(req.content) == {"email": "jo@x.com", "name": "jo", "status": "blocklisted", "lists": []}


@pytest.mark.parametrize(
    "status,text", [(409, "E-mail already exists."), (400, '{"message":"E-mail already exists."}')]
)
@respx.mock
def test_listmonk_blocklist_existing(listmonk, status, text):
    respx.post(f"{LM}/api/subscribers").respond(status, text=text)
    find = respx.get(f"{LM}/api/subscribers").respond(json={"data": {"results": [{"id": 42, "email": "o'neil@x.com"}]}})
    block = respx.put(f"{LM}/api/subscribers/42/blocklist").respond(json={"data": True})
    listmonk.blocklist_email("o'neil@x.com")
    assert block.called
    params = find.calls.last.request.url.params
    assert params["query"] == "subscribers.email='o''neil@x.com'"
    assert params["per_page"] == "1"


@respx.mock
def test_listmonk_blocklist_other_400_raises(listmonk):
    respx.post(f"{LM}/api/subscribers").respond(400, text="invalid email")
    with pytest.raises(ListmonkError) as ei:
        listmonk.blocklist_email("bad")
    assert ei.value.status_code == 400 and "lm-secret" not in str(ei.value)


@respx.mock
def test_listmonk_blocklist_exists_but_not_found(listmonk):
    respx.post(f"{LM}/api/subscribers").respond(409, text="already exists")
    respx.get(f"{LM}/api/subscribers").respond(json={"data": {"results": []}})
    with pytest.raises(ListmonkError):
        listmonk.blocklist_email("a@x.com")


@respx.mock
def test_listmonk_lists(listmonk):
    route = respx.get(f"{LM}/api/subscribers").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"results": [{"email": "A@x.com"}, {"email": "a@x.com"}, {"email": "b@x.com"}]}}),
            httpx.Response(200, json={"data": {"results": []}}),
        ]
    )
    assert listmonk.blocklisted_emails() == ["a@x.com", "b@x.com"]
    first = route.calls[0].request.url.params
    assert first["query"] == "subscribers.status='blocklisted'" and first["per_page"] == "all"
    assert listmonk.unsubscribed_emails() == []
    second = route.calls[1].request.url.params
    assert second["subscription_status"] == "unsubscribed" and second["per_page"] == "all"


@respx.mock
def test_listmonk_429(listmonk):
    respx.get(f"{LM}/api/subscribers").respond(429, headers={"Retry-After": "2"})
    with pytest.raises(ListmonkRateLimitError) as ei:
        listmonk.unsubscribed_emails()
    assert ei.value.retry_after == 2.0


# ----------------------------------------------------------- Companies House
CH = "https://api.company-information.service.gov.uk"


@pytest.fixture
def ch():
    return CompaniesHouseClient("ch-secret")


def test_ch_from_settings():
    assert CompaniesHouseClient.from_settings(make_settings()) is None
    assert isinstance(
        CompaniesHouseClient.from_settings(make_settings(companies_house_api_key="k")), CompaniesHouseClient
    )


@respx.mock
def test_ch_search(ch):
    route = respx.get(f"{CH}/advanced-search/companies").respond(
        json={
            "hits": 1,
            "items": [
                {
                    "company_name": "ACME LTD",
                    "company_number": "01234567",
                    "company_status": "active",
                    "date_of_creation": "2020-01-02",
                    "sic_codes": ["62012"],
                    "registered_office_address": {"address_line_1": "1 High St", "locality": "London",
                                                  "postal_code": "N1 1AA"},
                }
            ],
        }
    )
    rows = ch.search_companies(
        name_includes="acme", sic_codes=["62012", "62020"], incorporated_from=date(2019, 1, 1),
        location="London", size=9999,
    )
    assert rows == [
        {
            "company_name": "ACME LTD",
            "company_number": "01234567",
            "company_status": "active",
            "date_of_creation": "2020-01-02",
            "sic_codes": ["62012"],
            "registered_office_address": {
                "address_line_1": "1 High St", "locality": "London", "postal_code": "N1 1AA", "country": None
            },
        }
    ]
    req = route.calls.last.request
    p = req.url.params
    assert p["company_name_includes"] == "acme" and p["company_status"] == "active"
    assert p["sic_codes"] == "62012,62020" and p["incorporated_from"] == "2019-01-01"
    assert p["location"] == "London" and p["size"] == "5000" and p["start_index"] == "0"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"ch-secret:").decode()


@respx.mock
def test_ch_search_404_is_empty(ch):
    respx.get(f"{CH}/advanced-search/companies").respond(404)
    assert ch.search_companies(name_includes="zzz") == []


@respx.mock
def test_ch_errors(ch):
    respx.get(f"{CH}/advanced-search/companies").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "300"}), httpx.Response(401, text="Invalid")]
    )
    with pytest.raises(CompaniesHouseRateLimitError) as ei:
        ch.search_companies()
    assert ei.value.retry_after == 300.0
    with pytest.raises(CompaniesHouseError) as ei:
        ch.search_companies()
    assert ei.value.status_code == 401 and "ch-secret" not in str(ei.value)


@respx.mock
def test_ch_active_directors(ch):
    respx.get(f"{CH}/company/01234567/officers").respond(
        json={
            "items": [
                {"name": "SMITH, John Paul", "officer_role": "director"},
                {"name": "O'BRIEN, mary", "officer_role": "director"},
                {"name": "JONES, Old", "officer_role": "director", "resigned_on": "2021-01-01"},
                {"name": "SECRETARY CO LTD", "officer_role": "corporate-secretary"},
                {"name": "BROWN, Sue", "officer_role": "secretary"},
            ]
        }
    )
    assert ch.active_directors("01234567") == [
        {"name": "SMITH, John Paul", "first_name": "John", "last_name": "Smith", "officer_role": "director"},
        {"name": "O'BRIEN, mary", "first_name": "Mary", "last_name": "O'Brien", "officer_role": "director"},
    ]


@respx.mock
def test_ch_directors_404(ch):
    respx.get(f"{CH}/company/99999999/officers").respond(404)
    assert ch.active_directors("99999999") == []


def test_parse_officer_name():
    assert parse_officer_name("SMITH-JONES, Anne Marie") == ("Anne", "Smith-Jones")
    assert parse_officer_name("BLOGGS, Joe, Dr") == ("Joe", "Bloggs")
    assert parse_officer_name("Jane Doe") == ("Jane", "Doe")
    assert parse_officer_name("") == ("", "")


# -------------------------------------------------------------------- Hunter
HU = "https://api.hunter.io/v2/domain-search"


def test_hunter_from_settings():
    assert HunterClient.from_settings(make_settings()) is None
    assert isinstance(HunterClient.from_settings(make_settings(hunter_api_key="k")), HunterClient)


@respx.mock
def test_hunter_domain_search():
    route = respx.get(HU).respond(
        json={
            "data": {
                "organization": "Acme",
                "emails": [
                    {
                        "value": "Jane@Acme.com", "type": "personal", "confidence": 94,
                        "first_name": "Jane", "last_name": "Doe", "position": "CTO",
                        "verification": {"status": "valid"},
                        "sources": [{"uri": "https://acme.com/team"}, {"domain": "x"}],
                    },
                    {"value": "info@acme.com", "type": "generic", "confidence": 80},
                ],
            }
        }
    )
    rows = HunterClient("hu-secret").domain_search("https://www.acme.com/about")
    assert rows[0] == {
        "email": "jane@acme.com", "first_name": "Jane", "last_name": "Doe", "position": "CTO",
        "confidence": 94, "verification_status": "valid", "source_uris": ["https://acme.com/team"],
        "organization": "Acme",
    }
    assert rows[1]["verification_status"] is None and rows[1]["source_uris"] == []
    req = route.calls.last.request
    assert req.url.params["domain"] == "acme.com" and req.url.params["limit"] == "10"
    assert req.headers["x-api-key"] == "hu-secret"


@respx.mock
def test_hunter_empty_and_errors():
    respx.get(HU).mock(
        side_effect=[
            httpx.Response(200, json={"data": {"emails": [], "organization": None}}),
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(401, json={"errors": [{"details": "No user found for the API key supplied"}]}),
        ]
    )
    client = HunterClient("hu-secret")
    assert client.domain_search("acme.com") == []
    with pytest.raises(HunterRateLimitError) as ei:
        client.domain_search("acme.com")
    assert ei.value.retry_after == 1.0
    with pytest.raises(HunterError) as ei:
        client.domain_search("acme.com")
    assert ei.value.status_code == 401 and "hu-secret" not in str(ei.value)
