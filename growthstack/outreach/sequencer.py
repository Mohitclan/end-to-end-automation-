"""Outreach sequencer state machine.

Lifecycle: an AI draft creates a PENDING_APPROVAL enrollment with one DRAFT message
per sequence step. A person approves it (step 1 becomes SCHEDULED). The worker sends
step N, then schedules step N+1 `delay_hours` after the actual send. A reply,
unsubscribe, bounce or complaint stops the enrollment and cancels every unsent
message; the worker re-checks all of this right before each send as well.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from growthstack import dnc
from growthstack.models import (
    Enrollment,
    EnrollmentStatus,
    Lead,
    LeadStage,
    Mailbox,
    MessageStatus,
    OutboundMessage,
    Sequence,
    utcnow,
)

log = logging.getLogger(__name__)

_template_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


class SequencerError(Exception):
    pass


def template_context(lead: Lead) -> dict:
    return {
        "first_name": lead.first_name or "there",
        "last_name": lead.last_name or "",
        "company": lead.company.name if lead.company else "your company",
        "title": lead.title or "",
    }


def render(template: str, lead: Lead) -> str:
    try:
        return _template_env.from_string(template).render(**template_context(lead)).strip()
    except TemplateError as exc:
        raise SequencerError(f"template error: {exc}") from exc


def pick_mailbox(db: Session) -> Mailbox:
    """The active mailbox with the fewest active enrollments (spreads load)."""
    load = (
        select(Enrollment.mailbox_id, func.count().label("n"))
        .where(Enrollment.status.in_([EnrollmentStatus.ACTIVE, EnrollmentStatus.PENDING_APPROVAL]))
        .group_by(Enrollment.mailbox_id)
        .subquery()
    )
    mailbox = db.scalars(
        select(Mailbox)
        .outerjoin(load, load.c.mailbox_id == Mailbox.id)
        .where(Mailbox.active.is_(True))
        .order_by(func.coalesce(load.c.n, 0), Mailbox.id)
    ).first()
    if not mailbox:
        raise SequencerError("no active mailbox configured")
    return mailbox


def default_sequence(db: Session) -> Sequence:
    seq = db.scalars(select(Sequence).where(Sequence.active.is_(True)).order_by(Sequence.id)).first()
    if not seq or not seq.steps:
        raise SequencerError("no active sequence with steps configured")
    return seq


def open_enrollment(db: Session, lead: Lead) -> Enrollment | None:
    return db.scalars(
        select(Enrollment).where(
            Enrollment.lead_id == lead.id,
            Enrollment.status.in_([EnrollmentStatus.PENDING_APPROVAL, EnrollmentStatus.ACTIVE]),
        )
    ).first()


def create_draft_enrollment(
    db: Session,
    lead: Lead,
    *,
    first_subject: str,
    first_body: str,
    sequence: Sequence | None = None,
    mailbox: Mailbox | None = None,
) -> Enrollment:
    if not lead.email:
        raise SequencerError("lead has no email address")
    if dnc.is_blocked(db, lead.email):
        raise SequencerError("lead is on the do-not-contact list")
    existing = open_enrollment(db, lead)
    if existing:
        raise SequencerError(f"lead already has an open enrollment (#{existing.id})")
    sequence = sequence or default_sequence(db)
    mailbox = mailbox or pick_mailbox(db)

    enrollment = Enrollment(lead=lead, sequence=sequence, mailbox=mailbox)
    db.add(enrollment)
    for step in sequence.steps:
        if step.position == 1:
            subject, body = first_subject.strip(), first_body.strip()
        else:
            subject = render(step.subject_template, lead) if step.subject_template else f"Re: {first_subject.strip()}"
            body = render(step.body_template, lead)
        db.add(
            OutboundMessage(
                enrollment=enrollment,
                step_position=step.position,
                subject=subject,
                body_text=body,
                status=MessageStatus.DRAFT,
                mailbox_id=mailbox.id,
            )
        )
    lead.stage = LeadStage.AWAITING_APPROVAL
    db.flush()
    return enrollment


def approve(
    db: Session,
    enrollment: Enrollment,
    *,
    approved_by: str,
    edits: dict[int, tuple[str, str]] | None = None,
    now: datetime | None = None,
) -> Enrollment:
    """A person approved the drafts. `edits` maps step position -> (subject, body)."""
    now = now or utcnow()
    if enrollment.status != EnrollmentStatus.PENDING_APPROVAL:
        raise SequencerError(f"enrollment is {enrollment.status}, not pending approval")
    lead = enrollment.lead
    if dnc.is_blocked(db, lead.email):
        stop_enrollment(db, enrollment, EnrollmentStatus.STOPPED_UNSUBSCRIBED, "on do-not-contact list")
        raise SequencerError("lead is on the do-not-contact list")
    if lead.verification_status == "invalid":
        stop_enrollment(db, enrollment, EnrollmentStatus.STOPPED_INVALID_EMAIL, "email failed verification")
        raise SequencerError("lead's email failed verification")

    for msg in enrollment.messages:
        if edits and msg.step_position in edits:
            subject, body = edits[msg.step_position]
            if not subject.strip() or not body.strip():
                raise SequencerError(f"step {msg.step_position}: subject and body are required")
            msg.subject, msg.body_text = subject.strip(), body.strip()
    first = _message_at(enrollment, 1)
    first.status = MessageStatus.SCHEDULED
    first.scheduled_for = now
    enrollment.status = EnrollmentStatus.ACTIVE
    enrollment.approved_by = approved_by
    enrollment.approved_at = now
    lead.stage = LeadStage.APPROVED
    db.flush()
    return enrollment


def reject(db: Session, enrollment: Enrollment, *, rejected_by: str, reason: str = "") -> None:
    if enrollment.status != EnrollmentStatus.PENDING_APPROVAL:
        raise SequencerError(f"enrollment is {enrollment.status}, not pending approval")
    stop_enrollment(db, enrollment, EnrollmentStatus.REJECTED, f"rejected by {rejected_by}: {reason}".strip())
    enrollment.lead.stage = LeadStage.REJECTED


def stop_enrollment(db: Session, enrollment: Enrollment, status: EnrollmentStatus, note: str) -> list[int]:
    """Stop an enrollment and cancel its unsent messages. Returns cancelled message ids."""
    cancelled = []
    if EnrollmentStatus(enrollment.status).is_terminal:
        return cancelled
    enrollment.status = status
    enrollment.stop_reason = note
    enrollment.stopped_at = utcnow()
    for msg in enrollment.messages:
        if msg.status in (MessageStatus.DRAFT, MessageStatus.SCHEDULED):
            msg.status = MessageStatus.CANCELLED
            cancelled.append(msg.id)
    log.info("enrollment stopped", extra={"enrollment_id": enrollment.id, "status": str(status)})
    return cancelled


def stop_for_lead(db: Session, lead: Lead, *, reason: EnrollmentStatus, note: str) -> list[int]:
    cancelled: list[int] = []
    for enrollment in lead.enrollments:
        cancelled += stop_enrollment(db, enrollment, reason, note)
    return cancelled


def on_message_sent(db: Session, msg: OutboundMessage, *, sent_at: datetime, provider_id: str) -> None:
    msg.status = MessageStatus.SENT
    msg.sent_at = sent_at
    msg.provider_message_id = provider_id
    msg.error = None
    enrollment = msg.enrollment
    if enrollment.lead.stage in (LeadStage.APPROVED, LeadStage.AWAITING_APPROVAL):
        enrollment.lead.stage = LeadStage.CONTACTED

    nxt = next((m for m in enrollment.messages if m.step_position == msg.step_position + 1), None)
    if nxt is None:
        enrollment.status = EnrollmentStatus.COMPLETED
        return
    step = next(s for s in enrollment.sequence.steps if s.position == nxt.step_position)
    nxt.status = MessageStatus.SCHEDULED
    nxt.scheduled_for = sent_at + timedelta(hours=step.delay_hours)


def _message_at(enrollment: Enrollment, position: int) -> OutboundMessage:
    for msg in enrollment.messages:
        if msg.step_position == position:
            return msg
    raise SequencerError(f"enrollment has no step {position}")
