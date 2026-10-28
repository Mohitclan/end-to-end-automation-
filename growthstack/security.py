"""Password hashing, unsubscribe tokens, CSRF tokens and webhook signature checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from itsdangerous import BadSignature, URLSafeSerializer

from growthstack.config import get_settings

# --- passwords (stdlib scrypt; no extra dependency) ---------------------------
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


# --- unsubscribe tokens -------------------------------------------------------
# No expiry: an unsubscribe link must keep working for as long as the email exists.
def _unsub_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key.get_secret_value(), salt="unsubscribe-v1")


def make_unsubscribe_token(email: str) -> str:
    return _unsub_serializer().dumps({"e": email.strip().lower()})


def read_unsubscribe_token(token: str) -> str | None:
    try:
        data = _unsub_serializer().loads(token)
    except BadSignature:
        return None
    email = data.get("e") if isinstance(data, dict) else None
    return email if isinstance(email, str) and "@" in email else None


def unsubscribe_url(email: str) -> str:
    return f"{get_settings().base_url.rstrip('/')}/u/{make_unsubscribe_token(email)}"


# --- CSRF ---------------------------------------------------------------------
def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


# --- Svix-style webhook signatures (used by Resend) ---------------------------
class WebhookVerificationError(Exception):
    pass


def verify_svix_signature(
    *, secret: str, msg_id: str, timestamp: str, signature_header: str, body: bytes, tolerance: int = 300
) -> None:
    """Raise WebhookVerificationError unless the payload was signed with `secret`.

    Scheme: base64(HMAC-SHA256(key, f"{id}.{timestamp}.{body}")), key = base64-decoded
    secret without its "whsec_" prefix; header holds space-separated "v1,<sig>" entries.
    """
    if not (msg_id and timestamp and signature_header):
        raise WebhookVerificationError("missing signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("bad timestamp") from exc
    if abs(time.time() - ts) > tolerance:
        raise WebhookVerificationError("timestamp outside tolerance")
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in signature_header.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return
    raise WebhookVerificationError("no matching signature")
