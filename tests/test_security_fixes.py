"""Regression tests for the findings of the security review (28 Oct 2026)."""

from __future__ import annotations

import ipaddress

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from growthstack import dnc
from growthstack.config import Settings, get_settings
from growthstack.models import DncReason, Lead, LeadStage
from growthstack.outreach import replies, sender, sequencer
from growthstack.research import crawler


# 1 (HIGH): a known or empty SECRET_KEY must stop the app in every environment.
@pytest.mark.parametrize("key", ["", "dev-insecure-change-me", "short"])
def test_weak_secret_refused_even_in_development(key):
    problems = Settings(_env_file=None, environment="development", secret_key=key).check_production_safety()
    assert any("SECRET_KEY" in p for p in problems)


def test_dev_app_refuses_default_secret(monkeypatch, database):
    from growthstack.web.app import create_app

    monkeypatch.setenv("SECRET_KEY", "dev-insecure-change-me")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


async def test_worker_refuses_default_secret(monkeypatch, database):
    from growthstack.jobs.worker import run_worker

    monkeypatch.setenv("SECRET_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        await run_worker()


# 2: Crawl4AI's browser follows redirects itself; a private final URL is rejected.
@respx.mock
def test_crawl4ai_redirect_to_private_address_rejected(monkeypatch):
    monkeypatch.setattr(crawler.socket, "getaddrinfo", lambda host, *a, **k: [
        (2, 1, 6, "", ("169.254.169.254" if host == "169.254.169.254" else "93.184.216.34", 0))])
    respx.post("http://crawl4ai:11235/crawl").mock(return_value=httpx.Response(200, json={
        "success": True,
        "results": [{"url": "http://169.254.169.254/latest/meta-data/", "success": True, "status_code": 200,
                     "markdown": {"raw_markdown": "ami-id instance-id"}}],
    }))
    with pytest.raises(crawler.UnsafeURL):
        crawler.Crawl4AIClient("http://crawl4ai:11235").fetch("https://prospect.example/")


def test_nat64_embedded_private_address_blocked():
    assert not crawler._ip_is_public(ipaddress.ip_address("64:ff9b::a9fe:a9fe"))  # 169.254.169.254
    assert crawler._ip_is_public(ipaddress.ip_address("64:ff9b::5db8:d822"))  # 93.184.216.34


# 3: oversized bodies are refused before being read; no session -> 403 before parsing.
def test_body_size_limit_and_sessionless_post(database):
    from growthstack.web.app import create_app

    client = TestClient(create_app())
    big = b"x" * (1024 * 1024 + 10)
    assert client.post("/login", content=big, headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413
    assert client.post("/webhooks/resend", content=big).status_code == 413
    assert client.post("/import/csv", content=b"x" * (6 * 1024 * 1024 + 10)).status_code == 413
    assert client.post("/dnc", data={"value": "a@b.com"}).status_code == 403  # no session cookie at all


# 6: an opt-out that can't be tied to one of our emails is recorded as unverified.
def test_unthreaded_opt_out_is_marked_unverified(db, seeded):
    e = sequencer.create_draft_enrollment(db, seeded["lead"], first_subject="Hi", first_body="b")
    sequencer.approve(db, e, approved_by="me@us.com")
    replies.process_inbound(db, replies.Inbound(from_email="jane@acme.io", subject="unsubscribe"))
    entry = db.query(dnc.DoNotContact).filter_by(email="jane@acme.io").one()
    assert entry.reason == DncReason.REPLY_OPT_OUT_UNVERIFIED  # still honoured, but removable
    assert dnc.is_blocked(db, "jane@acme.io")


def test_threaded_opt_out_is_verified(db, seeded):
    e = sequencer.create_draft_enrollment(db, seeded["lead"], first_subject="Hi", first_body="b")
    sequencer.approve(db, e, approved_by="me@us.com")
    e.messages[0].message_id_header = "<gs.1.aa@send.example.com>"
    db.flush()
    replies.process_inbound(db, replies.Inbound(from_email="jane@acme.io", subject="Re: Hi", body_text="remove me",
                                                in_reply_to="<gs.1.aa@send.example.com>"))
    assert db.query(dnc.DoNotContact).filter_by(email="jane@acme.io").one().reason == DncReason.REPLY_OPT_OUT


# 7: a (forgeable) signup event never changes a lead that is being contacted.
def test_signup_event_does_not_touch_active_lead(database, monkeypatch):
    from growthstack.db import SessionLocal
    from growthstack.integrations.posthog_client import PostHogQueryClient
    from growthstack.jobs import tasks

    with SessionLocal() as s:
        s.add(Lead(email="busy@co.io", source="csv", source_detail="x", stage=LeadStage.CONTACTED))
        s.add(Lead(email="fresh@co.io", source="csv", source_detail="x", stage=LeadStage.NEW))
        s.commit()

    class Fake:
        def daily_event_counts(self, *a, **k): return {}
        def daily_unique_visitors(self, *a, **k): return {}
        def recent_signups(self, *a, **k): return [{"email": "busy@co.io"}, {"email": "fresh@co.io"}]
        def person_event_counts(self, *a, **k): return {}

    monkeypatch.setattr(PostHogQueryClient, "from_settings", classmethod(lambda cls, settings=None: Fake()))
    tasks.posthog_pull()
    with SessionLocal() as s:
        assert s.query(Lead).filter_by(email="busy@co.io").one().stage == LeadStage.CONTACTED
        assert s.query(Lead).filter_by(email="fresh@co.io").one().stage == LeadStage.CONVERTED


# Header injection: no CR/LF in subject or display name.
def test_subject_and_from_name_are_header_safe(db, seeded):
    e = sequencer.create_draft_enrollment(db, seeded["lead"], first_subject="Hi\r\nBcc: victim@x.com", first_body="b")
    seeded["mailbox"].from_name = 'Alex "Sales" <evil@x.com>\nBcc: y@z.com'
    built = sender.build_email(e.messages[0], seeded["lead"], seeded["mailbox"])
    assert "\n" not in built.params["subject"] and "\r" not in built.params["subject"]
    assert built.params["from"] == "Alex Sales evil@x.com Bcc: y@z.com <alex@send.example.com>"
