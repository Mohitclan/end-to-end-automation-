"""Numbers for the one-screen dashboard: leads, replies, signups, search
impressions and clicks, page scores, sending-domain health and job freshness."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from growthstack.models import (
    DomainCheck,
    InboundReply,
    JobRun,
    Lead,
    LeadStage,
    MessageStatus,
    OutboundMessage,
    PageScore,
    ProductMetric,
    SearchMetric,
    utcnow,
)

STAGE_ORDER = [
    LeadStage.NEW,
    LeadStage.RESEARCHING,
    LeadStage.AWAITING_APPROVAL,
    LeadStage.APPROVED,
    LeadStage.CONTACTED,
    LeadStage.REPLIED,
    LeadStage.CONVERTED,
]


def _series(rows: dict[date, float], start: date, end: date) -> list[float]:
    out, d = [], start
    while d <= end:
        out.append(float(rows.get(d, 0)))
        d += timedelta(days=1)
    return out


def build(db: Session) -> dict:
    now = utcnow()
    week_ago = now - timedelta(days=7)
    today = now.date()

    by_stage = dict(db.execute(select(Lead.stage, func.count()).group_by(Lead.stage)).all())
    total_leads = sum(by_stage.values())
    new_leads_7d = db.scalar(select(func.count()).select_from(Lead).where(Lead.created_at >= week_ago)) or 0
    sent_7d = db.scalar(
        select(func.count()).select_from(OutboundMessage).where(
            OutboundMessage.status == MessageStatus.SENT, OutboundMessage.sent_at >= week_ago
        )
    ) or 0
    replies_7d = db.scalar(
        select(func.count()).select_from(InboundReply).where(
            InboundReply.received_at >= week_ago, InboundReply.is_auto_reply.is_(False)
        )
    ) or 0
    sent_30d = db.scalar(
        select(func.count()).select_from(OutboundMessage).where(
            OutboundMessage.status == MessageStatus.SENT, OutboundMessage.sent_at >= now - timedelta(days=30)
        )
    ) or 0
    replied_leads_30d = db.scalar(
        select(func.count(func.distinct(InboundReply.lead_id))).where(
            InboundReply.received_at >= now - timedelta(days=30), InboundReply.is_auto_reply.is_(False)
        )
    ) or 0
    contacted_30d = db.scalar(
        select(func.count(func.distinct(OutboundMessage.enrollment_id))).where(
            OutboundMessage.status == MessageStatus.SENT, OutboundMessage.sent_at >= now - timedelta(days=30)
        )
    ) or 0
    awaiting = by_stage.get(LeadStage.AWAITING_APPROVAL, 0)

    # PostHog product metrics (last 30 days)
    start30 = today - timedelta(days=29)
    pm = db.execute(
        select(ProductMetric.day, ProductMetric.name, ProductMetric.value).where(ProductMetric.day >= start30)
    ).all()
    signups = {r.day: r.value for r in pm if r.name == "signups"}
    visitors = {r.day: r.value for r in pm if r.name == "visitors"}

    # Search Console (data ends ~3 days ago)
    gsc_rows = db.execute(
        select(SearchMetric.day, SearchMetric.clicks, SearchMetric.impressions, SearchMetric.position)
        .where(SearchMetric.kind == "daily")
        .order_by(SearchMetric.day.desc())
        .limit(28)
    ).all()
    gsc_rows = list(reversed(gsc_rows))
    last_query_day = db.scalar(select(func.max(SearchMetric.day)).where(SearchMetric.kind == "query"))
    top_queries = (
        db.scalars(
            select(SearchMetric)
            .where(SearchMetric.kind == "query", SearchMetric.day == last_query_day)
            .order_by(SearchMetric.clicks.desc(), SearchMetric.impressions.desc())
            .limit(8)
        ).all()
        if last_query_day
        else []
    )

    # Latest page score per (url, strategy)
    latest = (
        select(PageScore.url, PageScore.strategy, func.max(PageScore.id).label("id"))
        .group_by(PageScore.url, PageScore.strategy)
        .subquery()
    )
    page_scores = db.scalars(
        select(PageScore).join(latest, PageScore.id == latest.c.id).order_by(PageScore.url, PageScore.strategy)
    ).all()

    domain = db.scalars(select(DomainCheck).order_by(DomainCheck.id.desc()).limit(1)).first()
    jobs = db.scalars(select(JobRun).order_by(JobRun.name)).all()
    top_leads = db.scalars(
        select(Lead)
        .where(Lead.score.is_not(None), Lead.stage.in_([LeadStage.NEW, LeadStage.AWAITING_APPROVAL]))
        .order_by(Lead.score.desc())
        .limit(6)
    ).all()

    return {
        "kpis": {
            "total_leads": total_leads,
            "new_leads_7d": new_leads_7d,
            "awaiting": awaiting,
            "sent_7d": sent_7d,
            "replies_7d": replies_7d,
            "sent_30d": sent_30d,
            "reply_rate_30d": (replied_leads_30d / contacted_30d * 100) if contacted_30d else None,
            "signups_30d": sum(signups.values()) if signups else None,
            "visitors_30d": sum(visitors.values()) if visitors else None,
            "clicks_28d": sum(r.clicks for r in gsc_rows) if gsc_rows else None,
            "impressions_28d": sum(r.impressions for r in gsc_rows) if gsc_rows else None,
            "avg_position": (sum(r.position for r in gsc_rows) / len(gsc_rows)) if gsc_rows else None,
        },
        "pipeline": [(str(s).replace("_", " ").title(), by_stage.get(s, 0)) for s in STAGE_ORDER],
        "signup_series": _series(signups, start30, today) if signups else [],
        "visitor_series": _series(visitors, start30, today) if visitors else [],
        "click_series": [r.clicks for r in gsc_rows],
        "impression_series": [r.impressions for r in gsc_rows],
        "gsc_last_day": gsc_rows[-1].day if gsc_rows else None,
        "top_queries": top_queries,
        "page_scores": page_scores,
        "domain": domain,
        "jobs": jobs,
        "top_leads": top_leads,
    }
