from __future__ import annotations

import os

# Safe defaults before anything reads settings: never send real email or call a real LLM,
# and never read the developer's .env.
os.environ["GROWTHSTACK_ENV_FILE"] = ""
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-123456")
os.environ["EMAIL_DRY_RUN"] = "true"
os.environ["LLM_PROVIDER"] = "fake"
os.environ.setdefault("BASE_URL", "https://growth.example.com")

import pytest

from growthstack import db as dbmod
from growthstack.config import get_settings
from growthstack.db import Base


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A fresh SQLite database file per test (file, so every session sees the same data)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite"))
    get_settings.cache_clear()
    engine = dbmod.configure(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def db(database):
    session = dbmod.SessionLocal()
    yield session
    session.close()


@pytest.fixture
def seeded(db):
    """A mailbox, a 3-step sequence and one lead at a company."""
    from growthstack.models import Company, Lead, Mailbox, Sequence, SequenceStep

    mailbox = Mailbox(
        from_email="alex@send.example.com", from_name="Alex", daily_limit=2, min_interval_seconds=60,
        window_start_hour=0, window_end_hour=24, send_weekends=True,
    )
    seq = Sequence(name="Default")
    seq.steps = [
        SequenceStep(position=1, delay_hours=0, subject_template="", body_template="(ai)"),
        SequenceStep(position=2, delay_hours=72, subject_template="", body_template="Hi {{ first_name }}, following up."),
        SequenceStep(position=3, delay_hours=120, subject_template="Last note", body_template="Bye from us to {{ company }}."),
    ]
    company = Company(name="Acme Ltd", domain="acme.io", country="united kingdom", sic_codes=["62012"])
    lead = Lead(
        email="jane@acme.io", first_name="Jane", last_name="Doe", title="CTO", company=company,
        website="https://acme.io", source="csv", source_detail="SaaS Expo 2026 exhibitor list",
        verification_status="valid", verification={"status": "valid", "checked_at": "2099-01-01T00:00:00"},
    )
    db.add_all([mailbox, seq, company, lead])
    db.commit()
    return {"mailbox": mailbox, "sequence": seq, "lead": lead, "company": company}
