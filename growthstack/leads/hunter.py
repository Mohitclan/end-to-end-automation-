"""Hunter.io domain search. The free plan has 50 credits/month, so keep limits small."""

from __future__ import annotations

import httpx

from growthstack.config import Settings, get_settings

API_URL = "https://api.hunter.io/v2"
TIMEOUT = httpx.Timeout(20.0)


class HunterError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class HunterRateLimitError(HunterError):
    pass


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _bare_domain(domain: str) -> str:
    d = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/", 1)[0]
    return d[4:] if d.startswith("www.") else d


class HunterClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None, base_url: str = API_URL):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=TIMEOUT)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> HunterClient | None:
        s = settings or get_settings()
        if not s.hunter_api_key:
            return None
        return cls(s.hunter_api_key.get_secret_value())

    def close(self) -> None:
        self._http.close()

    def domain_search(self, domain: str, limit: int = 10) -> list[dict]:
        """Each result: email, first_name, last_name, position, confidence,
        verification_status, source_uris, organization."""
        params = {"domain": _bare_domain(domain), "limit": max(1, int(limit))}
        try:
            resp = self._http.get(
                f"{self.base_url}/domain-search", params=params, headers={"X-API-KEY": self._api_key}
            )
        except httpx.HTTPError as exc:
            raise HunterError(f"Hunter domain search failed: {type(exc).__name__}") from None
        if resp.status_code == 429:
            raise HunterRateLimitError("Hunter rate limited (429)", 429, _retry_after(resp))
        if resp.status_code == 404:
            return []
        if not resp.is_success:
            raise HunterError(f"Hunter domain search returned {resp.status_code}: {resp.text[:300]}", resp.status_code)
        data = resp.json().get("data") or {}
        org = data.get("organization")
        out = []
        for e in data.get("emails") or []:
            if not e.get("value"):
                continue
            out.append(
                {
                    "email": e["value"].strip().lower(),
                    "first_name": e.get("first_name"),
                    "last_name": e.get("last_name"),
                    "position": e.get("position"),
                    "confidence": e.get("confidence"),
                    "verification_status": (e.get("verification") or {}).get("status"),
                    "source_uris": [s["uri"] for s in e.get("sources") or [] if s.get("uri")],
                    "organization": org,
                }
            )
        return out
