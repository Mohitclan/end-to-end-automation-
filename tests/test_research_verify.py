from __future__ import annotations

import json
import socket

import dns.exception
import dns.resolver
import httpx
import pytest
import respx

from growthstack.config import Settings
from growthstack.research import crawler as cr
from growthstack.research.crawler import (
    BasicFetcher,
    Crawl4AIClient,
    CrawlError,
    FirecrawlClient,
    ResearchResult,
    RobotsDisallowed,
    UnsafeURL,
    fetch_site_text,
    get_crawler,
    html_to_text,
    normalize_url,
)
from growthstack.verify import email_verifier as ev
from growthstack.verify.domain_check import (
    WARN_NO_DMARC,
    WARN_P_NONE,
    DomainHealth,
    check_domain,
    save_domain_check,
)
from growthstack.verify.email_verifier import verify_email

PUBLIC_IP = "93.184.215.14"
UA = "growthstack-test/1.0"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every hostname resolves to a public address unless a test says otherwise."""
    table: dict[str, str] = {}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        ip = table.get(host, PUBLIC_IP)
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(cr.socket, "getaddrinfo", fake_getaddrinfo)
    return table


def settings(**kw) -> Settings:
    base = dict(crawl4ai_url="", firecrawl_api_key=None, crawler_user_agent=UA, research_max_chars=12000)
    base.update(kw)
    return Settings(_env_file=None, **base)


def crawl4ai_ok(markdown, title="Acme", url="https://acme.example/"):
    return {
        "success": True,
        "results": [
            {
                "url": url,
                "success": True,
                "status_code": 200,
                "error_message": "",
                "markdown": markdown,
                "metadata": {"title": title},
            }
        ],
    }


# --------------------------------------------------------------------- URLs


def test_normalize_url():
    assert normalize_url("  acme.example ") == "https://acme.example/"
    assert normalize_url("http://Acme.example/about?x=1") == "http://Acme.example/about?x=1"
    for bad in ["", "   ", "ftp://acme.example", "not a url", "localhost", "javascript:alert(1)", "https://user:pw@acme.example"]:
        with pytest.raises(CrawlError):
            normalize_url(bad)


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://10.0.0.5"])
def test_ssrf_ip_literals_rejected_for_every_provider(url):
    providers = [
        BasicFetcher(UA),
        Crawl4AIClient("http://crawl4ai:11235"),
        FirecrawlClient("fc-test", user_agent=UA),
    ]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route()  # nothing should go out at all
        for provider in providers:
            with pytest.raises(UnsafeURL):
                provider.fetch(url)
        with pytest.raises(UnsafeURL):
            fetch_site_text(url, settings(crawl4ai_url="http://crawl4ai:11235", firecrawl_api_key="fc-test"))
        assert not route.called


def test_ssrf_hostname_resolving_to_private_ip(public_dns):
    public_dns["internal.acme.example"] = "192.168.1.10"
    public_dns["meta.acme.example"] = "169.254.169.254"
    public_dns["v6.acme.example"] = "::ffff:127.0.0.1"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route()
        for host in ("internal.acme.example", "meta.acme.example", "v6.acme.example"):
            with pytest.raises(UnsafeURL):
                BasicFetcher(UA).fetch(f"https://{host}/")
            with pytest.raises(UnsafeURL):
                Crawl4AIClient("http://crawl4ai:11235").fetch(host)
        assert not route.called


@respx.mock
def test_basic_fetcher_refuses_redirect_to_private_ip(public_dns):
    public_dns["evil.example"] = "127.0.0.1"
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get("https://acme.example/").respond(302, headers={"location": "https://evil.example/admin"})
    evil = respx.get("https://evil.example/admin").respond(200, text="secret")
    with pytest.raises(UnsafeURL):
        BasicFetcher(UA).fetch("https://acme.example/")
    assert not evil.called


# ------------------------------------------------------------------ Crawl4AI


@respx.mock
def test_crawl4ai_request_body_and_fit_markdown():
    route = respx.post("http://crawl4ai:11235/crawl").respond(
        json=crawl4ai_ok({"raw_markdown": "raw text", "fit_markdown": "# Acme\n\nWe make widgets."})
    )
    result = Crawl4AIClient("http://crawl4ai:11235/", token="tok-123").fetch("acme.example")
    assert result == ResearchResult(
        url="https://acme.example/", text="# Acme\n\nWe make widgets.", provider="crawl4ai", title="Acme"
    )
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok-123"
    body = json.loads(request.content)
    assert body["urls"] == ["https://acme.example/"]
    assert body["browser_config"] == {"type": "BrowserConfig", "params": {"headless": True, "enable_stealth": False}}
    params = body["crawler_config"]["params"]
    assert body["crawler_config"]["type"] == "CrawlerRunConfig"
    assert params["check_robots_txt"] is True
    assert params["simulate_user"] is False
    assert params["magic"] is False
    assert params["override_navigator"] is False
    assert params["cache_mode"] == "bypass"


@respx.mock
def test_crawl4ai_raw_markdown_and_plain_string_and_no_token():
    route = respx.post("http://crawl4ai:11235/crawl")
    route.respond(json=crawl4ai_ok({"raw_markdown": "raw only", "fit_markdown": ""}))
    assert Crawl4AIClient("http://crawl4ai:11235").fetch("https://acme.example").text == "raw only"
    assert "Authorization" not in route.calls.last.request.headers
    route.respond(json=crawl4ai_ok("plain string markdown"))
    assert Crawl4AIClient("http://crawl4ai:11235").fetch("https://acme.example").text == "plain string markdown"


@respx.mock
def test_crawl4ai_robots_disallowed():
    respx.post("http://crawl4ai:11235/crawl").respond(
        json={
            "success": True,
            "results": [
                {
                    "url": "https://acme.example/",
                    "success": False,
                    "status_code": 403,
                    "error_message": "Access denied by robots.txt",
                    "markdown": None,
                    "metadata": {},
                }
            ],
        }
    )
    with pytest.raises(RobotsDisallowed):
        Crawl4AIClient("http://crawl4ai:11235").fetch("https://acme.example")


@respx.mock
def test_crawl4ai_failure_falls_back_to_firecrawl():
    respx.post("http://crawl4ai:11235/crawl").respond(500)
    respx.get("https://acme.example/robots.txt").respond(200, text="User-agent: *\nAllow: /\n")
    fc = respx.post("https://api.firecrawl.dev/v2/scrape").respond(
        json={
            "success": True,
            "data": {"markdown": "x" * 50, "metadata": {"title": "Acme Inc", "sourceURL": "https://acme.example/", "statusCode": 200}},
        }
    )
    s = settings(crawl4ai_url="http://crawl4ai:11235", firecrawl_api_key="fc-secret", research_max_chars=10)
    result = fetch_site_text("acme.example", s)
    assert result.provider == "firecrawl"
    assert result.title == "Acme Inc"
    assert result.text == "x" * 10  # truncated to research_max_chars
    req = fc.calls.last.request
    assert req.headers["Authorization"] == "Bearer fc-secret"
    assert json.loads(req.content) == {"url": "https://acme.example/", "formats": ["markdown"], "onlyMainContent": True}


@respx.mock
def test_no_fallback_when_robots_disallowed():
    respx.post("http://crawl4ai:11235/crawl").respond(
        json={"success": True, "results": [{"url": "u", "success": False, "error_message": "Blocked by robots.txt"}]}
    )
    fc = respx.post("https://api.firecrawl.dev/v2/scrape").respond(json={"success": True, "data": {"markdown": "x"}})
    s = settings(crawl4ai_url="http://crawl4ai:11235", firecrawl_api_key="fc-secret")
    with pytest.raises(RobotsDisallowed):
        fetch_site_text("acme.example", s)
    assert not fc.called


@respx.mock
def test_crawl4ai_failure_without_firecrawl_raises():
    respx.post("http://crawl4ai:11235/crawl").mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(CrawlError):
        fetch_site_text("acme.example", settings(crawl4ai_url="http://crawl4ai:11235"))


@respx.mock
def test_firecrawl_checks_robots_first():
    respx.get("https://acme.example/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    fc = respx.post("https://api.firecrawl.dev/v2/scrape").respond(json={"success": True, "data": {"markdown": "x"}})
    with pytest.raises(RobotsDisallowed):
        FirecrawlClient("fc-secret", user_agent=UA).fetch("https://acme.example/")
    assert not fc.called


def test_get_crawler_selection():
    assert isinstance(get_crawler(settings(crawl4ai_url="http://c:1", firecrawl_api_key="fc-x")), Crawl4AIClient)
    assert isinstance(get_crawler(settings(firecrawl_api_key="fc-x")), FirecrawlClient)
    assert isinstance(get_crawler(settings()), BasicFetcher)


# -------------------------------------------------------------- BasicFetcher

PAGE = """<!doctype html><html><head><title> Acme  Widgets </title>
<style>body{color:red}</style><script>var x = "hidden";</script></head>
<body><nav><a href="/">Home</a> <a href="/about">About</a></nav>
<svg><text>svg text</text></svg>
<main><h1>We build   widgets</h1><p>Since 2010 we have shipped
widgets to <b>500</b> teams.</p><p>Second&nbsp;paragraph &amp; more.</p>
<ul><li>Fast</li><li>Cheap</li></ul><noscript>enable js</noscript></main>
<footer>Copyright Acme</footer></body></html>"""


def test_html_to_text():
    text, title = html_to_text(PAGE)
    assert title == "Acme Widgets"
    assert text == (
        "# We build widgets\n\nSince 2010 we have shipped widgets to 500 teams.\n\n"
        "Second paragraph & more.\n\n- Fast\n- Cheap"
    )
    for hidden in ("hidden", "color:red", "Home", "svg text", "enable js", "Copyright"):
        assert hidden not in text


@respx.mock
def test_basic_fetcher_success():
    robots = respx.get("https://acme.example/robots.txt").respond(200, text="User-agent: *\nDisallow: /private\n")
    page = respx.get("https://acme.example/").respond(200, html=PAGE)
    result = BasicFetcher(UA).fetch("acme.example")
    assert result.provider == "basic"
    assert result.title == "Acme Widgets"
    assert result.text.startswith("# We build widgets")
    assert robots.called and page.calls.last.request.headers["User-Agent"] == UA


@respx.mock
def test_basic_fetcher_robots_rules():
    respx.get("https://acme.example/robots.txt").respond(
        200, text="User-agent: growthstack-test\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    )
    page = respx.get("https://acme.example/").respond(200, html=PAGE)
    with pytest.raises(RobotsDisallowed):
        BasicFetcher(UA).fetch("https://acme.example/")
    assert not page.called


@respx.mock
def test_basic_fetcher_robots_status_handling():
    robots = respx.get("https://acme.example/robots.txt")
    page = respx.get("https://acme.example/").respond(200, html=PAGE)
    robots.respond(404)  # no robots.txt: allow all
    assert BasicFetcher(UA).fetch("https://acme.example/").text
    robots.respond(503)  # server error: stay away
    with pytest.raises(RobotsDisallowed):
        BasicFetcher(UA).fetch("https://acme.example/")
    robots.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(RobotsDisallowed):
        BasicFetcher(UA).fetch("https://acme.example/")
    assert page.call_count == 1


@respx.mock
def test_basic_fetcher_rejects_non_html_and_errors():
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get("https://acme.example/file.pdf").respond(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    respx.get("https://acme.example/missing").respond(404, html="<p>nope</p>")
    with pytest.raises(CrawlError):
        BasicFetcher(UA).fetch("https://acme.example/file.pdf")
    with pytest.raises(CrawlError):
        BasicFetcher(UA).fetch("https://acme.example/missing")


@respx.mock
def test_basic_fetcher_follows_redirects_and_caps_size(monkeypatch):
    monkeypatch.setattr(cr, "MAX_PAGE_BYTES", 200)
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get("https://acme.example/").respond(301, headers={"location": "/home"})
    respx.get("https://acme.example/home").respond(200, html="<p>" + "word " * 1000 + "</p>")
    result = BasicFetcher(UA).fetch("https://acme.example/")
    assert result.url == "https://acme.example/home"
    assert len(result.text) <= 200


@respx.mock
def test_fetch_site_text_basic_default():
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get("https://acme.example/").respond(200, html=PAGE)
    result = fetch_site_text("acme.example", settings(research_max_chars=20))
    assert result.provider == "basic" and len(result.text) <= 20


# ------------------------------------------------------------ email verifier


class MX:
    def __init__(self, exchange: str):
        self.exchange = exchange
        self.preference = 10


class FakeResolver:
    def __init__(self, answers=None, error: Exception | None = None):
        self.answers = answers or {}
        self.error = error
        self.calls: list[tuple[str, str, float | None]] = []

    def resolve(self, name, rdtype, lifetime=None):
        self.calls.append((name, rdtype, lifetime))
        if self.error:
            raise self.error
        if name not in self.answers:
            raise dns.resolver.NXDOMAIN()
        return self.answers[name]


def test_valid_email_with_mx():
    resolver = FakeResolver({"acme.example": [MX("mx1.acme.example.")]})
    r = verify_email("Jane.Doe@ACME.example", resolver=resolver)
    assert r.status == "valid" and r.has_mx is True and r.syntax_valid
    assert r.email == "Jane.Doe@acme.example"
    assert resolver.calls == [("acme.example", "MX", 5.0)]
    d = r.to_dict()
    assert d["status"] == "valid"
    assert json.loads(json.dumps(d)) == d  # JSON-serialisable for the JSON column


def test_free_provider_is_valid_but_flagged():
    r = verify_email("jane@gmail.com", resolver=FakeResolver({"gmail.com": [MX("gmail-smtp-in.l.google.com.")]}))
    assert r.status == "valid" and r.free_provider and "free_provider" in r.reasons
    assert "gmail.com" in ev.free_provider_domains()
    assert {"googlemail.com", "outlook.com", "proton.me", "btinternet.com"} <= ev.free_provider_domains()


def test_nxdomain_and_no_answer_invalid():
    r = verify_email("jane@nowhere.example", resolver=FakeResolver())
    assert r.status == "invalid" and r.has_mx is False and "no_mx" in r.reasons
    r = verify_email("jane@acme.example", resolver=FakeResolver(error=dns.resolver.NoAnswer()))
    assert r.status == "invalid" and r.has_mx is False


def test_null_mx_is_invalid():
    r = verify_email("jane@acme.example", resolver=FakeResolver({"acme.example": [MX(".")]}))
    assert r.status == "invalid" and r.has_mx is False


def test_timeout_is_risky():
    r = verify_email("jane@acme.example", resolver=FakeResolver(error=dns.exception.Timeout()))
    assert r.status == "risky" and r.has_mx is None and "mx_lookup_failed" in r.reasons
    r = verify_email("jane@acme.example", resolver=FakeResolver(error=dns.resolver.NoNameservers()))
    assert r.status == "risky" and r.has_mx is None


def test_disposable_is_invalid_and_skips_mx():
    resolver = FakeResolver({"mailinator.com": [MX("mx.mailinator.com.")]})
    r = verify_email("someone@mailinator.com", resolver=resolver)
    assert r.status == "invalid" and r.disposable and "disposable" in r.reasons
    assert resolver.calls == []  # stopped before DNS
    r = verify_email("someone@sub.mailinator.com", resolver=resolver)  # parent domain listed
    assert r.disposable and r.status == "invalid"


def test_role_account_is_risky():
    resolver = FakeResolver({"acme.example": [MX("mx.acme.example.")]})
    for local in ("info", "Sales", "support", "noreply", "admin+billing"):
        r = verify_email(f"{local}@acme.example", resolver=resolver)
        assert r.status == "risky" and r.role_account, local


def test_typo_suggestion(monkeypatch):
    # gmial.com is itself on the disposable list (typo squatters), so it is invalid
    # but still gets the suggestion
    r = verify_email("jane@gmial.com", resolver=FakeResolver())
    assert r.suggestion == "jane@gmail.com" and r.status == "invalid"
    # with the domain off the disposable list, a typo with MX records is risky
    monkeypatch.setattr(ev, "disposable_domains", lambda: frozenset())
    r = verify_email("jane@gmial.com", resolver=FakeResolver({"gmial.com": [MX("mx.gmial.com.")]}))
    assert r.status == "risky" and r.suggestion == "jane@gmail.com" and "possible_typo" in r.reasons
    assert ev.similarity("gmial.com", "gmail.com") >= 0.82
    assert verify_email("jane@gmail.com", check_mx=False).suggestion is None
    assert verify_email("jane@aon.com", check_mx=False).suggestion is None  # short domains: no guess


@pytest.mark.parametrize(
    "email",
    [
        "",
        "plainaddress",
        "a@b",
        "two@@acme.example",
        "jane@acme",
        ".jane@acme.example",
        "jane.@acme.example",
        "ja..ne@acme.example",
        "jane@-acme.example",
        "jane@acme..example",
        "jane doe@acme.example",
        "jane@acme.123",
        ("x" * 65) + "@acme.example",
        "jane@" + ("a" * 63 + ".") * 4 + "com",
    ],
)
def test_bad_syntax(email):
    resolver = FakeResolver()
    r = verify_email(email, resolver=resolver)
    assert r.status == "invalid" and not r.syntax_valid and r.reasons == ["invalid_syntax"]
    assert resolver.calls == []


def test_check_mx_false_skips_dns():
    r = verify_email("jane@acme.example", check_mx=False)
    assert r.status == "valid" and r.has_mx is None


def test_no_smtp_connections(monkeypatch):
    import smtplib

    def boom(*a, **k):
        raise AssertionError("SMTP must never be used")

    monkeypatch.setattr(smtplib.SMTP, "__init__", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    verify_email("jane@acme.example", resolver=FakeResolver({"acme.example": [MX("mx.acme.example.")]}))


def test_data_lists_loaded():
    assert len(ev.disposable_domains()) > 10000
    assert {"admin", "info", "sales", "support", "noreply", "postmaster"} <= ev.role_accounts()
    assert not (ev.disposable_domains() & ev.free_provider_domains())


@respx.mock
def test_refresh_disposable_list(tmp_path):
    good = "\n".join(f"throwaway{i}.example" for i in range(1200)) + "\nNOT A DOMAIN\n# comment\n"
    respx.get(ev.DISPOSABLE_LIST_URL).respond(200, text=good)
    target = tmp_path / "disposable.txt"
    assert ev.refresh_disposable_list(target) == 1200
    lines = target.read_text().splitlines()
    assert len(lines) == 1200 and lines == sorted(lines)

    respx.get(ev.DISPOSABLE_LIST_URL).respond(200, text="tiny.example\n")
    with pytest.raises(ValueError):
        ev.refresh_disposable_list(target)
    assert len(target.read_text().splitlines()) == 1200  # old list kept


# -------------------------------------------------------------- domain check


def fake_checker(result):
    calls = []

    def checker(domain, timeout):
        calls.append((domain, timeout))
        if isinstance(result, Exception):
            raise result
        return result

    checker.calls = calls
    return checker


def test_domain_check_healthy_with_p_none():
    checker = fake_checker(
        {
            "domain": "send.acme.example",
            "spf": {"record": "v=spf1 include:amazonses.com ~all", "valid": True, "warnings": []},
            "dmarc": {
                "record": "v=DMARC1; p=none; rua=mailto:d@acme.example",
                "location": "acme.example",
                "valid": True,
                "warnings": ["rua address has no MX"],
                "tags": {"v": {"value": "DMARC1", "explicit": True}, "p": {"value": "none", "explicit": True}},
            },
        }
    )
    h = check_domain(" Send.Acme.Example. ", timeout=3.0, checker=checker)
    assert checker.calls == [("send.acme.example", 3.0)]
    assert h.spf_valid and h.dmarc_valid
    assert h.spf_record.startswith("v=spf1")
    assert h.dmarc_policy == "none"
    assert WARN_P_NONE in h.warnings
    assert "DMARC: rua address has no MX" in h.warnings


def test_domain_check_missing_dmarc_and_bad_spf():
    checker = fake_checker(
        {
            "spf": {"record": None, "valid": False, "error": "An SPF record does not exist.", "warnings": []},
            "dmarc": {"record": None, "location": None, "valid": False, "error": "A DMARC record does not exist", "warnings": []},
        }
    )
    h = check_domain("acme.example", checker=checker)
    assert not h.spf_valid and not h.dmarc_valid and h.dmarc_policy is None
    assert WARN_NO_DMARC in h.warnings
    assert "SPF: An SPF record does not exist." in h.warnings


def test_domain_check_reject_policy_no_p_none_warning():
    checker = fake_checker(
        {
            "spf": {"record": "v=spf1 -all", "valid": True, "warnings": []},
            "dmarc": {"record": "v=DMARC1; p=reject", "valid": True, "warnings": [], "tags": {"p": {"value": "reject"}}},
        }
    )
    h = check_domain("acme.example", checker=checker)
    assert h.dmarc_policy == "reject" and h.warnings == []


def test_domain_check_checker_exception():
    h = check_domain("acme.example", checker=fake_checker(RuntimeError("dns exploded")))
    assert not h.spf_valid and not h.dmarc_valid
    assert any("dns exploded" in w for w in h.warnings)


def test_save_domain_check_persists():
    from growthstack import db

    db.configure("sqlite://")
    from growthstack.db import Base
    from growthstack.models import DomainCheck

    Base.metadata.create_all(db.get_engine())
    health = DomainHealth("acme.example", True, "v=spf1 -all", True, "v=DMARC1; p=none", "none", [WARN_P_NONE])
    with db.session_scope() as session:
        row = save_domain_check(session, health)
        assert row.id is not None
    with db.session_scope() as session:
        stored = session.query(DomainCheck).one()
        assert stored.domain == "acme.example"
        assert stored.dmarc_policy == "none"
        assert stored.warnings == [WARN_P_NONE]
        assert stored.checked_at is not None
