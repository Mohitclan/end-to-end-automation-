"""Reply detection. A real reply stops the lead's sequence at once; a reply asking
us to stop also adds the address to the do-not-contact list. Out-of-office and
other auto-replies are recorded but do not stop anything.

Inbound mail arrives either by IMAP polling of each mailbox or through Resend's
`email.received` webhook (see growthstack.web.webhooks)."""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import os
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from growthstack import dnc
from growthstack.models import (
    DncReason,
    Enrollment,
    EnrollmentStatus,
    InboundReply,
    Lead,
    LeadStage,
    Mailbox,
    OutboundMessage,
)
from growthstack.outreach import sequencer

log = logging.getLogger(__name__)

_AUTO_SUBJECT = re.compile(
    r"^(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|abwesenheit|absence|on vacation|away from)",
    re.I,
)
_OPT_OUT = re.compile(
    r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting|sending)|do not (email|contact)|"
    r"don't (email|contact)|not interested,? (please )?(stop|remove)|opt[- ]?out)\b",
    re.I,
)


@dataclass
class Inbound:
    from_email: str
    subject: str = ""
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    body_text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    channel: str = "imap"


def is_auto_reply(msg: Inbound) -> bool:
    h = {k.lower(): v for k, v in msg.headers.items()}
    if h.get("auto-submitted", "no").lower() not in ("", "no"):
        return True
    if "x-autoreply" in h or "x-autorespond" in h:
        return True
    if h.get("precedence", "").lower() in ("auto_reply", "bulk", "junk"):
        return True
    return bool(_AUTO_SUBJECT.search(msg.subject.strip()))


def is_opt_out(msg: Inbound) -> bool:
    # Only look at the new text, not the quoted original (which contains our footer).
    fresh = []
    for line in msg.body_text.splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:$", line.strip()):
            break
        fresh.append(line)
    return bool(_OPT_OUT.search(msg.subject) or _OPT_OUT.search("\n".join(fresh)))


def _match_enrollment(db: Session, msg: Inbound) -> tuple[Lead | None, Enrollment | None, bool]:
    """Returns (lead, enrollment, threaded). `threaded` means the reply references one
    of our Message-IDs, which a forger of the From address can't know."""
    ids = [i for i in [msg.in_reply_to, *msg.references] if i]
    if ids:
        out = db.scalars(select(OutboundMessage).where(OutboundMessage.message_id_header.in_(ids))).first()
        if out:
            return out.enrollment.lead, out.enrollment, True
    sender_email = dnc.normalize_email(msg.from_email)
    lead = db.scalar(select(Lead).where(Lead.email == sender_email))
    if lead is None:
        return None, None, False
    enrollment = db.scalars(
        select(Enrollment).where(Enrollment.lead_id == lead.id).order_by(Enrollment.id.desc())
    ).first()
    return lead, enrollment, False


def process_inbound(db: Session, msg: Inbound) -> InboundReply | None:
    """Record an inbound email and stop outreach if it is a reply from a lead.
    Returns None for mail unrelated to any lead (or already processed)."""
    if msg.message_id and db.scalar(select(InboundReply.id).where(InboundReply.message_id == msg.message_id)):
        return None
    lead, enrollment, threaded = _match_enrollment(db, msg)
    if lead is None:
        return None
    auto = is_auto_reply(msg)
    opt_out = not auto and is_opt_out(msg)
    reply = InboundReply(
        lead_id=lead.id,
        enrollment_id=enrollment.id if enrollment else None,
        from_email=dnc.normalize_email(msg.from_email),
        subject=msg.subject[:500],
        message_id=msg.message_id,
        is_auto_reply=auto,
        is_opt_out=opt_out,
        snippet=msg.body_text[:1000],
        channel=msg.channel,
    )
    db.add(reply)
    if auto:
        return reply
    if opt_out and lead.email:
        reason = DncReason.REPLY_OPT_OUT if threaded else DncReason.REPLY_OPT_OUT_UNVERIFIED
        dnc.add(db, email=lead.email, reason=reason, channel="email", note="asked in a reply")
    else:
        sequencer.stop_for_lead(db, lead, reason=EnrollmentStatus.STOPPED_REPLY, note="lead replied")
        if lead.stage not in (LeadStage.CONVERTED, LeadStage.DO_NOT_CONTACT):
            lead.stage = LeadStage.REPLIED
    db.flush()
    return reply


# --- IMAP --------------------------------------------------------------------
def parse_rfc822(raw: bytes, channel: str = "imap") -> Inbound:
    parsed: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
    body = parsed.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, ValueError):
            text = ""
    refs = str(parsed.get("References", "") or "").split()
    return Inbound(
        from_email=parseaddr(str(parsed.get("From", "")))[1],
        subject=str(parsed.get("Subject", "") or ""),
        message_id=(str(parsed.get("Message-ID")).strip() if parsed.get("Message-ID") else None),
        in_reply_to=(str(parsed.get("In-Reply-To")).strip() if parsed.get("In-Reply-To") else None),
        references=refs,
        body_text=text,
        headers={k: str(v) for k, v in parsed.items()},
        channel=channel,
    )


def poll_mailbox(db: Session, mailbox: Mailbox, *, imap_factory=imaplib.IMAP4_SSL, batch: int = 200) -> int:
    """Fetch new messages (by UID) from the mailbox's INBOX. Returns replies recorded.
    Uses BODY.PEEK so messages are not marked as read."""
    if not (mailbox.imap_host and mailbox.imap_user and mailbox.imap_password_env):
        return 0
    password = os.environ.get(mailbox.imap_password_env)
    if not password:
        raise RuntimeError(f"env var {mailbox.imap_password_env} (IMAP password) is not set")
    conn = imap_factory(mailbox.imap_host)
    recorded = 0
    try:
        conn.login(mailbox.imap_user, password)
        conn.select("INBOX", readonly=True)
        last = mailbox.imap_last_uid or 0
        typ, data = conn.uid("search", None, f"UID {last + 1}:*")
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last)[:batch]
        for uid in uids:
            typ, fetched = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            if process_inbound(db, parse_rfc822(fetched[0][1])):
                recorded += 1
            mailbox.imap_last_uid = uid
            db.flush()
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - best effort
            pass
    return recorded
