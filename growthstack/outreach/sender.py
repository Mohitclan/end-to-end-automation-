"""Builds outreach emails (with the compliance footer and one-click unsubscribe
headers) and sends them through Resend.

Every email gets:
  * List-Unsubscribe + List-Unsubscribe-Post (RFC 8058 one-click, required by Gmail/Yahoo)
  * a visible unsubscribe link and our postal address in the body
  * on the first email to a lead: where we got their details (GDPR Art. 14)
Emails are plain text: it is what a person writing one-to-one would send, and it
avoids tracking pixels. (react-email templates are for the HTML newsletters, which
listmonk sends.)
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass

from growthstack.config import Settings, get_settings
from growthstack.dnc import email_domain
from growthstack.models import SOURCE_LABELS, Lead, LeadSource, Mailbox, OutboundMessage
from growthstack.security import unsubscribe_url

log = logging.getLogger(__name__)


class SendError(Exception):
    """Sending failed and may succeed on retry."""


class PermanentSendError(SendError):
    """Sending failed and retrying will not help (e.g. Resend rejected the payload)."""


@dataclass
class BuiltEmail:
    params: dict
    message_id_header: str


def source_disclosure(lead: Lead) -> str:
    try:
        label = SOURCE_LABELS[LeadSource(lead.source)]
    except ValueError:
        label = "a public business listing"
    return f"I found your business contact details via {label} ({lead.source_detail})."


def footer(lead: Lead, *, first_email: bool, settings: Settings) -> str:
    lines = ["", "--"]
    if first_email:
        lines.append(source_disclosure(lead))
    who = settings.company_name
    if settings.company_postal_address:
        who += f", {settings.company_postal_address}"
    lines.append(who)
    lines.append(f"Not interested? Unsubscribe in one click: {unsubscribe_url(lead.email or '')}")
    return "\n".join(lines)


def build_email(
    msg: OutboundMessage,
    lead: Lead,
    mailbox: Mailbox,
    *,
    thread_root_message_id: str | None = None,
    settings: Settings | None = None,
) -> BuiltEmail:
    settings = settings or get_settings()
    if not lead.email:
        raise PermanentSendError("lead has no email address")
    domain = settings.sending_domain or email_domain(mailbox.from_email)
    message_id = f"<gs.{msg.id}.{secrets.token_hex(6)}@{domain}>"
    unsub = unsubscribe_url(lead.email)
    list_unsub = f"<{unsub}>"
    if settings.unsubscribe_mailto:
        list_unsub += f", <mailto:{settings.unsubscribe_mailto}?subject=unsubscribe>"
    headers = {
        "Message-ID": message_id,
        "List-Unsubscribe": list_unsub,
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    if thread_root_message_id:  # follow-ups thread under the first email
        headers["In-Reply-To"] = thread_root_message_id
        headers["References"] = thread_root_message_id
    params = {
        "from": f"{_header_safe(mailbox.from_name, display_name=True)} <{mailbox.from_email}>",
        "to": [lead.email],
        "subject": _header_safe(msg.subject),
        "text": msg.body_text.rstrip() + "\n" + footer(lead, first_email=msg.step_position == 1, settings=settings),
        "headers": headers,
        "tags": [{"name": "kind", "value": "outreach"}, {"name": "step", "value": str(msg.step_position)}],
    }
    if mailbox.reply_to:
        params["reply_to"] = mailbox.reply_to
    return BuiltEmail(params=params, message_id_header=message_id)


def _header_safe(value: str, *, display_name: bool = False) -> str:
    """Collapse line breaks (header injection) and, for display names, drop the
    characters that would change the address."""
    value = " ".join(value.split())
    if display_name:
        value = "".join(ch for ch in value if ch not in '<>",;')
    return value[:300]


def send(built: BuiltEmail, *, idempotency_key: str, settings: Settings | None = None) -> str:
    """Send through Resend and return the provider's email id."""
    settings = settings or get_settings()
    if settings.email_dry_run:
        log.info("dry-run: email not sent", extra={"subject": built.params["subject"]})
        return f"dryrun-{uuid.uuid4()}"
    if not settings.resend_api_key:
        raise PermanentSendError("RESEND_API_KEY is not set (or enable EMAIL_DRY_RUN)")

    import resend
    from resend.exceptions import ResendError

    resend.api_key = settings.resend_api_key.get_secret_value()
    try:
        resp = resend.Emails.send(built.params, {"idempotency_key": idempotency_key})
    except ResendError as exc:
        try:
            code = int(getattr(exc, "code", 0))
        except (TypeError, ValueError):
            code = 0
        # 4xx other than rate limits won't get better on retry.
        if 400 <= code < 500 and code != 429:
            raise PermanentSendError(f"Resend rejected the email ({code}): {exc}") from exc
        raise SendError(f"Resend error: {exc}") from exc
    except Exception as exc:  # network errors etc.
        raise SendError(f"Resend request failed: {exc}") from exc
    return resp["id"]
