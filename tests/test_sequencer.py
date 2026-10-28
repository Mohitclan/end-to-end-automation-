from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from growthstack import dnc
from growthstack.models import (
    DncReason,
    DoNotContact,
    EnrollmentStatus,
    LeadStage,
    Mailbox,
    MessageStatus,
    OutboundMessage,
)
from growthstack.outreach import delivery, policy, replies, sender, sequencer

NOW = datetime(2026, 10, 28, 10, 0, 0)  # a Wednesday


def _valid(_email):
    return {"status": "valid", "reasons": []}


def _approved(db, seeded, now=NOW):
    e = sequencer.create_draft_enrollment(db, seeded["lead"], first_subject="Hello Acme", first_body="Hi Jane, ...")
    sequencer.approve(db, e, approved_by="me@us.com", now=now)
    db.commit()
    return e


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, built, *, idempotency_key):
        self.sent.append((built, idempotency_key))
        return f"re_{len(self.sent)}"


# --- sending policy --------------------------------------------------------------
def test_window_and_weekends():
    mb = Mailbox(window_start_hour=8, window_end_hour=17, send_weekends=False)
    assert policy._next_window_start(mb, datetime(2026, 10, 28, 7, 30)) == datetime(2026, 10, 28, 8, 0)
    assert policy._next_window_start(mb, datetime(2026, 10, 28, 12, 0)) == datetime(2026, 10, 28, 12, 0)
    # Friday evening -> Monday 08:00
    assert policy._next_window_start(mb, datetime(2026, 10, 30, 18, 0)) == datetime(2026, 11, 2, 8, 0)


def test_daily_limit_and_min_interval(db, seeded):
    mb = seeded["mailbox"]  # limit 2/day, 60s apart
    assert policy.next_allowed_send_time(db, mb, NOW) == NOW
    e = _approved(db, seeded)
    msgs = e.messages
    msgs[0].status, msgs[0].sent_at, msgs[0].mailbox_id = MessageStatus.SENT, NOW - timedelta(seconds=10), mb.id
    db.flush()
    assert policy.next_allowed_send_time(db, mb, NOW) == NOW - timedelta(seconds=10) + timedelta(seconds=60)
    msgs[1].status, msgs[1].sent_at, msgs[1].mailbox_id = MessageStatus.SENT, NOW - timedelta(hours=5), mb.id
    db.flush()
    # cap reached: next slot is 24h after the older of the two sends that count
    assert policy.next_allowed_send_time(db, mb, NOW) == NOW - timedelta(hours=5) + timedelta(hours=24, seconds=1)


# --- drafting / approval ------------------------------------------------------------
def test_draft_renders_followups_and_needs_approval(db, seeded):
    e = sequencer.create_draft_enrollment(db, seeded["lead"], first_subject="Hello Acme", first_body="Hi Jane")
    assert e.status == EnrollmentStatus.PENDING_APPROVAL
    assert [m.status for m in e.messages] == [MessageStatus.DRAFT] * 3
    assert e.messages[1].subject == "Re: Hello Acme" and e.messages[1].body_text == "Hi Jane, following up."
    assert e.messages[2].subject == "Last note" and "Acme Ltd" in e.messages[2].body_text
    assert seeded["lead"].stage == LeadStage.AWAITING_APPROVAL
    with pytest.raises(sequencer.SequencerError):
        sequencer.create_draft_enrollment(db, seeded["lead"], first_subject="x", first_body="y")


def test_sandboxed_templates_reject_attribute_access(db, seeded):
    with pytest.raises(sequencer.SequencerError):
        sequencer.render("{{ first_name.__class__.__mro__ }}{{ missing_var }}", seeded["lead"])


def test_approve_with_edits_schedules_step_one_only(db, seeded):
    e = sequencer.create_draft_enrollment(db, seeded["lead"], first_subject="s", first_body="b")
    sequencer.approve(db, e, approved_by="me@us.com", edits={1: ("Edited", "New body")}, now=NOW)
    assert e.status == EnrollmentStatus.ACTIVE and e.approved_by == "me@us.com"
    assert (e.messages[0].subject, e.messages[0].status, e.messages[0].scheduled_for) == ("Edited", MessageStatus.SCHEDULED, NOW)
    assert e.messages[1].status == MessageStatus.DRAFT


def test_cannot_approve_blocked_lead(db, seeded):
    e = sequencer.create_draft_enrollment(db, seeded["lead"], first_subject="s", first_body="b")
    db.add(DoNotContact(email="jane@acme.io", reason="manual"))
    db.flush()
    with pytest.raises(sequencer.SequencerError):
        sequencer.approve(db, e, approved_by="me@us.com", now=NOW)
    assert e.status == EnrollmentStatus.STOPPED_UNSUBSCRIBED


# --- delivery ---------------------------------------------------------------------
def test_full_sequence_sends_steps_with_delays(db, seeded):
    e = _approved(db, seeded)
    rec = Recorder()
    out = delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid, send=rec)
    assert out.action == "sent"
    m1, m2, m3 = e.messages
    assert m1.status == MessageStatus.SENT and m1.message_id_header.startswith("<gs.")
    assert m2.status == MessageStatus.SCHEDULED and m2.scheduled_for == NOW + timedelta(hours=72)
    assert seeded["lead"].stage == LeadStage.CONTACTED

    # not due yet -> delayed until its time
    assert delivery.process_send(db, m2.id, now=NOW + timedelta(hours=1), verify=_valid, send=rec).action == "delayed"
    t2 = NOW + timedelta(hours=72)
    assert delivery.process_send(db, m2.id, now=t2, verify=_valid, send=rec).action == "sent"
    built2 = rec.sent[1][0]
    assert built2.params["headers"]["In-Reply-To"] == m1.message_id_header  # threads under step 1
    t3 = t2 + timedelta(hours=120)
    assert delivery.process_send(db, m3.id, now=t3, verify=_valid, send=rec).action == "sent"
    assert e.status == EnrollmentStatus.COMPLETED
    assert [k for _, k in rec.sent] == [f"outbound-{m.id}" for m in (m1, m2, m3)]


def test_compliance_headers_and_footer(db, seeded):
    e = _approved(db, seeded)
    rec = Recorder()
    delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid, send=rec)
    built = rec.sent[0][0]
    h = built.params["headers"]
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert h["List-Unsubscribe"].startswith("<https://growth.example.com/u/")
    text = built.params["text"]
    assert "SaaS Expo 2026 exhibitor list" in text  # GDPR Art. 14 source disclosure on first email
    assert "Unsubscribe in one click: https://growth.example.com/u/" in text


def test_followups_do_not_repeat_source_disclosure(db, seeded):
    e = _approved(db, seeded)
    built = sender.build_email(e.messages[1], seeded["lead"], seeded["mailbox"])
    assert "I found your business contact details" not in built.params["text"]
    assert "Unsubscribe" in built.params["text"]


def test_mailbox_limit_delays_send(db, seeded):
    e = _approved(db, seeded)
    mb = seeded["mailbox"]
    # fill today's quota with two other sent messages
    for i in range(2):
        db.add(OutboundMessage(enrollment_id=e.id, step_position=10 + i, subject="x", body_text="x",
                               status=MessageStatus.SENT, sent_at=NOW - timedelta(hours=i + 1), mailbox_id=mb.id))
    db.flush()
    out = delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid, send=Recorder())
    assert out.action == "delayed" and out.retry_at > NOW
    m = e.messages[0]
    assert m.status == MessageStatus.SCHEDULED
    assert m.deferred_until == out.retry_at and m.scheduled_for == NOW  # due time is not moved
    # raising the limit takes effect on the very next attempt
    mb.daily_limit = 10
    assert delivery.process_send(db, m.id, now=NOW, verify=_valid, send=Recorder()).action == "sent"
    assert m.deferred_until is None


def test_invalid_email_stops_before_sending(db, seeded):
    seeded["lead"].verification_status = None
    e = _approved(db, seeded)
    rec = Recorder()
    out = delivery.process_send(db, e.messages[0].id, now=NOW, verify=lambda _: {"status": "invalid"}, send=rec)
    assert out.action == "skipped" and not rec.sent
    assert e.status == EnrollmentStatus.STOPPED_INVALID_EMAIL
    assert seeded["lead"].stage == LeadStage.DISQUALIFIED


def test_retryable_error_keeps_message_scheduled(db, seeded):
    e = _approved(db, seeded)

    def boom(built, *, idempotency_key):
        raise sender.SendError("timeout")

    with pytest.raises(sender.SendError):
        delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid, send=boom)
    assert e.messages[0].status == MessageStatus.SCHEDULED and e.messages[0].attempts == 1


def test_permanent_error_fails_message(db, seeded):
    e = _approved(db, seeded)

    def reject(built, *, idempotency_key):
        raise sender.PermanentSendError("422 invalid from")

    out = delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid, send=reject)
    assert out.action == "failed" and e.messages[0].status == MessageStatus.FAILED


def test_dry_run_send_never_calls_resend(db, seeded, monkeypatch):
    import resend

    monkeypatch.setattr(resend.Emails, "send", lambda *a, **k: pytest.fail("Resend called in dry run"))
    e = _approved(db, seeded)
    out = delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid)
    assert out.action == "sent" and out.provider_id.startswith("dryrun-")


# --- stopping -----------------------------------------------------------------------
def test_reply_stops_sequence_and_cancels_followups(db, seeded):
    e = _approved(db, seeded)
    delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid, send=Recorder())
    reply = replies.process_inbound(
        db, replies.Inbound(from_email="Jane@Acme.io", subject="Re: Hello Acme", in_reply_to=e.messages[0].message_id_header,
                            message_id="<r1@acme.io>", body_text="Sounds good, call Tuesday?")
    )
    assert reply and not reply.is_auto_reply and not reply.is_opt_out
    assert e.status == EnrollmentStatus.STOPPED_REPLY
    assert [m.status for m in e.messages[1:]] == [MessageStatus.CANCELLED] * 2
    assert seeded["lead"].stage == LeadStage.REPLIED
    # the worker would skip the cancelled job
    assert delivery.process_send(db, e.messages[1].id, now=NOW + timedelta(days=5), verify=_valid, send=Recorder()).action == "skipped"
    # duplicate delivery of the same reply is ignored
    assert replies.process_inbound(db, replies.Inbound(from_email="jane@acme.io", message_id="<r1@acme.io>")) is None


def test_out_of_office_does_not_stop(db, seeded):
    e = _approved(db, seeded)
    r = replies.process_inbound(db, replies.Inbound(from_email="jane@acme.io", subject="Out of Office: back Monday",
                                                    headers={"Auto-Submitted": "auto-replied"}))
    assert r.is_auto_reply and e.status == EnrollmentStatus.ACTIVE


def test_opt_out_reply_adds_to_dnc(db, seeded):
    e = _approved(db, seeded)
    replies.process_inbound(db, replies.Inbound(
        from_email="jane@acme.io", subject="Re: Hello",
        body_text="Please remove me from your list.\n\nOn Tue, Alex wrote:\n> Unsubscribe in one click"))
    assert dnc.is_blocked(db, "jane@acme.io")
    assert e.status == EnrollmentStatus.STOPPED_UNSUBSCRIBED
    assert seeded["lead"].stage == LeadStage.DO_NOT_CONTACT


def test_quoted_footer_is_not_an_opt_out(db, seeded):
    msg = replies.Inbound(from_email="x@y.com", body_text="Yes keen to chat!\n\n> Not interested? Unsubscribe in one click")
    assert not replies.is_opt_out(msg)


def test_dnc_domain_block_and_bulk_check(db, seeded):
    dnc.add(db, domain="acme.io", reason=DncReason.MANUAL)
    assert dnc.is_blocked(db, "anyone@ACME.io")
    assert dnc.blocked_subset(db, ["a@acme.io", "b@other.com"]) == {"a@acme.io"}
    with pytest.raises(ValueError):
        dnc.add(db, reason=DncReason.MANUAL)


def test_unsubscribe_mid_sequence_skips_scheduled_followup(db, seeded):
    e = _approved(db, seeded)
    delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid, send=Recorder())
    dnc.add(db, email="jane@acme.io", reason=DncReason.UNSUBSCRIBE)
    assert e.status == EnrollmentStatus.STOPPED_UNSUBSCRIBED
    assert e.messages[1].status == MessageStatus.CANCELLED


def test_parse_rfc822_reply():
    raw = (b"From: Jane <jane@acme.io>\r\nSubject: Re: Hello\r\nMessage-ID: <abc@acme.io>\r\n"
           b"In-Reply-To: <gs.1.ff@send.example.com>\r\nReferences: <gs.1.ff@send.example.com>\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\nYes please.\r\n")
    m = replies.parse_rfc822(raw)
    assert (m.from_email, m.in_reply_to, m.references, m.body_text.strip()) == (
        "jane@acme.io", "<gs.1.ff@send.example.com>", ["<gs.1.ff@send.example.com>"], "Yes please.")


def test_imap_poll_with_fake_server(db, seeded, monkeypatch):
    e = _approved(db, seeded)
    delivery.process_send(db, e.messages[0].id, now=NOW, verify=_valid, send=Recorder())
    mid = e.messages[0].message_id_header
    raw = (f"From: jane@acme.io\r\nSubject: Re: hi\r\nMessage-ID: <x1@acme.io>\r\nIn-Reply-To: {mid}\r\n\r\nInterested!").encode()

    class FakeIMAP:
        def __init__(self, host):
            self.host = host

        def login(self, user, pw):
            assert pw == "secret"

        def select(self, box, readonly):
            assert readonly

        def uid(self, cmd, *args):
            if cmd == "search":
                return "OK", [b"7"]
            return "OK", [(b"7 (BODY[] {10}", raw)]

        def logout(self):
            pass

    mb = seeded["mailbox"]
    mb.imap_host, mb.imap_user, mb.imap_password_env = "imap.test", "alex", "TEST_IMAP_PW"
    monkeypatch.setenv("TEST_IMAP_PW", "secret")
    assert replies.poll_mailbox(db, mb, imap_factory=FakeIMAP) == 1
    assert mb.imap_last_uid == 7 and e.status == EnrollmentStatus.STOPPED_REPLY
