"""UK Companies House public data API: advanced company search and directors.
Rate limit is 600 requests per 5 minutes per key (429 beyond that)."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from growthstack.config import Settings, get_settings

API_URL = "https://api.company-information.service.gov.uk"
TIMEOUT = httpx.Timeout(20.0)


class CompaniesHouseError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class CompaniesHouseRateLimitError(CompaniesHouseError):
    pass


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def parse_officer_name(name: str) -> tuple[str, str]:
    """'SMITH, John Paul' -> ('John', 'Smith'). Middle names and trailing titles dropped."""
    if "," in name:
        parts = [p.strip() for p in name.split(",")]
        last = parts[0]
        forenames = parts[1].split() if len(parts) > 1 else []
        first = forenames[0] if forenames else ""
    else:
        tokens = name.split()
        first = tokens[0] if len(tokens) > 1 else ""
        last = tokens[-1] if tokens else ""
    return first.title(), last.title()


class CompaniesHouseClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None, base_url: str = API_URL):
        self.base_url = base_url.rstrip("/")
        self._auth = httpx.BasicAuth(api_key, "")
        self._http = http or httpx.Client(timeout=TIMEOUT)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> CompaniesHouseClient | None:
        s = settings or get_settings()
        if not s.companies_house_api_key:
            return None
        return cls(s.companies_house_api_key.get_secret_value())

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict | None:
        """GET returning parsed JSON, or None on 404 (Companies House's 'no results')."""
        try:
            resp = self._http.get(f"{self.base_url}{path}", params=params, auth=self._auth)
        except httpx.HTTPError as exc:
            raise CompaniesHouseError(f"Companies House GET {path} failed: {type(exc).__name__}") from None
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            raise CompaniesHouseRateLimitError("Companies House rate limited (429)", 429, _retry_after(resp))
        if not resp.is_success:
            raise CompaniesHouseError(
                f"Companies House GET {path} returned {resp.status_code}: {resp.text[:300]}", resp.status_code
            )
        return resp.json()

    def search_companies(
        self,
        name_includes: str | None = None,
        status: str | None = "active",
        sic_codes: list[str] | str | None = None,
        incorporated_from: date | str | None = None,
        location: str | None = None,
        size: int = 100,
        start_index: int = 0,
    ) -> list[dict]:
        """Advanced search. Each result: company_name, company_number, company_status,
        date_of_creation (str|None), sic_codes (list), registered_office_address
        (dict of address_line_1, locality, postal_code, country)."""
        params: dict[str, Any] = {"size": max(1, min(int(size), 5000)), "start_index": max(0, int(start_index))}
        if name_includes:
            params["company_name_includes"] = name_includes
        if status:
            params["company_status"] = status
        if sic_codes:
            params["sic_codes"] = sic_codes if isinstance(sic_codes, str) else ",".join(sic_codes)
        if incorporated_from:
            params["incorporated_from"] = (
                incorporated_from.isoformat() if isinstance(incorporated_from, date) else incorporated_from
            )
        if location:
            params["location"] = location

        body = self._get("/advanced-search/companies", params)
        if not body:
            return []
        out = []
        for item in body.get("items") or []:
            addr = item.get("registered_office_address") or {}
            out.append(
                {
                    "company_name": item.get("company_name", ""),
                    "company_number": item.get("company_number", ""),
                    "company_status": item.get("company_status"),
                    "date_of_creation": item.get("date_of_creation"),
                    "sic_codes": list(item.get("sic_codes") or []),
                    "registered_office_address": {
                        k: addr.get(k) for k in ("address_line_1", "locality", "postal_code", "country")
                    },
                }
            )
        return out

    def active_directors(self, company_number: str) -> list[dict]:
        """Current (not resigned) directors: name (raw), first_name, last_name, officer_role."""
        body = self._get(f"/company/{company_number}/officers", {"items_per_page": 100})
        if not body:
            return []
        out = []
        for item in body.get("items") or []:
            if item.get("resigned_on") or item.get("officer_role") != "director":
                continue
            first, last = parse_officer_name(item.get("name", ""))
            out.append(
                {"name": item.get("name", ""), "first_name": first, "last_name": last, "officer_role": "director"}
            )
        return out
