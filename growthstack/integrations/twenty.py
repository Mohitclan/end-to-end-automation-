"""Twenty CRM REST client (people, companies, opportunities)."""

from __future__ import annotations

from typing import Any

import httpx

from growthstack.config import Settings, get_settings

TIMEOUT = httpx.Timeout(20.0)
DEFAULT_STAGES = ("NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER")


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
    """Quote a value for a Twenty filter expression, escaping backslashes and double quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def normalize_domain(domain: str) -> str:
    """'Example.com/about', 'http://example.com' -> 'https://example.com'."""
    d = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/", 1)[0].split("?", 1)[0]
    return f"https://{d}"


class TwentyClient:
    def __init__(self, api_url: str, api_key: str, http: httpx.Client | None = None):
        self.api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=TIMEOUT)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> TwentyClient | None:
        s = settings or get_settings()
        if not s.twenty_api_url or not s.twenty_api_key:
            return None
        return cls(s.twenty_api_url, s.twenty_api_key.get_secret_value())

    def close(self) -> None:
        self._http.close()

    # -- low level -----------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        try:
            resp = self._http.request(method, f"{self.api_url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise TwentyError(f"Twenty {method} {path} failed: {type(exc).__name__}") from None
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
        if domain:
            existing = self.find_company_by_domain(domain)
            if existing:
                return existing["id"]
            payload["domainName"] = {"primaryLinkUrl": normalize_domain(domain)}
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
