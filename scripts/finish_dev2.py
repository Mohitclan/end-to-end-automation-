"""Finish and verify Developer 2's phase in one command, once the keys are in .env.

    .venv/bin/python scripts/finish_dev2.py

It (1) lists every Developer 2 setting that is still missing, (2) runs the live PostHog
query check (P2.09), (3) runs the three measurement jobs (P2.08), and (4) prints the
numbers the dashboard now shows, so they can be compared with PostHog, Search Console
and pagespeed.web.dev. Read-only towards PostHog and Google; it writes only to the local
growthstack database. Exit code 0 = everything configured and every step worked.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from growthstack.config import get_settings  # noqa: E402


def missing_settings(s) -> list[str]:
    gaps = []
    if not s.posthog_project_api_key:
        gaps.append("POSTHOG_PROJECT_API_KEY (the public phc_ key)")
    if not s.posthog_personal_api_key:
        gaps.append("POSTHOG_PERSONAL_API_KEY (phx_ key with Query Read)")
    if not s.posthog_project_id:
        gaps.append("POSTHOG_PROJECT_ID (number in PostHog Project settings)")
    if s.posthog_region.strip().lower() not in ("us", "eu"):
        gaps.append("POSTHOG_REGION (us or eu)")
    if not s.gsc_site_url:
        gaps.append("GSC_SITE_URL (sc-domain:yourcompany.com or https://www.yourcompany.com/)")
    if not s.gsc_service_account_file:
        gaps.append("GSC_SERVICE_ACCOUNT_FILE (path to the service account JSON)")
    elif not Path(s.gsc_service_account_file).is_file():
        gaps.append(f"GSC_SERVICE_ACCOUNT_FILE points to a missing file: {s.gsc_service_account_file}")
    if not s.psi_url_list:
        gaps.append("PSI_URLS (home, pricing and signup pages, comma separated)")
    if not s.psi_api_key:
        gaps.append("PSI_API_KEY (Google Cloud key restricted to PageSpeed Insights)")
    return gaps


def main() -> int:
    s = get_settings()
    ok = True

    print("1. Settings")
    gaps = missing_settings(s)
    for g in gaps:
        print(f"   MISSING  {g}")
    if not gaps:
        print("   all Developer 2 settings are present")
    ok &= not gaps

    print("\n2. Live PostHog check (P2.09)")
    if s.posthog_personal_api_key and s.posthog_project_id:
        import check_posthog  # same folder

        ok &= check_posthog.main([]) == 0
    else:
        print("   skipped: needs the personal key and project id")
        ok = False

    print("\n3. Measurement jobs (P2.08)")
    from growthstack.jobs import tasks

    for name in ("posthog_pull", "gsc_pull", "psi_run"):
        try:
            result = tasks.run_tracked(name, tasks.PERIODIC[name][0])
        except Exception as exc:  # report every job, then fail at the end
            result, ok = f"FAILED: {type(exc).__name__}: {exc}", False
        if result.startswith("skipped"):
            ok = False
        print(f"   {name:<13} {result}")

    print("\n4. What the dashboard shows now")
    from growthstack.db import session_scope
    from growthstack.web import dashboard

    with session_scope() as db:
        d = dashboard.build(db)
        k = d["kpis"]
        print(f"   signups (30 days):        {k['signups_30d']}")
        print(f"   unique visitors (30 days): {k['visitors_30d']}")
        print(f"   search impressions (28d): {k['impressions_28d']}   clicks: {k['clicks_28d']}   "
              f"avg position: {k['avg_position']}")
        print(f"   search data ends:         {d['gsc_last_day']} (Google lags 2-3 days)")
        for p in d["page_scores"]:
            if p.error:
                print(f"   page score {p.url} [{p.strategy}]: ERROR {p.error[:100]}")
            else:
                print(f"   page score {p.url} [{p.strategy}]: perf {p.performance} seo {p.seo} "
                      f"a11y {p.accessibility} best {p.best_practices}")
        if not d["page_scores"]:
            print("   page scores: none yet")
        if any(p.error for p in d["page_scores"]):
            ok = False

    print("\nCompare these with PostHog, Search Console (Performance, same dates) and pagespeed.web.dev.")
    print("DONE: everything configured and working" if ok else "NOT DONE: fix the items above and run again")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
