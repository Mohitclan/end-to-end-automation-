"""End-to-end through real BullMQ + Redis: dispatcher -> queue -> worker -> send.
Needs Redis; set TEST_REDIS_URL (default redis://127.0.0.1:6390/0). Skipped if unreachable."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, timedelta

import pytest
import redis

from growthstack.db import SessionLocal
from growthstack.models import (
    Company,
    Enrollment,
    EnrollmentStatus,
    Lead,
    MessageStatus,
    OutboundMessage,
    utcnow,
)
from growthstack.outreach import replies, sequencer

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6390/0")


def _redis_up() -> bool:
    try:
        return redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
    except redis.RedisError:
        return False


pytestmark = [pytest.mark.redis, pytest.mark.skipif(not _redis_up(), reason=f"no Redis at {REDIS_URL}")]


@pytest.fixture
def queues(database, monkeypatch):
    prefix = f"gstest-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QUEUE_PREFIX", prefix)
    from growthstack.config import get_settings

    get_settings.cache_clear()
    yield prefix
    r = redis.Redis.from_url(REDIS_URL)
    for key in r.scan_iter(f"{prefix}:*"):
        r.delete(key)


async def _wait_for(predicate, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(predicate):
            return True
        await asyncio.sleep(0.2)
    return False


def _status(message_id):
    with SessionLocal() as s:
        return s.get(OutboundMessage, message_id).status


def _approve(lead_id) -> Enrollment:
    with SessionLocal() as s:
        lead = s.get(Lead, lead_id)
        e = sequencer.create_draft_enrollment(s, lead, first_subject="Hello", first_body="Hi")
        sequencer.approve(s, e, approved_by="me@us.com")
        s.commit()
        return e.id, [m.id for m in e.messages]


async def test_send_followup_and_cancel_through_bullmq(seeded, queues):
    import redis.asyncio as aioredis
    from bullmq import Queue, Worker

    from growthstack.jobs import worker as w

    opts = w.queue_opts()
    send_q, research_q = Queue(w.SEND, opts), Queue(w.RESEARCH, opts)
    rds = aioredis.from_url(REDIS_URL)
    procs = w.Processors(rds, send_q, research_q)
    send_worker = Worker(w.SEND, procs.send, {**opts, "concurrency": 2})
    try:
        eid, (m1, m2, m3) = _approve(seeded["lead"].id)

        # 1. dispatcher enqueues step 1; worker sends it (dry run)
        summary = await w.dispatch(send_q, research_q)
        assert summary.startswith("1 sends")
        assert await _wait_for(lambda: _status(m1) == MessageStatus.SENT), "step 1 was not sent"

        # 2. step 2 is scheduled 72h later -> a delayed BullMQ job with a deterministic id
        await w.dispatch(send_q, research_q)
        assert await send_q.getJobState(w.msg_job_id(m2)) == "delayed"
        assert _status(m2) == MessageStatus.SCHEDULED

        # 3. a reply cancels it in the DB; the dispatcher removes the job from Redis
        with SessionLocal() as s:
            replies.process_inbound(s, replies.Inbound(from_email="jane@acme.io", subject="Re: Hello", body_text="Yes!"))
            s.commit()
        assert _status(m2) == MessageStatus.CANCELLED and _status(m3) == MessageStatus.CANCELLED
        summary = await w.dispatch(send_q, research_q)
        assert "1 removed" in summary
        assert await send_q.getJobState(w.msg_job_id(m2)) == "unknown"
        with SessionLocal() as s:
            assert s.get(Enrollment, eid).status == EnrollmentStatus.STOPPED_REPLY
    finally:
        await send_worker.close()
        await send_q.close()
        await research_q.close()
        await rds.aclose()


async def test_mailbox_quota_moves_job_back_to_delayed(seeded, queues):
    import redis.asyncio as aioredis
    from bullmq import Queue, Worker

    from growthstack.jobs import worker as w

    # Fill the mailbox's 24h quota (daily_limit=2 in the fixture) with earlier sends.
    with SessionLocal() as s:
        other = Lead(email="old@else.io", source="csv", source_detail="x", company=Company(name="Else"))
        s.add(other)
        s.flush()
        e = sequencer.create_draft_enrollment(s, other, first_subject="a", first_body="b")
        for i, m in enumerate(e.messages[:2]):
            m.status, m.sent_at, m.mailbox_id = MessageStatus.SENT, utcnow() - timedelta(hours=i + 1), seeded["mailbox"].id
        s.commit()

    opts = w.queue_opts()
    send_q, research_q = Queue(w.SEND, opts), Queue(w.RESEARCH, opts)
    rds = aioredis.from_url(REDIS_URL)
    procs = w.Processors(rds, send_q, research_q)
    send_worker = Worker(w.SEND, procs.send, {**opts})
    try:
        _, (m1, *_rest) = _approve(seeded["lead"].id)
        await w.dispatch(send_q, research_q)

        async def delayed():
            return await send_q.getJobState(w.msg_job_id(m1)) == "delayed"

        for _ in range(75):
            if await delayed():
                break
            await asyncio.sleep(0.2)
        assert await delayed(), "job should have been moved back to delayed"
        with SessionLocal() as s:
            msg = s.get(OutboundMessage, m1)
            assert msg.status == MessageStatus.SCHEDULED and msg.deferred_until > utcnow() + timedelta(hours=20)
            expected_ms = int(msg.deferred_until.replace(tzinfo=UTC).timestamp() * 1000)
        # BullMQ keeps delayed jobs in a sorted set scored by (run-at ms << 12).
        score = await rds.zscore(f"{queues}:{w.SEND}:delayed", w.msg_job_id(m1))
        assert score is not None and abs((int(score) >> 12) - expected_ms) < 1000
    finally:
        await send_worker.close()
        await send_q.close()
        await research_q.close()
        await rds.aclose()


async def test_job_schedulers_install(queues, database):
    from bullmq import Queue

    from growthstack.jobs import tasks
    from growthstack.jobs import worker as w

    q = Queue(w.MAINTENANCE, w.queue_opts())
    try:
        await w.install_schedulers(q)
        await w.install_schedulers(q)  # idempotent
        schedulers = await q.getJobSchedulers()
        names = {s["key"] if isinstance(s, dict) and "key" in s else s.get("id") for s in schedulers}
        assert {"dispatch", *tasks.PERIODIC} <= names
    finally:
        await q.close()
