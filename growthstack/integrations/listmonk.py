"""listmonk client: keep newsletter blocklist/unsubscribes in sync with our DNC list.

Checked against listmonk v6.2.0 (the latest stable release), from its source and
against a live instance. The API user's role needs ``subscribers:get_all``,
``subscribers:manage`` and ``lists:get_all``. listmonk only lets a role blocklist an
existing subscriber by id if they're on one of the role's lists, or if the role has
``lists:get_all``. Our do-not-contact addresses are usually on no list. The client never
sends the ``query`` parameter: that needs the extra ``subscribers:sql_query``
permission (403 without it). And ``subscription_status`` is ignored unless a
``list_id`` is also given, so filtering on it alone returns every subscriber.
Instead it pages through all subscribers and reads each one's ``status`` and
per-list ``subscription_status``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from growthstack.config import Settings, get_settings

TIMEOUT = httpx.Timeout(20.0)
PAGE_SIZE = 500  # listmonk allows any per_page because "all" is enabled; keep pages modest
_REGEX_SPECIALS = set("\\.^$*+?()[]{}|")


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


def _exact_regex(value: str) -> str:
    """Anchored Postgres regex matching ``value`` literally (for listmonk's ``search``,
    which runs ``name ~* $1 OR email ~* $1``)."""
    return "^" + "".join("\\" + c if c in _REGEX_SPECIALS else c for c in value) + "$"


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

    def _subscribers(self) -> Iterator[dict]:
        """Every subscriber, page by page (oldest first, so new sign-ups during the
        scan land on later pages instead of shifting earlier ones)."""
        page = 1
        while True:
            # No order_by: listmonk then sorts by subscribers.id.
            query = {"page": page, "per_page": PAGE_SIZE, "order": "asc"}
            body = self._check(self._send("GET", "/api/subscribers", params=query), "list subscribers")
            data = body.get("data") or {}
            results = data.get("results") or []
            yield from results
            if len(results) < PAGE_SIZE or page * PAGE_SIZE >= int(data.get("total") or 0):
                return
            page += 1

    @staticmethod
    def _unique_emails(subscribers: Iterator[dict]) -> list[str]:
        seen: dict[str, None] = {}
        for s in subscribers:
            if s.get("email"):
                seen.setdefault(s["email"].strip().lower(), None)
        return list(seen)

    def find_subscriber(self, email: str) -> dict | None:
        email = email.strip().lower()
        params = {"search": _exact_regex(email), "per_page": 10}
        body = self._check(self._send("GET", "/api/subscribers", params=params), "find subscriber")
        for r in (body.get("data") or {}).get("results") or []:
            # The search also matches names, so only an exact email counts.
            if (r.get("email") or "").strip().lower() == email:
                return r
        return None

    def find_subscriber_id(self, email: str) -> int | None:
        sub = self.find_subscriber(email)
        return sub["id"] if sub else None

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
        sub = self.find_subscriber(email)
        if sub is None:
            raise ListmonkError("listmonk reported subscriber exists but it could not be found")
        if sub.get("status") == "blocklisted":
            return
        # Acting on a subscriber by id needs lists:get_all unless they're on one of the
        # role's lists (listmonk's hasSubPerm), so the role must have it.
        self._check(self._send("PUT", f"/api/subscribers/{sub['id']}/blocklist"), "blocklist subscriber")

    def blocklisted_emails(self) -> list[str]:
        """Subscribers blocklisted in listmonk (by an admin, or "unsubscribe from all")."""
        return self._unique_emails(s for s in self._subscribers() if s.get("status") == "blocklisted")

    def unsubscribed_emails(self) -> list[str]:
        """Subscribers who unsubscribed from at least one list (the campaign unsubscribe link)."""
        return self._unique_emails(
            s
            for s in self._subscribers()
            if any(lst.get("subscription_status") == "unsubscribed" for lst in s.get("lists") or [])
        )
