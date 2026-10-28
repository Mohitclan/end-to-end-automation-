from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from growthstack import dnc
from growthstack.db import SessionLocal
from growthstack.models import (
    Company,
    DoNotContact,
    EmailEvent,
    EnrollmentStatus,
    Lead,
    LeadStage,
    MessageStatus,
    User,
)
from growthstack.outreach import sequencer
from growthstack.security import hash_password, make_unsubscribe_token, read_unsubscribe_token, verify_password

WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from growthstack.config import get_settings

    get_settings.cache_clear()
    from growthstack.web import auth
    from growthstack.web.app import create_app

    monkeypatch.setattr(auth, "throttle", auth.LoginThrottle())  # isolate per test
    with SessionLocal() as s:
        s.add(User(email="me@us.com", password_hash=hash_password("correct horse battery")))
        s.commit()
    return TestClient(create_app())


def _csrf(client, path="/login"):
    html = client.get(path).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _login(client):
    token = _csrf(client)
    r = client.post("/login", data={"email": "me@us.com", "password": "correct horse battery", "csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return _csrf(client, "/")


def _sign(body: bytes, msg_id="msg_1", ts=None):
    ts = str(ts or int(time.time()))
    key = base64.b64decode(WEBHOOK_SECRET.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}", "content-type": "application/json"}


# --- security primitives -------------------------------------------------------------
def test_password_hashing():
    h = hash_password("a long password!")
    assert verify_password("a long password!", h) and not verify_password("wrong", h)
    with pytest.raises(ValueError):
        hash_password("short")


def test_unsubscribe_token_is_signed():
    t = make_unsubscribe_token("Jane@Acme.io")
    assert read_unsubscribe_token(t) == "jane@acme.io"
    assert read_unsubscribe_token(t[:-2] + "xx") is None


# --- auth --------------------------------------------------------------------------
def test_pages_require_login(client):
    for path in ("/", "/leads", "/approvals", "/import", "/dnc", "/setup"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_login_rejects_bad_password_and_throttles(client):
    token = _csrf(client)
    for _ in range(5):
        client.post("/login", data={"email": "me@us.com", "password": "nope", "csrf_token": token})
    r = client.post("/login", data={"email": "me@us.com", "password": "correct horse battery", "csrf_token": token},
                    follow_redirects=False)
    assert r.headers["location"] == "/login"  # still throttled even with the right password


def test_post_without_csrf_is_rejected(client):
    _login(client)
    r = client.post("/dnc", data={"value": "x@y.com"})
    assert r.status_code == 403


def test_all_pages_render_after_login(client, seeded):
    _login(client)
    for path in ("/", "/leads", f"/leads/{seeded['lead'].id}", "/approvals", "/import", "/dnc", "/setup", "/leads?q=jane&stage=new"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Content-Security-Policy" in client.get("/").headers


# --- do-not-contact UI -------------------------------------------------------------
def test_dnc_add_and_protect_opt_outs(client, seeded):
    token = _login(client)
    r = client.post("/dnc", data={"value": "acme.io", "csrf_token": token})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert dnc.is_blocked(s, "jane@acme.io")
        assert s.get(Lead, seeded["lead"].id).stage == LeadStage.DO_NOT_CONTACT
        entry = DoNotContact(email="opted@out.com", reason="unsubscribe")
        s.add(entry)
        s.commit()
        eid = entry.id
    client.post(f"/dnc/{eid}/delete", data={"csrf_token": token})
    with SessionLocal() as s:
        assert s.get(DoNotContact, eid) is not None  # people who opted out can't be removed


# --- unsubscribe -------------------------------------------------------------------
def test_unsubscribe_get_confirms_post_unsubscribes(client, seeded):
    token = make_unsubscribe_token("jane@acme.io")
    page = client.get(f"/u/{token}")
    assert page.status_code == 200 and "Stop all emails" in page.text
    with SessionLocal() as s:
        assert not dnc.is_blocked(s, "jane@acme.io")  # GET must not unsubscribe (link scanners)
    # RFC 8058 one-click POST, as Gmail sends it
    r = client.post(f"/u/{token}", content="List-Unsubscribe=One-Click",
                    headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 200 and "unsubscribed" in r.text
    with SessionLocal() as s:
        assert dnc.is_blocked(s, "jane@acme.io")
    assert client.post(f"/u/{token}").status_code == 200  # idempotent


def test_unsubscribe_bad_token(client):
    assert client.get("/u/not-a-token").status_code == 404
    assert client.post("/u/not-a-token").status_code == 404


# --- Resend webhook ----------------------------------------------------------------
def test_webhook_rejects_bad_or_old_signature(client):
    body = json.dumps({"type": "email.delivered", "data": {}}).encode()
    headers = _sign(body)
    headers["svix-signature"] = "v1,AAAA"
    assert client.post("/webhooks/resend", content=body, headers=headers).status_code == 401
    assert client.post("/webhooks/resend", content=body, headers=_sign(body, ts=int(time.time()) - 3600)).status_code == 401


def test_webhook_hard_bounce_and_complaint_add_to_dnc_idempotently(client, seeded):
    bounce = json.dumps({"type": "email.bounced", "data": {"email_id": "re_1", "to": ["jane@acme.io"],
                         "bounce": {"type": "Permanent", "message": "mailbox does not exist"}}}).encode()
    r = client.post("/webhooks/resend", content=bounce, headers=_sign(bounce, "msg_b"))
    assert r.status_code == 200 and "hard bounce" in r.json()["result"]
    assert client.post("/webhooks/resend", content=bounce, headers=_sign(bounce, "msg_b")).json()["result"] == "duplicate"
    soft = json.dumps({"type": "email.bounced", "data": {"to": ["x@y.com"], "bounce": {"type": "Transient"}}}).encode()
    assert client.post("/webhooks/resend", content=soft, headers=_sign(soft, "msg_s")).json()["result"] == "recorded"
    complaint = json.dumps({"type": "email.complained", "data": {"to": ["bob@z.com"]}}).encode()
    client.post("/webhooks/resend", content=complaint, headers=_sign(complaint, "msg_c"))
    with SessionLocal() as s:
        assert dnc.is_blocked(s, "jane@acme.io") and dnc.is_blocked(s, "bob@z.com") and not dnc.is_blocked(s, "x@y.com")
        assert s.query(EmailEvent).count() == 3


def test_webhook_inbound_reply_stops_sequence(client, seeded):
    with SessionLocal() as s:
        lead = s.get(Lead, seeded["lead"].id)
        e = sequencer.create_draft_enrollment(s, lead, first_subject="Hi", first_body="Hello")
        sequencer.approve(s, e, approved_by="me@us.com")
        s.commit()
        eid = e.id
    body = json.dumps({"type": "email.received", "data": {"from": "Jane Doe <jane@acme.io>", "subject": "Re: Hi",
                       "message_id": "<in1@acme.io>"}}).encode()
    assert client.post("/webhooks/resend", content=body, headers=_sign(body, "msg_r")).json()["result"] == "reply recorded"
    with SessionLocal() as s:
        from growthstack.models import Enrollment

        assert s.get(Enrollment, eid).status == EnrollmentStatus.STOPPED_REPLY


# --- approvals through the UI ---------------------------------------------------------
def test_approve_draft_from_ui(client, seeded):
    token = _login(client)
    with SessionLocal() as s:
        lead = s.get(Lead, seeded["lead"].id)
        e = sequencer.create_draft_enrollment(s, lead, first_subject="Draft subject", first_body="Draft body")
        s.commit()
        eid = e.id
    page = client.get("/approvals")
    assert "Draft subject" in page.text and "SaaS Expo 2026 exhibitor list" in page.text
    r = client.post(f"/approvals/{eid}", data={
        "csrf_token": token, "action": "approve",
        "subject_1": "Better subject", "body_1": "Better body",
        "subject_2": "Re: Draft subject", "body_2": "Hi Jane, following up.",
        "subject_3": "Last note", "body_3": "Bye from us to Acme Ltd.",
    })
    assert r.status_code == 200 and "Approved" in r.text
    with SessionLocal() as s:
        from growthstack.models import Enrollment

        e = s.get(Enrollment, eid)
        assert e.status == EnrollmentStatus.ACTIVE and e.approved_by == "me@us.com"
        assert (e.messages[0].subject, e.messages[0].body_text, e.messages[0].status) == ("Better subject", "Better body", MessageStatus.SCHEDULED)


def test_csv_upload_from_ui(client):
    token = _login(client)
    r = client.post("/import/csv", data={"csrf_token": token, "source_detail": "Public speaker list, DevConf 2026"},
                    files={"file": ("leads.csv", b"email,name\nann@co.io,Ann Lee\n", "text/csv")})
    assert r.status_code == 200 and "Imported 1 new" in r.text
    with SessionLocal() as s:
        assert s.query(Lead).filter_by(email="ann@co.io").one().imported_by == "me@us.com"


def test_company_domain_from_lead_page_enables_hunter(client, monkeypatch, seeded):
    monkeypatch.setenv("HUNTER_API_KEY", "test-hunter-key")
    from growthstack.config import get_settings

    get_settings.cache_clear()
    with SessionLocal() as s:  # a Companies House director: company with no domain, no email
        lead = Lead(first_name="Raj", last_name="Patel", title="Director", source="companies_house",
                    source_detail="Companies House register, company 01234567",
                    company=Company(name="Beta Software Ltd", companies_house_number="01234567"))
        s.add(lead)
        s.commit()
        lead_id = lead.id
    token = _login(client)
    assert "Beta Software Ltd" not in client.get("/import").text

    r = client.post(f"/leads/{lead_id}/company-domain",
                    data={"csrf_token": token, "domain": "https://www.Beta-Software.co.uk/about"})
    assert r.status_code == 200 and "Website set to beta-software.co.uk" in r.text
    assert "Beta Software Ltd (beta-software.co.uk)" in client.get("/import").text

    r = client.post(f"/leads/{lead_id}/company-domain", data={"csrf_token": token, "domain": "not a domain"})
    assert "doesn&#39;t look like a website domain" in r.text
    r = client.post(f"/leads/{lead_id}/company-domain", data={"csrf_token": token, "domain": "acme.io"})
    assert "acme.io already belongs to Acme Ltd" in r.text
    with SessionLocal() as s:
        assert s.get(Lead, lead_id).company.domain == "beta-software.co.uk"


def test_setup_mailbox_validation(client):
    token = _login(client)
    r = client.post("/setup/mailboxes", data={"csrf_token": token, "from_email": "a@b.com", "from_name": "A",
                                              "window_start_hour": 18, "window_end_hour": 9})
    assert "Check the sending window" in r.text


def _mailbox_form(token, **overrides):
    form = {"csrf_token": token, "from_email": "alex@send.example.com", "from_name": "Alex", "reply_to": "",
            "daily_limit": 2, "min_interval_seconds": 60, "window_start_hour": 0, "window_end_hour": 24,
            "send_weekends": "1", "imap_host": "", "imap_user": "", "imap_password_env": ""}
    form.update(overrides)
    return form


def test_setup_edit_mailbox_including_imap(client, seeded):
    token = _login(client)
    mid = seeded["mailbox"].id
    assert "alex@send.example.com" in client.get(f"/setup/mailboxes/{mid}/edit").text
    r = client.post(f"/setup/mailboxes/{mid}/edit", data=_mailbox_form(
        token, daily_limit=25, imap_host="imap.gmail.com", imap_user="alex@example.com", imap_password_env="IMAP_PASSWORD_ALEX"))
    assert r.status_code == 200 and "saved" in r.text
    with SessionLocal() as s:
        from growthstack.models import Mailbox

        m = s.get(Mailbox, mid)
        assert (m.daily_limit, m.imap_host, m.imap_password_env) == (25, "imap.gmail.com", "IMAP_PASSWORD_ALEX")
    r = client.post(f"/setup/mailboxes/{mid}/edit", data=_mailbox_form(token, window_start_hour=18, window_end_hour=9))
    assert "Check the sending window" in r.text


def test_setup_edit_mailbox_requires_csrf(client, seeded):
    _login(client)
    r = client.post(f"/setup/mailboxes/{seeded['mailbox'].id}/edit", data={"from_email": "x@y.com", "from_name": "X"})
    assert r.status_code == 403
    client.cookies.clear()
    assert client.get(f"/setup/mailboxes/{seeded['mailbox'].id}/edit", follow_redirects=False).headers["location"] == "/login"


def test_setup_mailbox_in_use_cannot_be_deleted(client, seeded):
    token = _login(client)
    from growthstack.models import Mailbox

    with SessionLocal() as s:
        lead = s.get(Lead, seeded["lead"].id)
        sequencer.create_draft_enrollment(s, lead, first_subject="Hi", first_body="Hello")
        s.commit()
    r = client.post(f"/setup/mailboxes/{seeded['mailbox'].id}/delete", data={"csrf_token": token})
    assert "Pause it instead" in r.text
    with SessionLocal() as s:
        assert s.get(Mailbox, seeded["mailbox"].id) is not None


def test_setup_unused_mailbox_can_be_deleted(client, seeded):
    token = _login(client)
    from growthstack.models import Mailbox

    client.post("/setup/mailboxes", data=_mailbox_form(token, from_email="spare@send.example.com"))
    client.post("/setup/mailboxes", data=_mailbox_form(token, from_email="spare@send.example.com"))  # no duplicate
    with SessionLocal() as s:
        spare = s.query(Mailbox).filter_by(from_email="spare@send.example.com").one()
    r = client.post(f"/setup/mailboxes/{spare.id}/delete", data={"csrf_token": token})
    assert r.status_code == 200 and "deleted" in r.text
    with SessionLocal() as s:
        assert s.get(Mailbox, spare.id) is None


def _sequence_form(token, name="Default", **steps):
    form = {"csrf_token": token, "name": name}
    form.update(steps)
    return form


def test_setup_edit_sequence_leaves_drafted_messages_unchanged(client, seeded):
    token = _login(client)
    sid = seeded["sequence"].id
    with SessionLocal() as s:
        e = sequencer.create_draft_enrollment(s, s.get(Lead, seeded["lead"].id), first_subject="Hi", first_body="Hello")
        s.commit()
        eid = e.id
    assert "keep the text they have" in client.get(f"/setup/sequences/{sid}/edit").text
    r = client.post(f"/setup/sequences/{sid}/edit", data=_sequence_form(
        token, delay_2=4, subject_2="", body_2="New follow-up for {{ first_name }}",
        delay_3=6, subject_3="Last note", body_3="New last note."))
    assert r.status_code == 200 and "saved" in r.text
    with SessionLocal() as s:
        from growthstack.models import Enrollment, Sequence

        msgs = {m.step_position: m.body_text for m in s.get(Enrollment, eid).messages}
        assert msgs[2] == "Hi Jane, following up." and msgs[3] == "Bye from us to Acme Ltd."
        steps = {st.position: st for st in s.get(Sequence, sid).steps}
        assert steps[2].body_template == "New follow-up for {{ first_name }}" and steps[2].delay_hours == 96


def test_setup_sequence_in_use_keeps_its_steps_and_cannot_be_deleted(client, seeded):
    token = _login(client)
    sid = seeded["sequence"].id
    with SessionLocal() as s:
        sequencer.create_draft_enrollment(s, s.get(Lead, seeded["lead"].id), first_subject="Hi", first_body="Hello")
        s.commit()
    r = client.post(f"/setup/sequences/{sid}/edit", data=_sequence_form(token, delay_2=3, body_2="Only one follow-up"))
    assert "can&#39;t be removed" in r.text
    r = client.post(f"/setup/sequences/{sid}/delete", data={"csrf_token": token})
    assert "Turn it off instead" in r.text
    with SessionLocal() as s:
        from growthstack.models import Sequence

        assert len(s.get(Sequence, sid).steps) == 3


def test_setup_unused_sequence_can_be_edited_and_deleted(client, seeded):
    token = _login(client)
    from growthstack.models import Sequence

    client.post("/setup/sequences", data=_sequence_form(token, name="Spare", delay_2=3, body_2="One", delay_3=5, body_3="Two"))
    with SessionLocal() as s:
        sid = s.query(Sequence).filter_by(name="Spare").one().id
    r = client.post(f"/setup/sequences/{sid}/edit", data=_sequence_form(token, name="Spare v2", delay_2=3, body_2="Only one"))
    assert "saved" in r.text
    with SessionLocal() as s:
        seq = s.get(Sequence, sid)
        assert seq.name == "Spare v2" and [st.position for st in seq.steps] == [1, 2]
    r = client.post(f"/setup/sequences/{sid}/delete", data={"csrf_token": token})
    assert "deleted" in r.text
    with SessionLocal() as s:
        assert s.get(Sequence, sid) is None


def test_production_refuses_unsafe_config(monkeypatch, database):
    from growthstack.config import get_settings
    from growthstack.web.app import create_app

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "dev-insecure-change-me")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()
