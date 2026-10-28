from __future__ import annotations

import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from growthstack import dnc
from growthstack.ai import pipeline
from growthstack.ai.llm import AnthropicLLM, FakeLLM, LLMError, LLMRefusal
from growthstack.config import Settings
from growthstack.db import SessionLocal
from growthstack.leads.importer import import_csv, normalize_domain
from growthstack.models import (
    DncReason,
    Enrollment,
    EnrollmentStatus,
    Lead,
    LeadStage,
    MessageStatus,
    ResearchStatus,
)
from growthstack.research.crawler import ResearchResult, RobotsDisallowed
from growthstack.scoring import compute_score


# --- scoring ------------------------------------------------------------------------
def _settings(**kw):
    return Settings(icp_title_keywords="cto,founder", icp_countries="united kingdom", icp_sic_prefixes="620", **kw)


def test_score_combines_crm_research_and_engagement(db, seeded):
    lead = seeded["lead"]
    lead.research_fit, lead.posthog_events_30d = 80, 12
    r = compute_score(lead, _settings())
    assert r.breakdown["title"]["points"] == 20 and r.breakdown["country"]["points"] == 7.5
    assert r.breakdown["industry"]["points"] == 7.5 and r.breakdown["research"]["points"] == 32
    assert r.breakdown["engagement"]["points"] == 25
    assert r.score == 92


def test_score_penalises_bad_email(db, seeded):
    lead = seeded["lead"]
    lead.research_fit = 100
    lead.verification_status = "risky"
    assert compute_score(lead, _settings()).score == 20 + 7.5 + 7.5 + 40 - 10
    lead.verification_status = "invalid"
    assert compute_score(lead, _settings()).score == 0


def test_score_without_data_is_low_and_explained(db, seeded):
    lead = seeded["lead"]
    lead.title, lead.company = None, None
    r = compute_score(lead, Settings(icp_title_keywords="cto"))
    assert r.score == 8  # neutral half points for unset country/industry targets
    assert r.breakdown["research"]["why"] == "not researched yet"


# --- import -----------------------------------------------------------------------
CSV = (
    "Email,Name,Job Title,Company,Website\n"
    "jane@acme.io,Jane Doe,CTO,Acme,acme.io\n"
    "JANE@acme.io,Jane Dup,CTO,Acme,acme.io\n"
    "bob@gmail.com,Bob Smith,=HYPERLINK(\"x\"),Bobco,\n"
    "not-an-email,X,Y,Z,\n"
    "gone@blocked.com,Gone,CEO,Blocked,blocked.com\n"
)


def test_csv_import_records_source_skips_dnc_and_dupes(db):
    dnc.add(db, domain="blocked.com", reason=DncReason.MANUAL)
    r = import_csv(db, CSV.encode(), source_detail="SaaS Expo 2026 exhibitor list", imported_by="me@us.com")
    db.commit()
    assert (r.created, r.skipped_duplicate, r.skipped_dnc) == (2, 1, 1)
    assert len(r.errors) == 1 and "Row 5" in r.errors[0]
    jane = db.query(Lead).filter_by(email="jane@acme.io").one()
    assert (jane.first_name, jane.last_name, jane.source, jane.source_detail) == ("Jane", "Doe", "csv", "SaaS Expo 2026 exhibitor list")
    assert jane.company.domain == "acme.io" and jane.website == "https://acme.io"
    bob = db.query(Lead).filter_by(email="bob@gmail.com").one()
    assert bob.company.domain is None  # webmail domain is not a company domain
    assert bob.title == 'HYPERLINK("x")'  # formula prefix stripped


def test_csv_import_requires_source_and_email_column(db):
    assert import_csv(db, CSV.encode(), source_detail="", imported_by="me").errors
    assert "No email column" in import_csv(db, b"name,company\nA,B\n", source_detail="a list", imported_by="me").errors[0]


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.io/about") == "acme.io"
    assert normalize_domain("acme") is None


# --- LLM ------------------------------------------------------------------------
class _Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class _Resp:
    def __init__(self, stop_reason, content, stop_details=None):
        self.stop_reason, self.content, self.stop_details = stop_reason, content, stop_details


class _FakeClient:
    def __init__(self, resp):
        self.resp, self.calls = resp, []
        self.beta = self
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.resp


def test_anthropic_llm_request_shape_and_parsing():
    client = _FakeClient(_Resp("end_turn", [_Block("thinking"), _Block("text", '{"subject":"Hi","body":"Yo"}')]))
    llm = AnthropicLLM("claude-opus-5-5", "medium", client=client)
    assert llm.json(system="s", prompt="p", schema=pipeline.DRAFT_SCHEMA) == {"subject": "Hi", "body": "Yo"}
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["effort"] == "medium"


def test_anthropic_llm_refusal_and_truncation():
    class Details:
        category = "cyber"

    with pytest.raises(LLMRefusal):
        AnthropicLLM("m", client=_FakeClient(_Resp("refusal", [], Details()))).json(system="", prompt="", schema={})
    with pytest.raises(LLMError):
        AnthropicLLM("m", client=_FakeClient(_Resp("max_tokens", [_Block("text", "{")]))).json(system="", prompt="", schema={})


# --- LangGraph pipeline (research -> draft -> human approval) -----------------------
@pytest.fixture
def graph(database, tmp_path, monkeypatch):
    from growthstack.research import crawler
    from growthstack.verify import email_verifier

    monkeypatch.setattr(crawler, "fetch_site_text", lambda url, settings=None: ResearchResult(
        url=url, text="Acme builds B2B software for logistics teams with 50 employees.", provider="test"))
    monkeypatch.setattr(email_verifier, "verify_email", lambda email, **kw: type(
        "R", (), {"to_dict": lambda self: {"status": "valid", "reasons": []}})())
    conn = sqlite3.connect(str(tmp_path / "cp.sqlite"), check_same_thread=False)
    yield pipeline.build_graph(FakeLLM(), SqliteSaver(conn))
    conn.close()


def test_pipeline_pauses_for_approval_then_schedules(graph, seeded, monkeypatch):
    monkeypatch.setenv("MIN_SCORE_TO_DRAFT", "0")
    from growthstack.config import get_settings

    get_settings.cache_clear()
    lead_id = seeded["lead"].id
    result = pipeline.run_for_lead(lead_id, graph=graph)
    assert "__interrupt__" in result  # waiting for a person
    with SessionLocal() as s:
        lead = s.get(Lead, lead_id)
        assert lead.stage == LeadStage.AWAITING_APPROVAL and lead.research_status == ResearchStatus.DONE
        assert lead.score is not None and "B2B software" in lead.research_summary
        enrollment = s.query(Enrollment).filter_by(lead_id=lead_id).one()
        assert enrollment.status == EnrollmentStatus.PENDING_APPROVAL
        assert all(m.status == MessageStatus.DRAFT for m in enrollment.messages)
        eid = enrollment.id

    outcome = pipeline.resume_review(eid, {"action": "approve", "by": "me@us.com", "edits": {"1": ["Edited subject", "Edited body"]}}, graph=graph)
    assert outcome == "approved"
    with SessionLocal() as s:
        e = s.get(Enrollment, eid)
        assert e.status == EnrollmentStatus.ACTIVE and e.approved_by == "me@us.com"
        assert (e.messages[0].subject, e.messages[0].status) == ("Edited subject", MessageStatus.SCHEDULED)
    with pytest.raises(pipeline.sequencer.SequencerError):  # can't decide twice
        pipeline.resume_review(eid, {"action": "reject", "by": "x@us.com"}, graph=graph)


def test_pipeline_reject(graph, seeded, monkeypatch):
    monkeypatch.setenv("MIN_SCORE_TO_DRAFT", "0")
    from growthstack.config import get_settings

    get_settings.cache_clear()
    pipeline.run_for_lead(seeded["lead"].id, graph=graph)
    with SessionLocal() as s:
        eid = s.query(Enrollment).one().id
    assert pipeline.resume_review(eid, {"action": "reject", "by": "me@us.com", "reason": "wrong fit"}, graph=graph) == "rejected"
    with SessionLocal() as s:
        assert s.get(Enrollment, eid).status == EnrollmentStatus.REJECTED
        assert s.get(Lead, seeded["lead"].id).stage == LeadStage.REJECTED


def test_pipeline_below_threshold_does_not_draft(graph, seeded, monkeypatch):
    monkeypatch.setenv("MIN_SCORE_TO_DRAFT", "100")
    from growthstack.config import get_settings

    get_settings.cache_clear()
    result = pipeline.run_for_lead(seeded["lead"].id, graph=graph)
    assert result["outcome"] == "below_threshold" and "__interrupt__" not in result
    with SessionLocal() as s:
        assert s.query(Enrollment).count() == 0


def test_pipeline_robots_disallowed_continues_without_text(graph, seeded, monkeypatch):
    from growthstack.research import crawler

    def disallowed(url, settings=None):
        raise RobotsDisallowed("robots.txt disallows this page")

    monkeypatch.setattr(crawler, "fetch_site_text", disallowed)
    monkeypatch.setenv("MIN_SCORE_TO_DRAFT", "100")
    from growthstack.config import get_settings

    get_settings.cache_clear()
    pipeline.run_for_lead(seeded["lead"].id, graph=graph)
    with SessionLocal() as s:
        lead = s.get(Lead, seeded["lead"].id)
        assert "robots.txt" in lead.research_error and lead.research_text is None


def test_resume_falls_back_when_checkpoint_missing(graph, seeded, monkeypatch):
    monkeypatch.setenv("MIN_SCORE_TO_DRAFT", "0")
    from growthstack.config import get_settings

    get_settings.cache_clear()
    pipeline.run_for_lead(seeded["lead"].id, graph=graph)
    with SessionLocal() as s:
        lead = s.get(Lead, seeded["lead"].id)
        lead.graph_thread_id = "lost-thread"
        s.commit()
        eid = s.query(Enrollment).one().id
    assert pipeline.resume_review(eid, {"action": "approve", "by": "me@us.com"}, graph=graph) == "approved"


GOOD_DRAFT = "Hi Jane,\n\nSaw that Acme builds software for logistics teams.\n\nWould a short chat be useful?\n\nAlex"


def test_draft_problems_flags_mechanical_misses():
    assert pipeline.draft_problems(GOOD_DRAFT, "Jane", "Acme Ltd") == []
    long_linked = "Hi Jane, " + "word " * 130 + "see https://us.example.com or **this**?\n\nAlex"
    problems = " | ".join(pipeline.draft_problems(long_linked, "Jane", "Acme Ltd"))
    assert "words" in problems and "link" in problems and "markdown" in problems and "name Acme Ltd" in problems
    no_question = "Hi there,\n\nAcme looks great. Let me know.\n\nAlex"
    problems = " | ".join(pipeline.draft_problems(no_question, "Jane", "Acme Ltd"))
    assert "greet Jane" in problems and "one low-pressure question" in problems
    assert pipeline.draft_problems("Hi Jane, ACME looks busy. Worth a chat?\nAlex", "Jane", "The Acme Ltd") == []


class _ScriptedLLM(FakeLLM):
    """FakeLLM for analysis; draft calls return the scripted bodies in turn."""

    def __init__(self, bodies):
        self.bodies, self.draft_prompts = list(bodies), []

    def json(self, *, system, prompt, schema):
        if "subject" not in schema["properties"]:
            return super().json(system=system, prompt=prompt, schema=schema)
        self.draft_prompts.append(prompt)
        return {"subject": "Logistics software", "body": self.bodies.pop(0)}


@pytest.mark.parametrize("bodies,calls", [([GOOD_DRAFT], 1), (["Hi Jane, visit www.us.com today.", GOOD_DRAFT], 2)])
def test_draft_is_rewritten_once_when_checks_fail(database, tmp_path, seeded, monkeypatch, bodies, calls):
    monkeypatch.setenv("MIN_SCORE_TO_DRAFT", "0")
    from growthstack.config import get_settings
    from growthstack.research import crawler
    from growthstack.verify import email_verifier

    get_settings.cache_clear()
    monkeypatch.setattr(crawler, "fetch_site_text", lambda url, settings=None: ResearchResult(
        url=url, text="Acme builds software for logistics teams.", provider="test"))
    monkeypatch.setattr(email_verifier, "verify_email", lambda email, **kw: type(
        "R", (), {"to_dict": lambda self: {"status": "valid", "reasons": []}})())
    llm = _ScriptedLLM(bodies)
    conn = sqlite3.connect(str(tmp_path / "cp.sqlite"), check_same_thread=False)
    pipeline.run_for_lead(seeded["lead"].id, graph=pipeline.build_graph(llm, SqliteSaver(conn)))
    conn.close()
    assert len(llm.draft_prompts) == calls
    if calls == 2:
        assert "<previous_draft>Hi Jane, visit www.us.com today.</previous_draft>" in llm.draft_prompts[1]
        assert "link or web address" in llm.draft_prompts[1]
    with SessionLocal() as s:
        assert s.query(Enrollment).one().messages[0].body_text == GOOD_DRAFT
