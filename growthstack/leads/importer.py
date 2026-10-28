"""Lead import from CSV and from public sources. Every lead records where it came
from (source + source_detail), because the first email has to tell the person
(GDPR Art. 14). Addresses on the do-not-contact list are never imported."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from growthstack import dnc
from growthstack.models import Company, Lead, LeadSource

MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Accepted header spellings -> our field name.
HEADER_ALIASES = {
    "email": ["email", "e-mail", "email address", "work email", "mail"],
    "first_name": ["first_name", "first name", "firstname", "given name"],
    "last_name": ["last_name", "last name", "lastname", "surname", "family name"],
    "name": ["name", "full name", "contact name"],
    "title": ["title", "job title", "position", "role", "job_title"],
    "company": ["company", "company name", "organisation", "organization", "account"],
    "website": ["website", "url", "domain", "company website", "web"],
    "country": ["country", "location country"],
}


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    skipped_dnc: int = 0
    skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)
    lead_ids: list[int] = field(default_factory=list)


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    if "://" not in value:
        value = "https://" + value
    host = urlparse(value).hostname or ""
    host = host.removeprefix("www.")
    return host if "." in host else None


def get_or_create_company(db: Session, *, name: str | None, domain: str | None, **extra) -> Company | None:
    if not name and not domain:
        return None
    company = None
    if extra.get("companies_house_number"):
        company = db.scalar(
            select(Company).where(Company.companies_house_number == extra["companies_house_number"])
        )
    if company is None and domain:
        company = db.scalar(select(Company).where(Company.domain == domain))
    if company is None:
        company = Company(name=name or domain, domain=domain, **extra)
        db.add(company)
        db.flush()
    else:
        if domain and not company.domain:
            company.domain = domain
        for k, v in extra.items():
            if v and not getattr(company, k):
                setattr(company, k, v)
    return company


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    mapping = {}
    lowered = {f.strip().lower(): f for f in fieldnames if f}
    for target, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                mapping[target] = lowered[alias]
                break
    return mapping


def upsert_lead(
    db: Session,
    report: ImportReport,
    *,
    email: str | None,
    source: LeadSource,
    source_detail: str,
    imported_by: str | None = None,
    lawful_basis: str = "legitimate_interest",
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    company: Company | None = None,
    website: str | None = None,
    country: str | None = None,
) -> Lead | None:
    if email:
        email = dnc.normalize_email(email)
        if dnc.is_blocked(db, email):
            report.skipped_dnc += 1
            return None
        lead = db.scalar(select(Lead).where(Lead.email == email))
    else:
        lead = None
        if company and first_name and last_name:  # e.g. a director without an email yet
            lead = db.scalar(
                select(Lead).where(
                    Lead.company_id == company.id,
                    Lead.first_name == first_name,
                    Lead.last_name == last_name,
                )
            )
    fields = {
        "first_name": first_name,
        "last_name": last_name,
        "title": title,
        "website": website,
        "country": country,
    }
    if lead is None:
        lead = Lead(
            email=email,
            source=source,
            source_detail=source_detail[:500],
            lawful_basis=lawful_basis,
            imported_by=imported_by,
            company=company,
            **{k: (v.strip()[:200] if isinstance(v, str) else v) for k, v in fields.items()},
        )
        db.add(lead)
        db.flush()
        report.created += 1
    else:
        # Keep the original provenance; only fill in blanks.
        changed = False
        for k, v in fields.items():
            if v and not getattr(lead, k):
                setattr(lead, k, v.strip()[:200])
                changed = True
        if company and not lead.company_id:
            lead.company = company
            changed = True
        if changed:
            report.updated += 1
        else:
            report.skipped_duplicate += 1
    report.lead_ids.append(lead.id)
    return lead


def import_csv(db: Session, data: bytes, *, source_detail: str, imported_by: str) -> ImportReport:
    """Import leads from CSV bytes. `source_detail` must say where the list came from
    (e.g. "Exhibitor list, SaaS Expo 2026 website"); it is shown to recipients."""
    report = ImportReport()
    if not source_detail or len(source_detail.strip()) < 5:
        report.errors.append("Describe where this list came from (it is shown to recipients).")
        return report
    if len(data) > MAX_CSV_BYTES:
        report.errors.append(f"File is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB.")
        return report
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    cols = _map_headers(reader.fieldnames or [])
    if "email" not in cols:
        report.errors.append("No email column found (expected a header like 'email').")
        return report

    seen: set[str] = set()
    for i, row in enumerate(reader, start=2):
        if i - 1 > MAX_ROWS:
            report.errors.append(f"Stopped after {MAX_ROWS} rows.")
            break

        def get(key: str, row: dict = row) -> str | None:
            col = cols.get(key)
            value = (row.get(col) or "").strip() if col else ""
            # Neutralise spreadsheet formula injection if this is ever exported again.
            return value.lstrip("=+-@\t") or None

        email = get("email")
        if not email or not _EMAIL_RE.match(email):
            report.errors.append(f"Row {i}: missing or invalid email")
            continue
        if email.lower() in seen:
            report.skipped_duplicate += 1
            continue
        seen.add(email.lower())
        first, last = get("first_name"), get("last_name")
        if not (first or last) and get("name"):
            parts = get("name").split(None, 1)  # type: ignore[union-attr]
            first, last = parts[0], (parts[1] if len(parts) > 1 else None)
        website = get("website")
        domain = normalize_domain(website) or (
            None if dnc.email_domain(email) in _free_domains() else dnc.email_domain(email)
        )
        company = get_or_create_company(db, name=get("company"), domain=domain)
        upsert_lead(
            db,
            report,
            email=email,
            source=LeadSource.CSV,
            source_detail=source_detail.strip(),
            imported_by=imported_by,
            first_name=first,
            last_name=last,
            title=get("title"),
            company=company,
            website=(website if "://" in website else f"https://{website}") if website else (f"https://{domain}" if domain else None),
            country=get("country"),
        )
    return report


def _free_domains() -> set[str]:
    try:
        from growthstack.verify.email_verifier import free_provider_domains

        return free_provider_domains()
    except ImportError:  # pragma: no cover
        return {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}
