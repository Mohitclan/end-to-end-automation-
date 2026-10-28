"""PostHog HogQL query client (read side; event capture uses the posthog SDK).

User-supplied values are never interpolated into HogQL: they are passed through
the HogQLQuery ``values`` dict and referenced as ``{placeholder}`` in the query.
Only validated integers are formatted into query text."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx

from growthstack.config import Settings, get_settings

TIMEOUT = httpx.Timeout(30.0)
HOSTS = {"us": "https://us.posthog.com", "eu": "https://eu.posthog.com"}  # query API
INGEST_HOSTS = {"us": "https://us.i.posthog.com", "eu": "https://eu.i.posthog.com"}  # event capture


class PostHogError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class PostHogRateLimitError(PostHogError):
    pass


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _to_naive_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _days(days: int) -> int:
    days = int(days)
    if days < 1:
        raise ValueError("days must be >= 1")
    return days


class PostHogQueryClient:
    def __init__(
        self,
        host: str,
        project_id: str,
        personal_api_key: str,
        http: httpx.Client | None = None,
    ):
        self.host = host.rstrip("/")
        self.project_id = project_id
        self._api_key = personal_api_key
        self._http = http or httpx.Client(timeout=TIMEOUT)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> PostHogQueryClient | None:
        s = settings or get_settings()
        if not s.posthog_personal_api_key or not s.posthog_project_id:
            return None
        host = HOSTS.get(s.posthog_region.strip().lower(), HOSTS["us"])
        return cls(host, s.posthog_project_id, s.posthog_personal_api_key.get_secret_value())

    def close(self) -> None:
        self._http.close()

    def hogql(self, query: str, values: dict[str, Any] | None = None) -> list[dict]:
        """Run a HogQL query and return rows as dicts keyed by column name."""
        q: dict[str, Any] = {"kind": "HogQLQuery", "query": query}
        if values:
            q["values"] = values
        url = f"{self.host}/api/projects/{self.project_id}/query/"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = self._http.post(url, json={"query": q}, headers=headers)
        except httpx.HTTPError as exc:
            raise PostHogError(f"PostHog query request failed: {type(exc).__name__}") from None
        if resp.status_code == 429:
            raise PostHogRateLimitError("PostHog rate limited (429)", 429, _retry_after(resp))
        if not resp.is_success:
            raise PostHogError(
                f"PostHog query returned {resp.status_code}: {resp.text[:300]}", resp.status_code
            )
        body = resp.json()
        columns = body.get("columns") or []
        return [dict(zip(columns, row, strict=False)) for row in body.get("results") or []]

    def daily_event_counts(self, event: str, days: int) -> dict[date, int]:
        rows = self.hogql(
            "SELECT toDate(timestamp) AS day, count() AS n FROM events "
            f"WHERE event = {{event}} AND timestamp >= now() - toIntervalDay({_days(days)}) "
            "GROUP BY day ORDER BY day",
            {"event": event},
        )
        return {_to_date(r["day"]): int(r["n"]) for r in rows}

    def daily_unique_visitors(self, days: int) -> dict[date, int]:
        rows = self.hogql(
            "SELECT toDate(timestamp) AS day, count(DISTINCT person_id) AS n FROM events "
            f"WHERE event = '$pageview' AND timestamp >= now() - toIntervalDay({_days(days)}) "
            "GROUP BY day ORDER BY day"
        )
        return {_to_date(r["day"]): int(r["n"]) for r in rows}

    def person_event_count(self, email: str, days: int) -> int:
        """Events in the last `days` by the person with this email (case-insensitive)."""
        return self.person_event_counts([email], days).get(email.strip().lower(), 0)

    def person_event_counts(self, emails: list[str], days: int, chunk: int = 200) -> dict[str, int]:
        """Event counts for many people at once, keyed by lower-cased email; emails with
        no events map to 0. One query per `chunk` emails instead of one per person,
        which keeps an hourly refresh of hundreds of leads well inside PostHog's
        query rate limit. Each email is its own `{eN}` placeholder (never text)."""
        wanted = sorted({e.strip().lower() for e in emails if e and e.strip()})
        counts = dict.fromkeys(wanted, 0)
        for i in range(0, len(wanted), max(1, int(chunk))):
            batch = wanted[i : i + max(1, int(chunk))]
            names = [f"e{n}" for n in range(len(batch))]
            rows = self.hogql(
                "SELECT lower(person.properties.email) AS email, count() AS n FROM events "
                f"WHERE lower(person.properties.email) IN ({', '.join('{' + n + '}' for n in names)}) "
                f"AND timestamp >= now() - toIntervalDay({_days(days)}) GROUP BY email",
                dict(zip(names, batch, strict=True)),
            )
            for r in rows:
                key = str(r.get("email") or "").lower()
                if key in counts:
                    counts[key] = int(r["n"])
        return counts

    def recent_signups(self, event: str, since: datetime, limit: int = 1000) -> list[dict]:
        """Signup events since ``since`` (naive = UTC), one per email, oldest first.
        Rows whose person has no email property are skipped. Keys: email, first_name,
        last_name, signed_up_at (naive UTC datetime), distinct_id."""
        since_utc = _to_naive_utc(since)
        rows = self.hogql(
            "SELECT person.properties.email AS email, person.properties.first_name AS first_name, "
            "person.properties.last_name AS last_name, person.properties.name AS name, "
            "timestamp AS signed_up_at, distinct_id FROM events "
            "WHERE event = {event} AND timestamp >= toDateTime({since}, 'UTC') "
            f"ORDER BY timestamp LIMIT {max(1, int(limit))}",
            {"event": event, "since": since_utc.strftime("%Y-%m-%d %H:%M:%S")},
        )
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            email = (r.get("email") or "").strip()
            if not email or email.lower() in seen:
                continue
            seen.add(email.lower())
            first, last = r.get("first_name") or None, r.get("last_name") or None
            if not first and not last and r.get("name"):
                parts = str(r["name"]).strip().split(None, 1)
                first = parts[0] if parts else None
                last = parts[1] if len(parts) > 1 else None
            out.append(
                {
                    "email": email,
                    "first_name": first,
                    "last_name": last,
                    "signed_up_at": _to_naive_utc(r.get("signed_up_at")),
                    "distinct_id": r.get("distinct_id"),
                }
            )
        return out
