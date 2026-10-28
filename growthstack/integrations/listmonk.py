"""listmonk client: keep newsletter blocklist/unsubscribes in sync with our DNC list."""

from __future__ import annotations

from typing import Any

import httpx

from growthstack.config import Settings, get_settings

TIMEOUT = httpx.Timeout(20.0)


class ListmonkError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ListmonkRateLimitError(ListmonkError):
    pass


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _sql_str(value: str) -> str:
    """SQL string literal for listmonk's ``query`` parameter (single quotes doubled)."""
    return "'" + value.replace("'", "''") + "'"


class ListmonkClient:
    def __init__(self, base_url: str, api_user: str, api_token: str, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self._auth = f"token {api_user}:{api_token}"
        self._http = http or httpx.Client(timeout=TIMEOUT)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ListmonkClient | None:
        s = settings or get_settings()
        if not s.listmonk_url or not s.listmonk_api_user or not s.listmonk_api_token:
            return None
        return cls(s.listmonk_url, s.listmonk_api_user, s.listmonk_api_token.get_secret_value())

    def close(self) -> None:
        self._http.close()

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.request(
                method, f"{self.base_url}{path}", headers={"Authorization": self._auth}, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ListmonkError(f"listmonk {method} {path} failed: {type(exc).__name__}") from None
        if resp.status_code == 429:
            raise ListmonkRateLimitError("listmonk rate limited (429)", 429, _retry_after(resp))
        return resp

    @staticmethod
    def _check(resp: httpx.Response, what: str) -> dict:
        if not resp.is_success:
            raise ListmonkError(f"listmonk {what} returned {resp.status_code}: {resp.text[:300]}", resp.status_code)
        return resp.json()

    def _emails(self, params: dict) -> list[str]:
        body = self._check(self._send("GET", "/api/subscribers", params=params), "list subscribers")
        results = (body.get("data") or {}).get("results") or []
        seen: dict[str, None] = {}
        for r in results:
            if r.get("email"):
                seen.setdefault(r["email"].strip().lower(), None)
        return list(seen)

    def find_subscriber_id(self, email: str) -> int | None:
        params = {"query": f"subscribers.email={_sql_str(email.strip().lower())}", "per_page": 1}
        body = self._check(self._send("GET", "/api/subscribers", params=params), "find subscriber")
        results = (body.get("data") or {}).get("results") or []
        return results[0]["id"] if results else None

    def blocklist_email(self, email: str, name: str | None = None) -> None:
        """Create the subscriber as blocklisted, or blocklist the existing one."""
        email = email.strip().lower()
        payload = {"email": email, "name": name or email.split("@")[0], "status": "blocklisted", "lists": []}
        resp = self._send("POST", "/api/subscribers", json=payload)
        if resp.is_success:
            return
        exists = resp.status_code == 409 or (resp.status_code == 400 and "already exists" in resp.text.lower())
        if not exists:
            self._check(resp, "create subscriber")
        sub_id = self.find_subscriber_id(email)
        if sub_id is None:
            raise ListmonkError("listmonk reported subscriber exists but it could not be found")
        self._check(self._send("PUT", f"/api/subscribers/{sub_id}/blocklist"), "blocklist subscriber")

    def blocklisted_emails(self) -> list[str]:
        return self._emails({"query": "subscribers.status='blocklisted'", "per_page": "all"})

    def unsubscribed_emails(self) -> list[str]:
        return self._emails({"subscription_status": "unsubscribed", "per_page": "all"})
