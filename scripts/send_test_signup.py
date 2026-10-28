"""Send one server-side `user_signed_up` event to PostHog, exactly as the website's
backend should after a real signup (task P2.03). Use it to test the signup path end to
end (acceptance step A.08) before the website backend sends real ones.

    .venv/bin/python scripts/send_test_signup.py --email you+signup-test@yourcompany.com --first-name Test --last-name Signup

Needs POSTHOG_PROJECT_API_KEY and POSTHOG_REGION (read from .env). This sends a real
event into your PostHog project, so only use internal test addresses. Then run
`growthstack run-job posthog_pull` and the address appears as a converted lead.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from growthstack.config import get_settings  # noqa: E402
from growthstack.integrations.posthog_client import INGEST_HOSTS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", required=True, help="an internal test address")
    parser.add_argument("--first-name", default="Test")
    parser.add_argument("--last-name", default="Signup")
    parser.add_argument("--distinct-id", help="defaults to a fresh test id")
    args = parser.parse_args(argv)

    s = get_settings()
    if not s.posthog_project_api_key:
        print("POSTHOG_PROJECT_API_KEY is not set in .env", file=sys.stderr)
        return 2
    region = s.posthog_region.strip().lower()
    if region not in INGEST_HOSTS:
        print(f"POSTHOG_REGION must be 'eu' or 'us', not {s.posthog_region!r}", file=sys.stderr)
        return 2

    from posthog import Posthog

    client = Posthog(s.posthog_project_api_key, host=INGEST_HOSTS[region], sync_mode=True)
    distinct_id = args.distinct_id or f"growthstack-test-{uuid.uuid4()}"
    client.capture(
        s.posthog_signup_event,
        distinct_id=distinct_id,
        properties={
            # growthstack reads these person properties (posthog_client.recent_signups)
            "$set": {"email": args.email.strip(), "first_name": args.first_name, "last_name": args.last_name},
            "source": "growthstack send_test_signup.py",
        },
    )
    client.shutdown()
    print(f"sent {s.posthog_signup_event} for {args.email} (distinct_id {distinct_id}) to {INGEST_HOSTS[region]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
