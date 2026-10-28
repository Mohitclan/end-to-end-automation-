"""Stage 3 email verification, modelled on AfterShip's email-verifier with its
SMTP check turned OFF.

Checks, in order: syntax, role account, free provider, disposable domain, typo
suggestion, then (unless disposable) an MX lookup. We NEVER open a connection to
a recipient's mail server: SMTP probing ("RCPT TO" checks) gets sending IPs and
domains flagged and hurts our reputation, so the best we can say is "risky".
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol

import dns.exception
import dns.resolver
import httpx

log = logging.getLogger(__name__)

DISPOSABLE_LIST_URL = "https://raw.githubusercontent.com/disposable/disposable-email-domains/master/domains.txt"
MX_LIFETIME = 5.0
SUGGESTION_THRESHOLD = 0.82
# Short domains sit within one edit of a free provider by accident (aon.com vs
# aol.com, mi.com vs me.com), so only suggest typo fixes for longer domains.
SUGGESTION_MIN_LENGTH = 8

Status = Literal["valid", "risky", "invalid"]


class MXResolver(Protocol):
    def resolve(self, qname: str, rdtype: str, *, lifetime: float | None = ...) -> Any: ...


@dataclass
class VerificationResult:
    email: str
    status: Status
    reasons: list[str] = field(default_factory=list)
    syntax_valid: bool = False
    has_mx: bool | None = None
    disposable: bool = False
    role_account: bool = False
    free_provider: bool = False
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ data files


def _read_list(name: str) -> frozenset[str]:
    text = resources.files("growthstack.verify").joinpath("data", name).read_text(encoding="utf-8")
    return frozenset(
        line.strip().lower() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


@cache
def disposable_domains() -> frozenset[str]:
    # A list refreshed by the weekly job (in DATA_DIR) wins over the packaged snapshot.
    refreshed = _default_disposable_path()
    if refreshed.is_file():
        text = refreshed.read_text(encoding="utf-8")
        return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())
    return _read_list("disposable_domains.txt")


@cache
def free_provider_domains() -> frozenset[str]:
    return _read_list("free_providers.txt")


@cache
def role_accounts() -> frozenset[str]:
    return _read_list("role_accounts.txt")


_DOMAIN_LINE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


def _default_disposable_path() -> Path:
    """Where the refresh job writes. Outside the package, which may be read-only."""
    from growthstack.config import get_settings

    return Path(get_settings().data_dir) / "disposable_domains.txt"


def refresh_disposable_list(
    path: str | os.PathLike[str] | None = None,
    *,
    url: str = DISPOSABLE_LIST_URL,
    http: httpx.Client | None = None,
    min_entries: int = 1000,
) -> int:
    """Re-download the disposable-domain list and atomically replace ``path``
    (default: DATA_DIR/disposable_domains.txt). Returns the number of domains written.

    A response with fewer than ``min_entries`` valid domains is rejected so a bad
    download can never empty the list."""
    target = Path(path) if path is not None else _default_disposable_path()
    if http is None:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url)
    else:
        resp = http.get(url, timeout=30.0)
    resp.raise_for_status()
    domains = sorted(
        {
            line.strip().lower()
            for line in resp.text.splitlines()
            if line.strip() and _DOMAIN_LINE.match(line.strip().lower())
        }
    )
    if len(domains) < min_entries:
        raise ValueError(f"disposable list download looks wrong ({len(domains)} domains); keeping the old list")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".disposable-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(domains) + "\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    disposable_domains.cache_clear()
    log.info("disposable domain list refreshed", extra={"count": len(domains)})
    return len(domains)


# --------------------------------------------------------------------- syntax

# dot-atom local part (RFC 5322 without quoted strings / comments)
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


def _split(email: str) -> tuple[str, str] | None:
    """Return (local, ascii lowercase domain) or None if the syntax is invalid."""
    if not email or len(email) > 254 or email.count("@") != 1:
        return None
    local, domain = email.split("@")
    if not local or len(local) > 64 or not _LOCAL_RE.match(local):
        return None
    domain = domain.lower()
    if domain.endswith("."):  # fully qualified form "example.com."
        domain = domain[:-1]
    if not domain:
        return None
    if not domain.isascii():
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    if len(domain) > 253:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        return None
    if not _TLD_RE.match(labels[-1]):
        return None
    if len(local) + 1 + len(domain) > 254:
        return None
    return local, domain


# ---------------------------------------------------------------- suggestions


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance that also counts an adjacent transposition as one
    edit (optimal string alignment), so ``gmial`` is one edit from ``gmail``."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - _edit_distance(a, b) / longest


def suggest_domain(domain: str) -> str | None:
    """The free-provider domain this one is probably a typo of, if any."""
    if domain in free_provider_domains() or len(domain) < SUGGESTION_MIN_LENGTH:
        return None
    best, best_score = None, 0.0
    for candidate in sorted(free_provider_domains()):
        score = similarity(domain, candidate)
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= SUGGESTION_THRESHOLD else None


def _is_disposable(domain: str) -> bool:
    listed = disposable_domains()
    labels = domain.split(".")
    # the domain itself and every parent with at least two labels
    return any(".".join(labels[i:]) in listed for i in range(len(labels) - 1))


# ------------------------------------------------------------------------- MX


def _lookup_mx(domain: str, resolver: MXResolver | None) -> bool | None:
    """True / False when DNS gives a definite answer, None when it could not be
    determined. MX only - like AfterShip, no fallback to A records."""
    resolver = resolver or dns.resolver.Resolver()
    try:
        answer = resolver.resolve(domain, "MX", lifetime=MX_LIFETIME)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.YXDOMAIN):
        return False
    except (dns.exception.Timeout, dns.resolver.NoNameservers, dns.exception.DNSException, OSError) as exc:
        log.info("mx lookup failed", extra={"domain": domain, "error": type(exc).__name__})
        return None
    records = list(answer)
    if not records:
        return False
    # RFC 7505 "null MX": a single record "0 ." means the domain accepts no mail
    if len(records) == 1:
        exchange = str(getattr(records[0], "exchange", "")).strip()
        if exchange in (".", ""):
            return False
    return True


# ---------------------------------------------------------------------- public


def verify_email(email: str, *, check_mx: bool = True, resolver: MXResolver | None = None) -> VerificationResult:
    """Check an address without contacting its mail server.

    * invalid: bad syntax, disposable domain, or the domain has no MX records
    * risky: role account (info@, sales@...), a likely typo of a free provider,
      or the MX lookup could not complete
    * valid: none of the above (free webmail addresses are flagged but valid)

    ``check_mx=False`` skips DNS (has_mx stays None and does not count against
    the address). ``resolver`` is any object with ``resolve(name, rdtype, lifetime=)``.
    """
    original = (email or "").strip() if isinstance(email, str) else ""
    parts = _split(original)
    if parts is None:
        return VerificationResult(email=original, status="invalid", reasons=["invalid_syntax"])
    local, domain = parts
    normalized = f"{local}@{domain}"
    result = VerificationResult(email=normalized, status="valid", syntax_valid=True)

    if local.lower().split("+", 1)[0] in role_accounts():
        result.role_account = True
        result.reasons.append("role_account")
    if domain in free_provider_domains():
        result.free_provider = True
        result.reasons.append("free_provider")
    if _is_disposable(domain):
        result.disposable = True
        result.reasons.append("disposable")
    suggested = suggest_domain(domain)
    if suggested:
        result.suggestion = f"{local}@{suggested}"
        result.reasons.append("possible_typo")

    if result.disposable:
        result.status = "invalid"
        return result

    if check_mx:
        result.has_mx = _lookup_mx(domain, resolver)
        if result.has_mx is False:
            result.reasons.append("no_mx")
        elif result.has_mx is None:
            result.reasons.append("mx_lookup_failed")

    if result.has_mx is False:
        result.status = "invalid"
    elif result.role_account or result.suggestion or (check_mx and result.has_mx is None):
        result.status = "risky"
    return result
