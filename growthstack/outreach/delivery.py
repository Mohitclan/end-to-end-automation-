"""What the send worker does with one queued message. Kept free of BullMQ so it can
be unit-tested; growthstack.jobs.worker wraps it."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from growthstack import dnc
from growthstack.models import (
    Enrollment,
    EnrollmentStatus,
    LeadStage,
    MessageStatus,
    OutboundMessage,
    utcnow,
)
from growthstack.outreach import policy, sender, sequencer

log = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 5
VERIFICATION_MAX_AGE = timedelta(days=30)


@dataclass
class Outcome:
    action: str  # sent | skipped | delayed | failed
    detail: str = ""
    retry_at: datetime | None = None
    provider_id: str | None = None


def _default_verify(email: str) -> dict:
    from growthstack.verify.email_verifier import verify_email

    return verify_email(email).to_dict()


def ensure_verified(lead, verify: Callable[[str], dict], now: datetime) -> str:
    """Verify the lead's address if it was never checked or the check is stale."""
    checked_at = (lead.verification or {}).get("checked_at")
    stale = True
    if checked_at:
        try:
            stale = now - datetime.fromisoformat(checked_at) > VERIFICATION_MAX_AGE
        except ValueError:
            stale = True
    if lead.verification_status is None or stale:
        result = verify(lead.email)
        result["checked_at"] = now.isoformat()
        lead.verification = result
        lead.verification_status = result["status"]
    return lead.verification_status


def mailbox_id_for(db: Session, message_id: int) -> int | None:
    msg = db.get(OutboundMessage, message_id)
    return msg.mailbox_id if msg else None


def process_send(
    db: Session,
    message_id: int,
    *,
    now: datetime | None = None,
    verify: Callable[[str], dict] = _default_verify,
    send: Callable[..., str] = sender.send,
) -> Outcome:
    """Send one message if it is still allowed. Caller holds the per-mailbox lock and
    commits. Raises sender.SendError for retryable failures (after recording them)."""
    now = now or utcnow()
    msg = db.get(OutboundMessage, message_id)
    if msg is None:
        return Outcome("skipped", "message no longer exists")
    # SENDING is accepted: a crash after marking SENDING must be retryable. Resend's
    # idempotency key (per message) prevents a double send in that case.
    if msg.status not in (MessageStatus.SCHEDULED, MessageStatus.SENDING):
        return Outcome("skipped", f"message is {msg.status}")
    enrollment: Enrollment = msg.enrollment
    lead = enrollment.lead
    if enrollment.status != EnrollmentStatus.ACTIVE:
        msg.status = MessageStatus.CANCELLED
        return Outcome("skipped", f"enrollment is {enrollment.status}")
    if lead.stage in (LeadStage.REPLIED, LeadStage.CONVERTED, LeadStage.DO_NOT_CONTACT):
        sequencer.stop_enrollment(db, enrollment, EnrollmentStatus.STOPPED_MANUAL, f"lead is {lead.stage}")
        return Outcome("skipped", f"lead is {lead.stage}")
    if dnc.is_blocked(db, lead.email):
        sequencer.stop_enrollment(db, enrollment, EnrollmentStatus.STOPPED_UNSUBSCRIBED, "on do-not-contact list")
        lead.stage = LeadStage.DO_NOT_CONTACT
        return Outcome("skipped", "on do-not-contact list")
    if msg.scheduled_for and msg.scheduled_for > now + timedelta(seconds=5):
        return Outcome("delayed", "not due yet", retry_at=msg.scheduled_for)

    if ensure_verified(lead, verify, now) == "invalid":
        sequencer.stop_enrollment(db, enrollment, EnrollmentStatus.STOPPED_INVALID_EMAIL, "email failed verification")
        lead.stage = LeadStage.DISQUALIFIED
        return Outcome("skipped", "email failed verification")

    mailbox = enrollment.mailbox
    if not mailbox.active:
        retry = now + timedelta(hours=1)
        msg.deferred_until = retry
        return Outcome("delayed", "mailbox is paused", retry_at=retry)
    allowed_at = policy.next_allowed_send_time(db, mailbox, now)
    if allowed_at > now:
        msg.deferred_until = allowed_at
        return Outcome("delayed", "mailbox limit or sending window", retry_at=allowed_at)

    root = next((m for m in enrollment.messages if m.step_position == 1), None)
    built = sender.build_email(
        msg,
        lead,
        mailbox,
        thread_root_message_id=root.message_id_header if root and root is not msg else None,
    )
    msg.status = MessageStatus.SENDING
    msg.deferred_until = None
    msg.mailbox_id = mailbox.id
    msg.attempts += 1
    db.flush()
    try:
        provider_id = send(built, idempotency_key=f"outbound-{msg.id}")
    except sender.PermanentSendError as exc:
        msg.status = MessageStatus.FAILED
        msg.error = str(exc)
        return Outcome("failed", str(exc))
    except sender.SendError as exc:
        msg.error = str(exc)
        if msg.attempts >= MAX_SEND_ATTEMPTS:
            msg.status = MessageStatus.FAILED
            return Outcome("failed", f"gave up after {msg.attempts} attempts: {exc}")
        msg.status = MessageStatus.SCHEDULED
        raise
    msg.message_id_header = built.message_id_header
    sequencer.on_message_sent(db, msg, sent_at=now, provider_id=provider_id)
    return Outcome("sent", provider_id=provider_id)
