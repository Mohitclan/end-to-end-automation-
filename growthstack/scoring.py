"""Lead scoring: one 0-100 number from CRM fields, website research and PostHog
activity. Deliberately a transparent weighted sum: every point is explained in
the stored breakdown so a person can see why a lead ranks where it does.

    CRM fit      up to 35  (job title 20, country 7.5, industry/SIC 7.5)
    Research fit up to 40  (the AI's 0-100 fit judgement x 0.4)
    Engagement   up to 25  (PostHog events by this person in the last 30 days)
    Email check  invalid address -> score 0; risky -> minus 10
"""

from __future__ import annotations

from dataclasses import dataclass

from growthstack.config import Settings, get_settings
from growthstack.models import Lead

WEIGHTS = {"title": 20.0, "country": 7.5, "industry": 7.5, "research": 40.0, "engagement": 25.0}
RISKY_EMAIL_PENALTY = 10.0


@dataclass
class ScoreResult:
    score: int
    breakdown: dict


def _engagement_points(events: int | None) -> tuple[float, str]:
    if events is None:
        return 0.0, "no PostHog data for this person"
    if events <= 0:
        return 0.0, "no site activity in 30 days"
    if events <= 2:
        return 10.0, f"{events} events in 30 days"
    if events <= 9:
        return 18.0, f"{events} events in 30 days"
    return WEIGHTS["engagement"], f"{events} events in 30 days"


def compute_score(lead: Lead, settings: Settings | None = None) -> ScoreResult:
    s = settings or get_settings()
    parts: dict[str, dict] = {}

    title = (lead.title or "").lower()
    hit = next((k for k in s.icp_titles if k in title), None)
    parts["title"] = {
        "points": WEIGHTS["title"] if hit else 0.0,
        "why": f"title matches '{hit}'" if hit else ("no title" if not title else "title outside target roles"),
    }

    country = (lead.country or (lead.company.country if lead.company else None) or "").lower()
    if not s.icp_country_list:
        parts["country"] = {"points": WEIGHTS["country"] / 2, "why": "no target countries set"}
    elif country and country in s.icp_country_list:
        parts["country"] = {"points": WEIGHTS["country"], "why": f"in target country {country}"}
    else:
        parts["country"] = {"points": 0.0, "why": "country unknown or outside targets"}

    sics = [str(c) for c in ((lead.company.sic_codes if lead.company else None) or [])]
    if not s.icp_sic_list:
        parts["industry"] = {"points": WEIGHTS["industry"] / 2, "why": "no target industries set"}
    elif any(code.startswith(p) for code in sics for p in s.icp_sic_list):
        parts["industry"] = {"points": WEIGHTS["industry"], "why": "SIC code in target industries"}
    else:
        parts["industry"] = {"points": 0.0, "why": "industry unknown or outside targets"}

    if lead.research_fit is None:
        parts["research"] = {"points": 0.0, "why": "not researched yet"}
    else:
        fit = max(0, min(100, lead.research_fit))
        parts["research"] = {"points": round(fit * WEIGHTS["research"] / 100, 1), "why": f"AI fit {fit}/100"}

    pts, why = _engagement_points(lead.posthog_events_30d)
    parts["engagement"] = {"points": pts, "why": why}

    total = sum(p["points"] for p in parts.values())
    status = lead.verification_status
    if status == "invalid":
        parts["email"] = {"points": -total, "why": "email address is undeliverable"}
        total = 0.0
    elif status == "risky":
        parts["email"] = {"points": -RISKY_EMAIL_PENALTY, "why": "email address is risky"}
        total -= RISKY_EMAIL_PENALTY

    score = int(round(max(0.0, min(100.0, total))))
    return ScoreResult(score=score, breakdown=parts)


def apply_score(lead: Lead, settings: Settings | None = None) -> ScoreResult:
    result = compute_score(lead, settings)
    lead.score = result.score
    lead.score_breakdown = result.breakdown
    return result
