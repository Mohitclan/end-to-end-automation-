"""Per-mailbox sending limits: a rolling 24-hour cap, a minimum gap between sends,
and a sending window (UTC hours, optionally weekdays only)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from growthstack.models import Mailbox, MessageStatus, OutboundMessage


def _next_window_start(mailbox: Mailbox, t: datetime) -> datetime:
    """Earliest time >= t that falls inside the mailbox's sending window."""
    for _ in range(8):  # at most a week ahead
        start = t.replace(hour=mailbox.window_start_hour, minute=0, second=0, microsecond=0)
        end = t.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=mailbox.window_end_hour)
        weekend_ok = mailbox.send_weekends or t.weekday() < 5
        if weekend_ok and t < end:
            return max(t, start)
        t = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("mailbox sending window never opens; check window_start_hour/window_end_hour")


def next_allowed_send_time(db: Session, mailbox: Mailbox, now: datetime) -> datetime:
    """Return `now` if the mailbox may send now, otherwise the earliest time it may."""
    window_from = now - timedelta(hours=24)
    sent_q = select(OutboundMessage.sent_at).where(
        OutboundMessage.mailbox_id == mailbox.id,
        OutboundMessage.status == MessageStatus.SENT,
        OutboundMessage.sent_at > window_from,
    )
    sent_count = db.scalar(select(func.count()).select_from(sent_q.subquery())) or 0
    candidate = now

    if sent_count >= mailbox.daily_limit:
        # The slot frees up 24h after the oldest send that still counts against the cap.
        oldest_counting = db.scalars(
            sent_q.order_by(OutboundMessage.sent_at.desc()).offset(mailbox.daily_limit - 1).limit(1)
        ).first()
        if oldest_counting:
            candidate = max(candidate, oldest_counting + timedelta(hours=24, seconds=1))

    last_sent = db.scalar(
        select(func.max(OutboundMessage.sent_at)).where(
            OutboundMessage.mailbox_id == mailbox.id, OutboundMessage.status == MessageStatus.SENT
        )
    )
    if last_sent:
        candidate = max(candidate, last_sent + timedelta(seconds=mailbox.min_interval_seconds))

    return _next_window_start(mailbox, candidate)
