"""Check the Twenty and listmonk clients against the live APIs (build-day P5.04 and P5.06).

Run inside the worker container, where the staging .env is loaded:

    docker compose exec worker python -m growthstack.integrations.live_check twenty
    docker compose exec worker python -m growthstack.integrations.live_check listmonk

`twenty` runs every client method twice against a test company and person, checks the
second run found the same records instead of creating duplicates, opens a SCREENING
opportunity, then deletes all three test records (keep them with --keep).
`listmonk` only reads: it reports what the next listmonk_sync would pull.
Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import sys

from growthstack.integrations.listmonk import ListmonkClient, ListmonkError
from growthstack.integrations.twenty import TwentyClient, TwentyError

TEST_COMPANY = "Build Day Test Ltd"
TEST_DOMAIN = "buildday-test.example"


class _Report:
    def __init__(self) -> None:
        self.failed = False

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        print(f"{'OK  ' if ok else 'FAIL'} {what}{f': {detail}' if detail else ''}")
        self.failed |= not ok
        return ok


def check_twenty(client: TwentyClient, email: str, keep: bool = False) -> bool:
    r = _Report()
    created: list[tuple[str, str]] = []  # deleted in reverse order: opportunity, people, companies

    def track(collection: str, record_id: str) -> str:
        if (collection, record_id) not in created:
            created.append((collection, record_id))
        return record_id

    try:
        cid = track("companies", client.upsert_company(TEST_COMPANY, TEST_DOMAIN))
        pid = track("people", client.upsert_person(email, "Test", "Person", "CTO", cid))
        r.check(True, "upsert_company and upsert_person", f"company {cid}, person {pid}")

        cid2 = track("companies", client.upsert_company(TEST_COMPANY, f"https://www.{TEST_DOMAIN}/"))
        pid2 = track("people", client.upsert_person(email, "Test", "Person", "CTO", cid2))
        r.check(cid2 == cid, "second upsert_company found the same company", cid2)
        r.check(pid2 == pid, "second upsert_person found the same person", pid2)

        found = client.find_person_by_email(email.upper())
        r.check(bool(found) and found["id"] == pid, "find_person_by_email", f"{found and found.get('id')}")

        try:
            oid = track("opportunities", client.create_opportunity(
                f"{TEST_COMPANY} - outreach reply", "SCREENING", cid, pid
            ))
            r.check(True, "create_opportunity at SCREENING", oid)
        except TwentyError as exc:
            r.check(False, "create_opportunity at SCREENING",
                    f"{exc} (if the stage was renamed, use its real value in jobs/tasks.py)")
    except TwentyError as exc:
        r.check(False, "Twenty API", f"{exc} (401: check the key and that TWENTY_API_URL ends in /rest)")
    finally:
        if not keep:
            for collection, record_id in reversed(created):
                try:
                    client.destroy(collection, record_id)
                except TwentyError as exc:
                    r.check(False, f"delete test record {collection}/{record_id}", str(exc))
            if created:
                print(f"Deleted {len(created)} test records.")
    return not r.failed


def check_listmonk(client: ListmonkClient, email: str | None = None) -> bool:
    r = _Report()
    try:
        blocked = client.blocklisted_emails()
        unsubscribed = client.unsubscribed_emails()
        r.check(True, "read subscribers",
                f"{len(blocked)} blocklisted, {len(unsubscribed)} unsubscribed from a list")
        if email:
            sub_id = client.find_subscriber_id(email)
            r.check(sub_id is not None, f"find_subscriber_id({email})", str(sub_id))
    except ListmonkError as exc:
        hint = (
            " (403: give the API user's role subscribers:get_all, subscribers:manage and lists:get_all)"
            if exc.status_code == 403
            else ""
        )
        r.check(False, "listmonk API", f"{exc}{hint}")
    return not r.failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m growthstack.integrations.live_check")
    sub = parser.add_subparsers(dest="target", required=True)
    p = sub.add_parser("twenty", help="run every Twenty method twice on test records, then delete them")
    p.add_argument("--email", default=f"buildday-test@{TEST_DOMAIN}")
    p.add_argument("--keep", action="store_true", help="leave the test records in Twenty")
    p = sub.add_parser("listmonk", help="read-only: what the next listmonk_sync would pull")
    p.add_argument("--email", help="also look up this subscriber")
    args = parser.parse_args(argv)

    if args.target == "twenty":
        client = TwentyClient.from_settings()
        if client is None:
            print("Twenty not configured: set TWENTY_API_URL and TWENTY_API_KEY")
            return 1
        return 0 if check_twenty(client, args.email, args.keep) else 1
    client = ListmonkClient.from_settings()
    if client is None:
        print("listmonk not configured: set LISTMONK_URL, LISTMONK_API_USER and LISTMONK_API_TOKEN")
        return 1
    return 0 if check_listmonk(client, args.email) else 1


if __name__ == "__main__":
    sys.exit(main())
