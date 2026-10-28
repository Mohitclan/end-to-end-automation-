"""Twenty CRM REST client (people, companies, opportunities).

Checked against Twenty v2.52.0's server code:
- Company ``domainName`` is a unique LINKS field of the "domain" kind. Since v2.38,
  Twenty stores it as the bare host ("https://www.Acme.com/x" -> "acme.com"), so we
  send and search for that form.
- Person ``emails.primaryEmail`` is unique and stored lowercased.
- Filter values are quoted with " or ' and never unescaped. A quote of the same
  kind ends the value, so we pick the quote character the value does not contain.
- The API allows 100 requests a minute per API key, so the client paces itself. Its 429s
  carry ``Retry-After: 1`` (a token bucket refilling continuously), so one retry recovers.
These were confirmed against a live v2.52.0 instance as well.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from growthstack.config import Settings, get_settings

TIMEOUT = httpx.Timeout(20.0)
DEFAULT_STAGES = ("NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER")
RATE_LIMIT = 90  # requests per RATE_WINDOW: Twenty allows 100 per key, leave some headroom
RATE_WINDOW = 60.0
MAX_RETRY_WAIT = 60.0  # longest Retry-After we wait out once before giving up
_SCHEME_PREFIX = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


class TwentyError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class TwentyRateLimitError(TwentyError):
    pass


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _filter_value(value: str) -> str:
    """Quote a value for a Twenty filter expression. Twenty has no escapes: the value
    runs to the next quote of the same kind, so use whichever kind it doesn't contain."""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    raise ValueError("Twenty filters cannot match a value containing both ' and \"")


def normalize_domain(domain: str) -> str:
    """The bare host Twenty stores for a company domain (its normalizeDomain):
    'https://user@www.Acme.com:8080/about?x=1' -> 'acme.com'."""
    d = _SCHEME_PREFIX.sub("", domain.strip())
    for sep in "/\\?#":
        d = d.split(sep, 1)[0]
    d = d.rsplit("@", 1)[-1].lower()
    host, _, port = d.rpartition(":")
    if host and port.isdigit():
        d = host
    labels = d.split(".")
    while labels and labels[-1] == "":
        labels.pop()
    while labels and labels[0] == "www":
        labels.pop(0)
    d = ".".join(labels)
    try:
        return d.encode("idna").decode("ascii")
    except UnicodeError:
        return d


class _Pacer:
    """Sliding-window limiter: at most ``limit`` calls in any ``window`` seconds."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.limit, self.window = limit, window
        self._clock, self._sleep = clock, sleep
        self._sent: deque[float] = deque()

    def wait(self) -> None:
        now = self._clock()
        while self._sent and now - self._sent[0] >= self.window:
            self._sent.popleft()
        if len(self._sent) >= self.limit:
            self._sleep(self.window - (now - self._sent[0]))
            self._sent.popleft()
        self._sent.append(self._clock())


class TwentyClient:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        http: httpx.Client | None = None,
        *,
        rate_limit: int = RATE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=TIMEOUT)
        self._sleep = sleep
        self._pacer = _Pacer(rate_limit, RATE_WINDOW, clock, sleep)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> TwentyClient | None:
        s = settings or get_settings()
        if not s.twenty_api_url or not s.twenty_api_key:
            return None
        return cls(s.twenty_api_url, s.twenty_api_key.get_secret_value())

    def close(self) -> None:
        self._http.close()

    # -- low level -----------------------------------------------------------
    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        self._pacer.wait()
        try:
            return self._http.request(method, f"{self.api_url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise TwentyError(f"Twenty {method} {path} failed: {type(exc).__name__}") from None

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        resp = self._send(method, path, **kwargs)
        if resp.status_code == 429:
            # Someone else used up the shared limit: wait it out once, then give up and
            # let the job resume on its next run.
            wait = _retry_after(resp)
            wait = RATE_WINDOW if wait is None else wait
            if wait <= MAX_RETRY_WAIT:
                self._sleep(wait)
                resp = self._send(method, path, **kwargs)
        if resp.status_code == 429:
            raise TwentyRateLimitError(
                f"Twenty rate limited (429) on {method} {path}", 429, _retry_after(resp)
            )
        if not resp.is_success:
            raise TwentyError(
                f"Twenty {method} {path} returned {resp.status_code}: {resp.text[:300]}",
                resp.status_code,
            )
        return resp.json()

    def _find_one(self, collection: str, filter_expr: str) -> dict | None:
        body = self._request("GET", f"/{collection}", params={"filter": filter_expr, "limit": 1})
        items = (body.get("data") or {}).get(collection) or []
        return items[0] if items else None

    def _create(self, collection: str, key: str, payload: dict) -> str:
        body = self._request("POST", f"/{collection}", json=payload)
        return body["data"][key]["id"]

    # -- public API ----------------------------------------------------------
    def find_company_by_domain(self, domain: str) -> dict | None:
        expr = f"domainName.primaryLinkUrl[eq]:{_filter_value(normalize_domain(domain))}"
        return self._find_one("companies", expr)

    def find_person_by_email(self, email: str) -> dict | None:
        expr = f"emails.primaryEmail[eq]:{_filter_value(email.strip().lower())}"
        return self._find_one("people", expr)

    def upsert_company(self, name: str, domain: str | None = None) -> str:
        """Return the id of the company with this domain, creating it if needed.
        Without a domain a new company is always created."""
        payload: dict[str, Any] = {"name": name}
        domain = normalize_domain(domain) if domain else ""
        if domain:
            existing = self.find_company_by_domain(domain)
            if existing:
                return existing["id"]
            payload["domainName"] = {"primaryLinkUrl": domain}
        return self._create("companies", "createCompany", payload)

    def upsert_person(
        self,
        email: str,
        first_name: str | None = None,
        last_name: str | None = None,
        title: str | None = None,
        company_id: str | None = None,
    ) -> str:
        """Find the person by email; PATCH the given (non-None) fields if found, else create."""
        email = email.strip().lower()
        payload: dict[str, Any] = {}
        if first_name is not None or last_name is not None:
            payload["name"] = {"firstName": first_name or "", "lastName": last_name or ""}
        if title is not None:
            payload["jobTitle"] = title
        if company_id is not None:
            payload["companyId"] = company_id

        existing = self.find_person_by_email(email)
        if existing:
            if payload:
                self._request("PATCH", f"/people/{existing['id']}", json=payload)
            return existing["id"]
        payload["emails"] = {"primaryEmail": email}
        return self._create("people", "createPerson", payload)

    def create_opportunity(
        self,
        name: str,
        stage: str = "NEW",
        company_id: str | None = None,
        person_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {"name": name, "stage": stage}
        if company_id:
            payload["companyId"] = company_id
        if person_id:
            payload["pointOfContactId"] = person_id
        return self._create("opportunities", "createOpportunity", payload)

    def destroy(self, collection: str, record_id: str) -> None:
        """Permanently delete a record (Twenty's REST DELETE destroys unless soft_delete=true).
        Only used to clean up test records."""
        self._request("DELETE", f"/{collection}/{record_id}")
