"""Stage 3: confirm our own sending domain's SPF and DMARC records with
checkdmarc (Apache-2.0).

checkdmarc's ``check_domains([d])`` also runs DNSSEC, NS/SOA, MTA-STS (an HTTPS
fetch), SMTP TLS reporting and BIMI checks. We only need SPF and DMARC, so the
default checker calls the same ``check_spf`` / ``check_dmarc`` functions that
``check_domains`` uses and returns the same shape::

    {"domain": ..., "spf": {"record", "valid", "warnings", "error"?, ...},
     "dmarc": {"record", "location", "valid", "warnings", "tags"?, "error"?}}

where ``dmarc["tags"]["p"]["value"]`` is the policy (none/quarantine/reject).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from growthstack.models import DomainCheck

log = logging.getLogger(__name__)

WARN_P_NONE = "DMARC policy is p=none: fine to start, move to quarantine once reports look clean"
WARN_NO_DMARC = "No DMARC record: Gmail/Yahoo require one for bulk senders"

Checker = Callable[[str, float], Mapping[str, Any]]


@dataclass
class DomainHealth:
    domain: str
    spf_valid: bool
    spf_record: str | None
    dmarc_valid: bool
    dmarc_record: str | None
    dmarc_policy: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.spf_valid and self.dmarc_valid

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def checkdmarc_checker(domain: str, timeout: float) -> dict[str, Any]:
    """Default checker: checkdmarc's SPF and DMARC checks only."""
    from checkdmarc.dmarc import check_dmarc
    from checkdmarc.spf import check_spf

    return {
        "domain": domain,
        "spf": check_spf(domain, timeout=timeout),
        "dmarc": check_dmarc(domain, timeout=timeout),
    }


def _normalize_domain(domain: str) -> str:
    value = (domain or "").strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    return value.rstrip(".")


def check_domain(domain: str, timeout: float = 5.0, *, checker: Checker | None = None) -> DomainHealth:
    """SPF/DMARC health of ``domain``. Never raises for DNS / checkdmarc failures;
    they come back as invalid with a warning."""
    domain = _normalize_domain(domain)
    if not domain or "." not in domain:
        return DomainHealth(domain, False, None, False, None, None, [f"Not a domain name: {domain!r}"])
    run = checker or checkdmarc_checker
    try:
        raw = run(domain, timeout)
        if isinstance(raw, list):  # check_domains returns a list for several domains
            raw = raw[0] if raw else {}
    except Exception as exc:
        log.warning("domain check failed", extra={"domain": domain, "error": type(exc).__name__})
        return DomainHealth(domain, False, None, False, None, None, [f"Domain check failed: {type(exc).__name__}: {exc}"])

    warnings: list[str] = []
    spf = raw.get("spf") or {}
    dmarc = raw.get("dmarc") or {}

    spf_record = spf.get("record") or None
    spf_valid = bool(spf.get("valid")) and spf_record is not None
    warnings += [f"SPF: {w}" for w in spf.get("warnings") or []]
    if spf.get("error"):
        warnings.append(f"SPF: {spf['error']}")
    elif spf_record is None:
        warnings.append("SPF: no SPF record found")

    dmarc_record = dmarc.get("record") or None
    dmarc_valid = bool(dmarc.get("valid")) and dmarc_record is not None
    dmarc_policy = None
    tags = dmarc.get("tags") or {}
    p_tag = tags.get("p")
    if isinstance(p_tag, Mapping):
        dmarc_policy = str(p_tag.get("value")).lower() if p_tag.get("value") is not None else None
    elif isinstance(p_tag, str):
        dmarc_policy = p_tag.lower()
    warnings += [f"DMARC: {w}" for w in dmarc.get("warnings") or []]
    if dmarc_record is None:
        warnings.append(WARN_NO_DMARC)
    elif dmarc.get("error"):
        warnings.append(f"DMARC: {dmarc['error']}")
    if dmarc_policy == "none":
        warnings.append(WARN_P_NONE)

    return DomainHealth(
        domain=domain,
        spf_valid=spf_valid,
        spf_record=spf_record,
        dmarc_valid=dmarc_valid,
        dmarc_record=dmarc_record,
        dmarc_policy=dmarc_policy,
        warnings=warnings,
    )


def save_domain_check(db: Session, health: DomainHealth) -> DomainCheck:
    """Store a check result. The row is flushed; committing is left to the caller."""
    row = DomainCheck(
        domain=health.domain,
        spf_valid=health.spf_valid,
        spf_record=health.spf_record,
        dmarc_valid=health.dmarc_valid,
        dmarc_record=health.dmarc_record,
        dmarc_policy=health.dmarc_policy,
        warnings=list(health.warnings),
    )
    db.add(row)
    db.flush()
    return row
