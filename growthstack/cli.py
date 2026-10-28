"""Command line: `growthstack <command>`. Run `growthstack -h` for the list."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys

from growthstack.config import get_settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        for key in ("enrollment_id", "reason", "channel", "status", "subject"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=get_settings().log_level.upper(), handlers=[handler], force=True)


def cmd_migrate(_args) -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    # Run from the repo / image root (where alembic.ini lives); fall back to the source tree.
    candidates = [Path.cwd() / "alembic.ini", Path(__file__).resolve().parent.parent / "alembic.ini"]
    ini = next((p for p in candidates if p.is_file()), None)
    if ini is None:
        sys.exit("alembic.ini not found; run this from the project (or image) root")
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    command.upgrade(cfg, "head")
    print("database is up to date")


def cmd_create_user(args) -> None:
    from sqlalchemy import select

    from growthstack.db import session_scope
    from growthstack.models import User
    from growthstack.security import hash_password

    password = args.password or getpass.getpass("Password (12+ characters): ")
    with session_scope() as db:
        email = args.email.strip().lower()
        user = db.scalar(select(User).where(User.email == email))
        if user:
            user.password_hash = hash_password(password)
            print(f"password updated for {email}")
        else:
            db.add(User(email=email, password_hash=hash_password(password)))
            print(f"created {email}")


def cmd_web(args) -> None:
    import uvicorn

    uvicorn.run(
        "growthstack.web.app:app",
        host=args.host,
        port=args.port,
        proxy_headers=True,
        # Only trust X-Forwarded-For from the reverse proxy, never "*" (spoofable).
        forwarded_allow_ips=get_settings().forwarded_allow_ips,
        log_config=None,
    )


def cmd_worker(args) -> None:
    from growthstack.jobs.worker import run_worker

    asyncio.run(run_worker(concurrency=args.concurrency))


def cmd_run_job(args) -> None:
    from growthstack.jobs import tasks

    if args.name == "research":
        from growthstack.ai.pipeline import run_for_lead

        print(run_for_lead(int(args.arg)).get("outcome", "waiting for approval"))
        return
    if args.name not in tasks.PERIODIC:
        sys.exit(f"unknown job; choose from: research, {', '.join(tasks.PERIODIC)}")
    print(tasks.run_tracked(args.name, tasks.PERIODIC[args.name][0]))


def cmd_check_config(_args) -> None:
    s = get_settings()
    problems = s.check_production_safety()
    print(f"environment: {s.environment}; database: {s.database_url.split('@')[-1]}; llm: {s.llm_provider}")
    print(f"email dry run: {s.email_dry_run}")
    for p in problems:
        print(f"PROBLEM: {p}")
    sys.exit(1 if problems else 0)


def cmd_seed_demo(_args) -> None:
    """A mailbox and a 3-step sequence so a fresh install can be tried end to end."""
    from sqlalchemy import select

    from growthstack.db import session_scope
    from growthstack.models import Mailbox, Sequence, SequenceStep

    with session_scope() as db:
        if not db.scalar(select(Mailbox).limit(1)):
            db.add(Mailbox(from_email="hello@example.com", from_name="Alex from Example", daily_limit=30))
        if not db.scalar(select(Sequence).limit(1)):
            seq = Sequence(name="Default outreach")
            seq.steps = [
                SequenceStep(position=1, delay_hours=0, subject_template="", body_template="(AI draft, approved per lead)"),
                SequenceStep(
                    position=2, delay_hours=72, subject_template="",
                    body_template="Hi {{ first_name }},\n\nJust bringing this back to the top of your inbox. "
                    "Would it be worth a short call?\n\nAlex",
                ),
                SequenceStep(
                    position=3, delay_hours=120, subject_template="",
                    body_template="Hi {{ first_name }},\n\nI won't keep emailing. If helping {{ company }} with this "
                    "ever becomes a priority, just reply and I'll pick it up.\n\nAlex",
                ),
            ]
            db.add(seq)
    print("seeded a demo mailbox (hello@example.com) and a 3-step sequence")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="growthstack")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create/upgrade database tables").set_defaults(fn=cmd_migrate)
    p = sub.add_parser("create-user", help="create a team member login (or reset a password)")
    p.add_argument("email")
    p.add_argument("--password", help="omit to be prompted (keeps it out of shell history)")
    p.set_defaults(fn=cmd_create_user)
    p = sub.add_parser("web", help="run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_web)
    p = sub.add_parser("worker", help="run the BullMQ workers and job schedulers")
    p.add_argument("--concurrency", type=int, default=4)
    p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("run-job", help="run one periodic job now (or 'research <lead_id>')")
    p.add_argument("name")
    p.add_argument("arg", nargs="?")
    p.set_defaults(fn=cmd_run_job)
    sub.add_parser("check-config", help="report configuration problems").set_defaults(fn=cmd_check_config)
    sub.add_parser("seed-demo", help="add a demo mailbox and sequence").set_defaults(fn=cmd_seed_demo)
    args = parser.parse_args(argv)
    setup_logging()
    args.fn(args)


if __name__ == "__main__":
    main()
