"""Google Search Console search analytics.

Search Console data lags by roughly 2-3 days and we request ``dataState="final"``,
so callers should end their date range at ``date.today() - timedelta(days=3)``."""

from __future__ import annotations

from datetime import date
from typing import Any

from growthstack.config import Settings, get_settings

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
MAX_ROWS = 25000


class SearchConsoleError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class SearchConsoleRateLimitError(SearchConsoleError):
    pass


def build_service(service_account_file: str) -> Any:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _metrics(row: dict) -> dict:
    return {
        "clicks": int(row.get("clicks", 0)),
        "impressions": int(row.get("impressions", 0)),
        "ctr": float(row.get("ctr", 0.0)),
        "position": float(row.get("position", 0.0)),
    }


class SearchConsoleClient:
    def __init__(self, site_url: str, service: Any = None, service_account_file: str | None = None):
        if service is None:
            if not service_account_file:
                raise ValueError("pass either service or service_account_file")
            service = build_service(service_account_file)
        self.site_url = site_url
        self._service = service

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SearchConsoleClient | None:
        s = settings or get_settings()
        if not s.gsc_site_url or not s.gsc_service_account_file:
            return None
        return cls(s.gsc_site_url, service_account_file=s.gsc_service_account_file)

    def _query(self, start: date, end: date, dimensions: list[str], row_limit: int) -> list[dict]:
        if start > end:
            raise ValueError("start must be on or before end")
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dimensions,
            "rowLimit": max(1, min(row_limit, MAX_ROWS)),
            "dataState": "final",
        }
        from googleapiclient.errors import HttpError

        try:
            resp = self._service.searchanalytics().query(siteUrl=self.site_url, body=body).execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            status = int(status) if status is not None else None
            if status == 429:
                retry = None
                try:
                    retry = float(exc.resp.get("retry-after"))
                except (TypeError, ValueError, AttributeError):
                    pass
                raise SearchConsoleRateLimitError("Search Console rate limited (429)", 429, retry) from None
            raise SearchConsoleError(f"Search Console query returned {status}: {exc.reason}", status) from None
        return resp.get("rows") or []

    def daily_totals(self, start: date, end: date) -> list[dict]:
        """One dict per day with data: day (date), clicks, impressions, ctr, position."""
        rows = self._query(start, end, ["date"], (end - start).days + 1)
        out = [{"day": date.fromisoformat(r["keys"][0]), **_metrics(r)} for r in rows]
        return sorted(out, key=lambda r: r["day"])

    def top_queries(self, start: date, end: date, limit: int = 50) -> list[dict]:
        rows = self._query(start, end, ["query"], limit)
        return [{"query": r["keys"][0], **_metrics(r)} for r in rows]
