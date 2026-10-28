"""Endpoints reachable without login: one-click unsubscribe, the Resend webhook
and the health check. Each is authenticated by its own token or signature."""

from __future__ import annotations

import json
import logging
from email.utils import parseaddr

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from growthstack import dnc
from growthstack.config import get_settings
from growthstack.db import get_db
from growthstack.models import DncReason, EmailEvent, OutboundMessage
from growthstack.outreach import replies
from growthstack.security import WebhookVerificationError, read_unsubscribe_token, verify_svix_signature
from growthstack.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/healthz", response_class=PlainTextResponse)
def healthz(db: Session = Depends(get_db)) -> str:
    db.execute(text("SELECT 1"))
    return "ok"


# --- unsubscribe ---------------------------------------------------------------------
# GET shows a confirmation page (link scanners prefetch GETs, so GET must not
# unsubscribe). POST unsubscribes immediately: that is what mail clients send for
# RFC 8058 one-click ("List-Unsubscribe=One-Click") and what the page's button does.
@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, request: Request):
    email = read_unsubscribe_token(token)
    if not email:
        return templates.TemplateResponse(request, "unsubscribe.html", {"invalid": True}, status_code=404)
    return templates.TemplateResponse(
        request, "unsubscribe.html", {"email": email, "token": token, "company": get_settings().company_name}
    )


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe(token: str, request: Request, db: Session = Depends(get_db)):
    email = read_unsubscribe_token(token)
    if not email:
        return templates.TemplateResponse(request, "unsubscribe.html", {"invalid": True}, status_code=404)
    dnc.add(db, email=email, reason=DncReason.UNSUBSCRIBE, channel="email", note="unsubscribe link")
    db.commit()
    return templates.TemplateResponse(
        request, "unsubscribe.html", {"done": True, "email": email, "company": get_settings().company_name}
    )


# --- Resend webhook ------------------------------------------------------------------
@router.post("/webhooks/resend")
async def resend_webhook(request: Request, db: Session = Depends(get_db)):
    secret = get_settings().resend_webhook_secret
    if not secret:
        raise HTTPException(503, "webhook secret not configured")
    body = await request.body()
    try:
        verify_svix_signature(
            secret=secret.get_secret_value(),
            msg_id=request.headers.get("svix-id", ""),
            timestamp=request.headers.get("svix-timestamp", ""),
            signature_header=request.headers.get("svix-signature", ""),
            body=body,
        )
    except WebhookVerificationError as exc:
        log.warning("rejected Resend webhook: %s", exc)
        raise HTTPException(401, "invalid signature") from exc
    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "invalid JSON") from exc
    result = handle_resend_event(db, request.headers["svix-id"], event)
    return JSONResponse({"ok": True, "result": result})


def handle_resend_event(db: Session, webhook_id: str, event: dict) -> str:
    """Record a delivery event once (svix-id makes retries idempotent) and act on it."""
    if db.scalar(select(EmailEvent.id).where(EmailEvent.webhook_id == webhook_id)):
        return "duplicate"
    etype = event.get("type", "")
    data = event.get("data") or {}
    to = data.get("to") or []
    recipient = (to[0] if isinstance(to, list) and to else to) or None
    try:
        with db.begin_nested():
            db.add(
                EmailEvent(
                    webhook_id=webhook_id,
                    event_type=etype,
                    provider_message_id=data.get("email_id"),
                    recipient=recipient,
                    payload=event,
                )
            )
    except IntegrityError:
        return "duplicate"

    result = "recorded"
    if etype == "email.bounced":
        bounce = data.get("bounce") or {}
        # Only permanent (hard) bounces go on the list; transient ones may recover.
        if str(bounce.get("type", "")).lower() in ("permanent", "hard") and recipient:
            dnc.add(db, email=recipient, reason=DncReason.HARD_BOUNCE, note=bounce.get("message"))
            result = "hard bounce: added to do-not-contact"
    elif etype == "email.complained" and recipient:
        dnc.add(db, email=recipient, reason=DncReason.COMPLAINT, note="spam complaint")
        result = "complaint: added to do-not-contact"
    elif etype == "email.received":
        sender_addr = data.get("from") or ""
        if isinstance(sender_addr, dict):
            sender_addr = sender_addr.get("email", "")
        inbound = replies.Inbound(
            from_email=parseaddr(sender_addr)[1] or sender_addr,
            subject=data.get("subject") or "",
            message_id=data.get("message_id"),
            body_text="",  # the webhook carries no body; opt-out phrases are caught by IMAP polling
            channel="resend",
        )
        reply = replies.process_inbound(db, inbound)
        result = "reply recorded" if reply else "inbound ignored (not from a lead)"
    elif etype == "email.delivered" and data.get("email_id"):
        msg = db.scalar(select(OutboundMessage).where(OutboundMessage.provider_message_id == data["email_id"]))
        result = "delivered" if msg else "delivered (unknown message)"
    db.commit()
    return result
