"""Call every PostHog query method against the LIVE API and print what comes back
(task P2.09). Read-only: it only runs HogQL queries.

    .venv/bin/python scripts/check_posthog.py
    .venv/bin/python scripts/check_posthog.py --email someone-who-signed-up@yourcompany.com

Needs POSTHOG_PERSONAL_API_KEY (Query Read scope), POSTHOG_PROJECT_ID and
POSTHOG_REGION in .env. Exit code 0 = every call worked.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from growthstack.config import get_settings  # noqa: E402
from growthstack.integrations.posthog_client import PostHogError, PostHogQueryClient  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", default="nobody@example.com", help="an email to count events for")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    s = get_settings()
    client = PostHogQueryClient.from_settings(s)
    if client is None:
        print("Not configured: set POSTHOG_PERSONAL_API_KEY and POSTHOG_PROJECT_ID in .env", file=sys.stderr)
        return 2
    print(f"PostHog {client.host}, project {client.project_id}, signup event {s.posthog_signup_event!r}\n")

    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=args.days)
    checks = [
        ("daily_unique_visitors", lambda: client.daily_unique_visitors(args.days)),
        ("daily_event_counts", lambda: client.daily_event_counts(s.posthog_signup_event, args.days)),
        ("person_event_count", lambda: client.person_event_count(args.email, 30)),
        ("person_event_counts", lambda: client.person_event_counts([args.email, "nobody@example.com"], 30)),
        ("recent_signups", lambda: client.recent_signups(s.posthog_signup_event, since)),
    ]
    failed = 0
    for name, call in checks:
        try:
            result = call()
        except PostHogError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
            continue
        if isinstance(result, dict):
            shown = {str(k): v for k, v in sorted(result.items(), key=lambda kv: str(kv[0]))}
        elif isinstance(result, list):
            shown = f"{len(result)} rows, first: {result[0] if result else None}"
        else:
            shown = result
        print(f"ok   {name}: {shown}")
    client.close()
    print("\nall calls worked" if not failed else f"\n{failed} call(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
