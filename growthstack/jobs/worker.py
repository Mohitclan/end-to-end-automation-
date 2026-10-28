"""The worker process (`growthstack worker`): BullMQ workers for three queues.

  send         one job per outbound email (job id "msg-<id>"), delayed until due
  research     one job per lead to run the AI pipeline
  maintenance  periodic jobs (job schedulers) incl. the dispatcher

The database is the source of truth. The dispatcher (every 15 s) moves due work
from the database into BullMQ, so the web app never talks to Redis and a lost
Redis queue is rebuilt from the database. Cancelling a message only needs a
database update: the send job re-checks everything before sending, and the
dispatcher also removes cancelled jobs from Redis.

BullMQ's Python worker has no per-group rate limiter, so per-mailbox limits are
enforced here: a Redis lock per mailbox serialises sends, delivery.process_send
checks the mailbox's quota/window, and a job that must wait is moved back to
delayed with job.moveToDelayed + DelayedError.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, timedelta

import redis.asyncio as aioredis
from bullmq import DelayedError, Queue, Worker
from sqlalchemy import select

from growthstack.config import get_settings
from growthstack.db import session_scope
from growthstack.jobs import tasks
from growthstack.models import Lead, MessageStatus, OutboundMessage, ResearchStatus, utcnow
from growthstack.outreach import delivery, sender

log = logging.getLogger(__name__)

SEND, RESEARCH, MAINTENANCE = "send", "research", "maintenance"
SEND_JOB_OPTS = {
    "attempts": delivery.MAX_SEND_ATTEMPTS,
    "backoff": {"type": "exponential", "delay": 60_000},
    "removeOnComplete": {"age": 7 * 24 * 3600},
    "removeOnFail": {"age": 30 * 24 * 3600},
}
RESEARCH_JOB_OPTS = {
    "attempts": 3,
    "backoff": {"type": "exponential", "delay": 120_000},
    "removeOnComplete": {"age": 7 * 24 * 3600},
    "removeOnFail": {"age": 30 * 24 * 3600},
}
STALE_AFTER = timedelta(minutes=30)


def queue_opts() -> dict:
    s = get_settings()
    return {"connection": s.redis_url, "prefix": s.queue_prefix}


def msg_job_id(message_id: int) -> str:
    return f"msg-{message_id}"


# --- dispatcher -------------------------------------------------------------------
def _collect_dispatch_work() -> dict:
    now = utcnow()
    with session_scope() as db:
        due = db.scalars(
            select(OutboundMessage).where(
                OutboundMessage.status == MessageStatus.SCHEDULED, OutboundMessage.enqueued_at.is_(None)
            )
        ).all()
        sends = [(m.id, m.scheduled_for or now) for m in due]
        for m in due:
            m.enqueued_at = now
        # Scheduled long ago but never sent: the job may have been lost (e.g. Redis reset).
        stale = db.scalars(
            select(OutboundMessage.id).where(
                OutboundMessage.status == MessageStatus.SCHEDULED,
                OutboundMessage.enqueued_at.is_not(None),
                OutboundMessage.scheduled_for < now - STALE_AFTER,
            )
        ).all()
        cancelled = db.scalars(
            select(OutboundMessage).where(
                OutboundMessage.status == MessageStatus.CANCELLED, OutboundMessage.enqueued_at.is_not(None)
            )
        ).all()
        removals = [m.id for m in cancelled]
        for m in cancelled:
            m.enqueued_at = None
        leads = db.scalars(
            select(Lead).where(
                Lead.research_status == ResearchStatus.QUEUED, Lead.research_enqueued_at.is_(None)
            )
        ).all()
        research = [lead.id for lead in leads]
        for lead in leads:
            lead.research_enqueued_at = now
    return {"now": now, "sends": sends, "stale": list(stale), "removals": removals, "research": research}


async def dispatch(send_q: Queue, research_q: Queue) -> str:
    work = await asyncio.to_thread(_collect_dispatch_work)
    now = work["now"]
    for message_id, when in work["sends"]:
        delay_ms = max(0, int((when - now).total_seconds() * 1000))
        await send_q.add("send", {"message_id": message_id}, {**SEND_JOB_OPTS, "jobId": msg_job_id(message_id), "delay": delay_ms})
    requeued = 0
    for message_id in work["stale"]:
        if await send_q.getJobState(msg_job_id(message_id)) == "unknown":
            await send_q.add("send", {"message_id": message_id}, {**SEND_JOB_OPTS, "jobId": msg_job_id(message_id)})
            requeued += 1
    for message_id in work["removals"]:
        try:
            await send_q.remove(msg_job_id(message_id))
        except Exception:  # active/locked jobs can't be removed; they will no-op anyway
            pass
    for lead_id in work["research"]:
        await research_q.add(
            "research", {"lead_id": lead_id}, {**RESEARCH_JOB_OPTS, "jobId": f"research-{lead_id}-{int(now.timestamp())}"}
        )
    return (
        f"{len(work['sends'])} sends, {requeued} requeued, {len(work['removals'])} removed, "
        f"{len(work['research'])} research"
    )


# --- processors ---------------------------------------------------------------------
class Processors:
    def __init__(self, redis: aioredis.Redis, send_q: Queue, research_q: Queue):
        self.redis = redis
        self.send_q = send_q
        self.research_q = research_q

    async def send(self, job, token):
        message_id = int(job.data["message_id"])
        mailbox_id = await asyncio.to_thread(self._mailbox_id, message_id)
        if mailbox_id is None:
            return "skipped: message gone"
        lock = self.redis.lock(f"{get_settings().queue_prefix}:lock:mailbox:{mailbox_id}", timeout=120, blocking_timeout=60)
        if not await lock.acquire():
            raise RuntimeError(f"mailbox {mailbox_id} busy; will retry")
        try:
            outcome = await asyncio.to_thread(self._send_sync, message_id)
        finally:
            try:
                await lock.release()
            except Exception:  # lock expired; nothing to release
                pass
        if outcome.action == "delayed" and outcome.retry_at:
            # retry_at is naive UTC (see models.utcnow)
            ts = int(outcome.retry_at.replace(tzinfo=UTC).timestamp() * 1000)
            await job.moveToDelayed(ts, token)
            raise DelayedError()
        return f"{outcome.action}: {outcome.detail}".strip(": ")

    @staticmethod
    def _mailbox_id(message_id: int) -> int | None:
        with session_scope() as db:
            msg = db.get(OutboundMessage, message_id)
            return msg.enrollment.mailbox_id if msg else None

    @staticmethod
    def _send_sync(message_id: int) -> delivery.Outcome:
        with session_scope() as db:
            try:
                return delivery.process_send(db, message_id)
            except sender.SendError:
                db.commit()  # keep the attempt count and error, then let BullMQ retry
                raise

    async def research(self, job, token):
        from growthstack.ai.pipeline import run_for_lead

        result = await asyncio.to_thread(run_for_lead, int(job.data["lead_id"]))
        return str(result.get("outcome", "interrupted" if "__interrupt__" in result else "done"))

    async def maintenance(self, job, token):
        if job.name == "dispatch":
            return await dispatch(self.send_q, self.research_q)
        if job.name not in tasks.PERIODIC:
            raise ValueError(f"unknown maintenance job {job.name}")
        fn, _ = tasks.PERIODIC[job.name]
        return await asyncio.to_thread(tasks.run_tracked, job.name, fn)


async def install_schedulers(maint_q: Queue) -> None:
    await maint_q.upsertJobScheduler("dispatch", {"every": 15_000}, "dispatch", {}, {"removeOnComplete": 50, "removeOnFail": 200})
    for name, (_, every) in tasks.PERIODIC.items():
        await maint_q.upsertJobScheduler(name, {"every": every * 1000}, name, {}, {"removeOnComplete": 20, "removeOnFail": 100})


async def run_worker(concurrency: int = 4) -> None:
    problems = get_settings().check_production_safety()
    if problems:
        raise RuntimeError("Refusing to start the worker:\n- " + "\n- ".join(problems))
    opts = queue_opts()
    redis = aioredis.from_url(get_settings().redis_url)
    send_q, research_q, maint_q = Queue(SEND, opts), Queue(RESEARCH, opts), Queue(MAINTENANCE, opts)
    await install_schedulers(maint_q)
    p = Processors(redis, send_q, research_q)
    workers = [
        Worker(SEND, p.send, {**opts, "concurrency": concurrency}),
        Worker(RESEARCH, p.research, {**opts, "concurrency": 2, "lockDuration": 300_000}),
        Worker(MAINTENANCE, p.maintenance, {**opts, "concurrency": 2, "lockDuration": 600_000}),
    ]
    for w in workers:
        w.on("failed", lambda job, err, *_: log.warning("job %s failed: %s", getattr(job, "id", "?"), err))
    log.info("worker started")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loop: Ctrl+C still stops the process
            pass
    await stop.wait()
    log.info("worker stopping")
    for w in workers:
        await w.close()
    for q in (send_q, research_q, maint_q):
        await q.close()
    await redis.aclose()
