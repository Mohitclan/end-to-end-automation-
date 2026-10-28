"""PageSpeed Insights v5 (Lighthouse scores + top opportunities)."""

from __future__ import annotations

from typing import Any

import httpx

from growthstack.config import Settings, get_settings

API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
TIMEOUT = httpx.Timeout(60.0)  # PSI runs a full Lighthouse audit; it is slow
CATEGORIES = ("PERFORMANCE", "SEO", "ACCESSIBILITY", "BEST_PRACTICES")
_CATEGORY_KEYS = {
    "performance": "performance",
    "seo": "seo",
    "accessibility": "accessibility",
    "best_practices": "best-practices",
}


class PageSpeedError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class PageSpeedRateLimitError(PageSpeedError):
    pass


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def parse_result(body: dict) -> dict:
    lh = body.get("lighthouseResult") or {}
    cats = lh.get("categories") or {}
    out: dict[str, Any] = {}
    for name, key in _CATEGORY_KEYS.items():
        score = (cats.get(key) or {}).get("score")
        out[name] = round(float(score) * 100, 1) if score is not None else None

    opportunities = []
    for audit in (lh.get("audits") or {}).values():
        details = audit.get("details") or {}
        if details.get("type") != "opportunity":
            continue
        savings = details.get("overallSavingsMs")
        if savings is None:
            savings = audit.get("numericValue")
        if savings and savings > 0:
            opportunities.append({"title": audit.get("title", ""), "savings_ms": float(savings)})
    opportunities.sort(key=lambda o: o["savings_ms"], reverse=True)
    out["top_opportunities"] = opportunities[:5]
    return out


class PageSpeedClient:
    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=TIMEOUT)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> PageSpeedClient | None:
        """None when no PSI_URLS are configured. The API key is optional (keyless
        calls work but share a small anonymous quota)."""
        s = settings or get_settings()
        if not s.psi_url_list:
            return None
        return cls(s.psi_api_key.get_secret_value() if s.psi_api_key else None)

    def close(self) -> None:
        self._http.close()

    def analyze(self, url: str, strategy: str = "MOBILE") -> dict:
        """Returns performance, seo, accessibility, best_practices (0-100 or None)
        and top_opportunities (up to 5 dicts of title, savings_ms, largest first)."""
        strategy = strategy.upper()
        if strategy not in ("MOBILE", "DESKTOP"):
            raise ValueError("strategy must be MOBILE or DESKTOP")
        params: list[tuple[str, str]] = [("url", url), ("strategy", strategy)]
        params += [("category", c) for c in CATEGORIES]
        if self._api_key:
            params.append(("key", self._api_key))
        try:
            resp = self._http.get(API_URL, params=params)
        except httpx.HTTPError as exc:
            # str(exc) can contain the request URL, which includes the key
            raise PageSpeedError(f"PageSpeed request for {url} failed: {type(exc).__name__}") from None
        if resp.status_code == 429:
            raise PageSpeedRateLimitError("PageSpeed rate limited (429)", 429, _retry_after(resp))
        if not resp.is_success:
            text = resp.text[:300]
            if self._api_key:
                text = text.replace(self._api_key, "***")
            raise PageSpeedError(f"PageSpeed returned {resp.status_code} for {url}: {text}", resp.status_code)
        return parse_result(resp.json())
