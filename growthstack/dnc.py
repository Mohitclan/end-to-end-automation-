"""The single do-not-contact list. Every channel (sequencer, listmonk sync, imports)
checks it through `is_blocked`, and every opt-out path writes through `add`."""

from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from growthstack.models import DncReason, DoNotContact, EnrollmentStatus, Lead, LeadStage

log = logging.getLogger(__name__)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def email_domain(email: str) -> str:
    return normalize_email(email).rsplit("@", 1)[-1]


def is_blocked(db: Session, email: str | None) -> bool:
    if not email:
        return False
    email = normalize_email(email)
    stmt = select(DoNotContact.id).where(
        or_(DoNotContact.email == email, DoNotContact.domain == email_domain(email))
    )
    return db.execute(stmt.limit(1)).first() is not None


def blocked_subset(db: Session, emails: list[str]) -> set[str]:
    """Return the members of `emails` that are blocked (one query per 500 addresses)."""
    normalized = {normalize_email(e) for e in emails if e}
    blocked: set[str] = set()
    items = sorted(normalized)
    for i in range(0, len(items), 500):
        chunk = items[i : i + 500]
        domains = {email_domain(e) for e in chunk}
        rows = db.execute(
            select(DoNotContact.email, DoNotContact.domain).where(
                or_(DoNotContact.email.in_(chunk), DoNotContact.domain.in_(domains))
            )
        ).all()
        blocked_emails = {r.email for r in rows if r.email}
        blocked_domains = {r.domain for r in rows if r.domain}
        blocked |= {e for e in chunk if e in blocked_emails or email_domain(e) in blocked_domains}
    return blocked


def add(
    db: Session,
    *,
    email: str | None = None,
    domain: str | None = None,
    reason: DncReason | str,
    channel: str = "email",
    note: str | None = None,
) -> DoNotContact:
    """Add an address (or a whole domain) and stop everything in flight for it.

    Idempotent: adding an existing entry returns it unchanged. Commits are left to
    the caller; the outreach stop happens in the same transaction.
    """
    if bool(email) == bool(domain):
        raise ValueError("pass exactly one of email or domain")
    if email:
        email = normalize_email(email)
        existing = db.scalar(select(DoNotContact).where(DoNotContact.email == email))
    else:
        domain = domain.strip().lower().lstrip("@")  # type: ignore[union-attr]
        existing = db.scalar(select(DoNotContact).where(DoNotContact.domain == domain))
    if existing:
        entry = existing
    else:
        entry = DoNotContact(email=email, domain=domain, reason=str(reason), channel=channel, note=note)
        try:
            with db.begin_nested():
                db.add(entry)
        except IntegrityError:  # concurrent insert of the same address
            entry = db.scalar(
                select(DoNotContact).where(
                    DoNotContact.email == email if email else DoNotContact.domain == domain
                )
            )
        log.info("do-not-contact added", extra={"reason": str(reason), "channel": channel})

    # Stop outreach for affected leads (import here to avoid an import cycle).
    from growthstack.outreach.sequencer import stop_for_lead

    if email:
        leads = db.scalars(select(Lead).where(Lead.email == email)).all()
    else:
        leads = db.scalars(select(Lead).where(Lead.email.like(f"%@{domain}"))).all()
    for lead in leads:
        stop_for_lead(db, lead, reason=_stop_reason_for(reason), note=f"do-not-contact: {reason}")
        lead.stage = LeadStage.DO_NOT_CONTACT
    return entry


def remove(db: Session, entry_id: int) -> None:
    entry = db.get(DoNotContact, entry_id)
    if entry:
        db.delete(entry)


def _stop_reason_for(reason: DncReason | str) -> EnrollmentStatus:
    if reason == DncReason.COMPLAINT:
        return EnrollmentStatus.STOPPED_COMPLAINT
    if reason == DncReason.HARD_BOUNCE:
        return EnrollmentStatus.STOPPED_BOUNCE
    return EnrollmentStatus.STOPPED_UNSUBSCRIBED
