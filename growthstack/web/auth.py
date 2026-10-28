"""Session login for the team, CSRF protection for every form, and a simple login
throttle. Sessions are signed cookies (Starlette SessionMiddleware)."""

from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from growthstack.db import get_db
from growthstack.models import User
from growthstack.security import new_csrf_token, verify_password


class LoginRequired(Exception):
    """Raised by require_user; the app turns it into a redirect to /login."""


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_csrf_token()
        request.session["csrf"] = token
    return token


async def check_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf") or ""
    if not expected:  # no session yet: reject before reading (possibly large) bodies
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and retry.")
    form = await request.form()
    sent = form.get("csrf_token") or request.headers.get("x-csrf-token") or ""
    if not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and retry.")


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    return user if user and user.is_active else None


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.is_active:
        # Same work either way so response time doesn't reveal which emails exist.
        verify_password(password, "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAA")
        return None
    return user if verify_password(password, user.password_hash) else None


class LoginThrottle:
    """At most `limit` failed logins per (ip, email) per `window` seconds. In-memory,
    so it is per web process; good enough for a small internal team behind one process."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _key(self, ip: str, email: str) -> str:
        return f"{ip}|{email.strip().lower()}"

    def blocked(self, ip: str, email: str) -> bool:
        now = time.monotonic()
        with self._lock:
            fails = [t for t in self._fails[self._key(ip, email)] if now - t < self.window]
            self._fails[self._key(ip, email)] = fails
            return len(fails) >= self.limit

    def fail(self, ip: str, email: str) -> None:
        with self._lock:
            self._fails[self._key(ip, email)].append(time.monotonic())

    def reset(self, ip: str, email: str) -> None:
        with self._lock:
            self._fails.pop(self._key(ip, email), None)


throttle = LoginThrottle()
