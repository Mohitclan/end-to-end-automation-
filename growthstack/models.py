"""Database models. All timestamps are naive UTC (see utcnow) so behaviour is the
same on SQLite (dev/tests) and Postgres (production)."""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from growthstack.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class LeadSource(enum.StrEnum):
    CSV = "csv"
    COMPANIES_HOUSE = "companies_house"
    HUNTER = "hunter"
    POSTHOG_SIGNUP = "posthog_signup"
    MANUAL = "manual"


# Human readable source labels used in the GDPR Art. 14 disclosure of the first email.
SOURCE_LABELS = {
    LeadSource.CSV: "a public business listing",
    LeadSource.COMPANIES_HOUSE: "the UK Companies House public register",
    LeadSource.HUNTER: "public sources on the web (via Hunter.io)",
    LeadSource.POSTHOG_SIGNUP: "your signup on our website",
    LeadSource.MANUAL: "a public business listing",
}


class LeadStage(enum.StrEnum):
    NEW = "new"
    RESEARCHING = "researching"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"  # enrolled in a sequence
    CONTACTED = "contacted"
    REPLIED = "replied"
    CONVERTED = "converted"
    REJECTED = "rejected"  # a person decided not to contact
    DISQUALIFIED = "disqualified"  # undeliverable / failed checks
    DO_NOT_CONTACT = "do_not_contact"


class ResearchStatus(enum.StrEnum):
    NONE = "none"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class EnrollmentStatus(enum.StrEnum):
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    COMPLETED = "completed"
    STOPPED_REPLY = "stopped_reply"
    STOPPED_UNSUBSCRIBED = "stopped_unsubscribed"
    STOPPED_BOUNCE = "stopped_bounce"
    STOPPED_COMPLAINT = "stopped_complaint"
    STOPPED_MANUAL = "stopped_manual"
    STOPPED_INVALID_EMAIL = "stopped_invalid_email"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self not in (EnrollmentStatus.PENDING_APPROVAL, EnrollmentStatus.ACTIVE)


class MessageStatus(enum.StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    SENT = "sent"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DncReason(enum.StrEnum):
    UNSUBSCRIBE = "unsubscribe"
    COMPLAINT = "complaint"
    HARD_BOUNCE = "hard_bounce"
    REPLY_OPT_OUT = "reply_opt_out"
    # Opt-out phrase in a reply we could only match by its From address, which can be
    # forged. Still honoured, but the team may remove it after checking.
    REPLY_OPT_OUT_UNVERIFIED = "reply_opt_out_unverified"
    MANUAL = "manual"
    LISTMONK = "listmonk"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    country: Mapped[str | None] = mapped_column(String(100))
    sic_codes: Mapped[list | None] = mapped_column(JSON)
    companies_house_number: Mapped[str | None] = mapped_column(String(20), unique=True)
    twenty_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    leads: Mapped[list[Lead]] = relationship(back_populates="company")


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable: Companies House gives us named directors without email addresses.
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    first_name: Mapped[str | None] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(500))
    country: Mapped[str | None] = mapped_column(String(100))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))

    # Provenance - required by GDPR Art. 14 (tell people where we got their data).
    source: Mapped[str] = mapped_column(String(40))
    source_detail: Mapped[str] = mapped_column(String(500))
    lawful_basis: Mapped[str] = mapped_column(String(40), default="legitimate_interest")
    imported_by: Mapped[str | None] = mapped_column(String(320))

    stage: Mapped[str] = mapped_column(String(30), default=LeadStage.NEW, index=True)
    score: Mapped[int | None] = mapped_column(Integer)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON)
    research_status: Mapped[str] = mapped_column(String(20), default=ResearchStatus.NONE)
    research_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime)
    research_error: Mapped[str | None] = mapped_column(Text)
    research_text: Mapped[str | None] = mapped_column(Text)
    research_summary: Mapped[str | None] = mapped_column(Text)
    research_fit: Mapped[int | None] = mapped_column(Integer)  # LLM fit 0-100
    graph_thread_id: Mapped[str | None] = mapped_column(String(100))

    verification: Mapped[dict | None] = mapped_column(JSON)
    verification_status: Mapped[str | None] = mapped_column(String(20))  # valid|risky|invalid

    twenty_person_id: Mapped[str | None] = mapped_column(String(64))
    twenty_opportunity_id: Mapped[str | None] = mapped_column(String(64))
    posthog_events_30d: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    company: Mapped[Company | None] = relationship(back_populates="leads")
    enrollments: Mapped[list[Enrollment]] = relationship(back_populates="lead")

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p) or (self.email or "")


class DoNotContact(Base):
    """The single do-not-contact list shared by every channel."""

    __tablename__ = "do_not_contact"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Exactly one of email / domain is set. A domain entry blocks every address at it.
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True)
    reason: Mapped[str] = mapped_column(String(30))
    channel: Mapped[str] = mapped_column(String(30), default="email")
    note: Mapped[str | None] = mapped_column(Text)
    synced_to_listmonk: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Mailbox(Base):
    __tablename__ = "mailboxes"
    id: Mapped[int] = mapped_column(primary_key=True)
    from_email: Mapped[str] = mapped_column(String(320), unique=True)
    from_name: Mapped[str] = mapped_column(String(200))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    daily_limit: Mapped[int] = mapped_column(Integer, default=30)
    min_interval_seconds: Mapped[int] = mapped_column(Integer, default=120)
    window_start_hour: Mapped[int] = mapped_column(Integer, default=8)  # UTC
    window_end_hour: Mapped[int] = mapped_column(Integer, default=17)  # UTC, exclusive
    send_weekends: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # IMAP reply polling. The password is read from the env var named here, never stored.
    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_user: Mapped[str | None] = mapped_column(String(320))
    imap_password_env: Mapped[str | None] = mapped_column(String(100))
    imap_last_uid: Mapped[int | None] = mapped_column(Integer)


class Sequence(Base):
    __tablename__ = "sequences"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    steps: Mapped[list[SequenceStep]] = relationship(
        back_populates="sequence", order_by="SequenceStep.position", cascade="all, delete-orphan"
    )


class SequenceStep(Base):
    """Step 1 is drafted by the AI and approved by a person. Later steps are
    follow-up templates rendered with the lead's fields (also shown at approval)."""

    __tablename__ = "sequence_steps"
    __table_args__ = (UniqueConstraint("sequence_id", "position"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sequence_id: Mapped[int] = mapped_column(ForeignKey("sequences.id"))
    position: Mapped[int] = mapped_column(Integer)  # 1-based
    delay_hours: Mapped[int] = mapped_column(Integer, default=0)  # after previous step was sent
    subject_template: Mapped[str] = mapped_column(String(300))  # "" on follow-ups = "Re: <step 1>"
    body_template: Mapped[str] = mapped_column(Text)
    sequence: Mapped[Sequence] = relationship(back_populates="steps")


class Enrollment(Base):
    __tablename__ = "enrollments"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    sequence_id: Mapped[int] = mapped_column(ForeignKey("sequences.id"))
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id"))
    status: Mapped[str] = mapped_column(String(30), default=EnrollmentStatus.PENDING_APPROVAL, index=True)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(320))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)

    lead: Mapped[Lead] = relationship(back_populates="enrollments")
    sequence: Mapped[Sequence] = relationship()
    mailbox: Mapped[Mailbox] = relationship()
    messages: Mapped[list[OutboundMessage]] = relationship(
        back_populates="enrollment", order_by="OutboundMessage.step_position"
    )


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        UniqueConstraint("enrollment_id", "step_position"),
        Index("ix_outbound_due", "status", "enqueued_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    step_position: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(300))
    body_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=MessageStatus.DRAFT)
    # When the sequence wants this step sent. Never moved by throttling.
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    # Set when a mailbox limit/window held the send back; informational only, the
    # limits are re-checked on every attempt so config changes apply immediately.
    deferred_until: Mapped[datetime | None] = mapped_column(DateTime)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    message_id_header: Mapped[str | None] = mapped_column(String(300), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(100), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    enrollment: Mapped[Enrollment] = relationship(back_populates="messages")


class InboundReply(Base):
    __tablename__ = "inbound_replies"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey("enrollments.id"))
    from_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(500))
    message_id: Mapped[str | None] = mapped_column(String(300), unique=True)
    is_auto_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    is_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    snippet: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(20), default="imap")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class EmailEvent(Base):
    """Delivery events reported by Resend's webhooks."""

    __tablename__ = "email_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    webhook_id: Mapped[str] = mapped_column(String(100), unique=True)  # svix-id, for idempotency
    event_type: Mapped[str] = mapped_column(String(50))
    provider_message_id: Mapped[str | None] = mapped_column(String(100), index=True)
    recipient: Mapped[str | None] = mapped_column(String(320))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SearchMetric(Base):
    """Search Console rows. kind='daily' rows have query=None; kind='query' rows are
    the top queries over the 28 days ending on `day`."""

    __tablename__ = "search_metrics"
    __table_args__ = (UniqueConstraint("kind", "day", "query"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    day: Mapped[date] = mapped_column(Date, index=True)
    query: Mapped[str | None] = mapped_column(String(500))
    clicks: Mapped[float] = mapped_column(Float, default=0)
    impressions: Mapped[float] = mapped_column(Float, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0)
    position: Mapped[float] = mapped_column(Float, default=0)


class PageScore(Base):
    __tablename__ = "page_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(500), index=True)
    strategy: Mapped[str] = mapped_column(String(10))
    performance: Mapped[float | None] = mapped_column(Float)
    seo: Mapped[float | None] = mapped_column(Float)
    accessibility: Mapped[float | None] = mapped_column(Float)
    best_practices: Mapped[float | None] = mapped_column(Float)
    top_opportunities: Mapped[list | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class DomainCheck(Base):
    __tablename__ = "domain_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    spf_valid: Mapped[bool] = mapped_column(Boolean)
    spf_record: Mapped[str | None] = mapped_column(Text)
    dmarc_valid: Mapped[bool] = mapped_column(Boolean)
    dmarc_record: Mapped[str | None] = mapped_column(Text)
    dmarc_policy: Mapped[str | None] = mapped_column(String(20))
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ProductMetric(Base):
    """Daily product numbers pulled from PostHog (signups, unique visitors)."""

    __tablename__ = "product_metrics"
    __table_args__ = (UniqueConstraint("day", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(50))
    value: Mapped[float] = mapped_column(Float)


class JobRun(Base):
    """Last run of each periodic job, shown on the dashboard so stale data is visible."""

    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_message: Mapped[str | None] = mapped_column(Text)
