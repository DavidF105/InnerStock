# Account emails and safety: confirming an email address, "forgot
# password", and limiting password guessing. Routes live under /auth next
# to signup/login (main.py), which call into this module.
#
# One-time links: a random token goes in the email link; only its SHA-256
# hash is stored (EmailToken), so a leaked database can't be used to
# confirm emails or reset passwords. Each token has a purpose ("verify" or
# "reset"), an expiry, and is marked used the moment it's redeemed.
import hashlib
import secrets
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

import email_templates
import emailer
from auth import create_access_token, hash_password
from deps import get_current_user, get_db
from models import EmailToken, User

router = APIRouter(prefix="/auth", tags=["accounts"])

VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(minutes=30)
MAX_LINKS_PER_HOUR = 3            # per user, per purpose - stops inbox flooding
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 72          # bcrypt only reads the first 72 bytes

# Password guessing: after this many wrong passwords for one email within
# the window, logins for that email are refused until the window passes.
# In memory (like the AI rate limit in main.py): resets on restart and
# isn't shared between server instances, which is fine at this size.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60
_login_failures = defaultdict(deque)
_login_lock = threading.Lock()


def _utcnow():
    return datetime.utcnow()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def find_user_by_email(db: Session, email: str):
    # Case-insensitive, so accounts created before emails were lower-cased
    # (e.g. "David@...") still match.
    return db.query(User).filter(func.lower(User.email) == normalize_email(email)).first()


# --- Login guessing limit --------------------------------------------------------------

def _prune(failures, now):
    while failures and now - failures[0] >= LOGIN_WINDOW_SECONDS:
        failures.popleft()


def check_login_allowed(email: str):
    key = normalize_email(email)
    now = monotonic()
    with _login_lock:
        failures = _login_failures[key]
        _prune(failures, now)
        if len(failures) >= LOGIN_MAX_FAILURES:
            wait = int(LOGIN_WINDOW_SECONDS - (now - failures[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong passwords. Try again in about {max(1, wait // 60)} minute(s), or reset your password.",
                headers={"Retry-After": str(wait)},
            )


def record_login_failure(email: str):
    with _login_lock:
        _login_failures[normalize_email(email)].append(monotonic())


def clear_login_failures(email: str):
    with _login_lock:
        _login_failures.pop(normalize_email(email), None)


# --- One-time links --------------------------------------------------------------------

def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _issue_token(db: Session, user: User, purpose: str, ttl: timedelta) -> str:
    raw = secrets.token_urlsafe(32)
    db.add(EmailToken(user_id=user.id, purpose=purpose, token_hash=_hash(raw), expires_at=_utcnow() + ttl))
    db.commit()
    return raw


def _links_sent_recently(db: Session, user: User, purpose: str) -> int:
    return db.query(EmailToken).filter(
        EmailToken.user_id == user.id, EmailToken.purpose == purpose,
        EmailToken.created_at >= _utcnow() - timedelta(hours=1),
    ).count()


def _redeem(db: Session, raw_token: str, purpose: str):
    """The token's user if it's valid, unused and unexpired (and marks it used), else None."""
    token = db.query(EmailToken).filter(EmailToken.token_hash == _hash(raw_token or ""), EmailToken.purpose == purpose).first()
    if not token or token.used_at is not None or token.expires_at < _utcnow():
        return None
    token.used_at = _utcnow()
    return db.get(User, token.user_id)


def _send(to: str, message):
    subject, html, text = message
    emailer.send_email(to, subject, html, text)


def send_verification(db: Session, user: User):
    """Emails a confirmation link. Raises EmailNotConfigured / EmailSendFailed."""
    raw = _issue_token(db, user, "verify", VERIFY_TTL)
    link = f"{emailer.app_url()}/?verify={raw}"
    _send(user.email, email_templates.verify_email(user.name, link, int(VERIFY_TTL.total_seconds() // 3600)))


def send_verification_quietly(db: Session, user: User):
    """Signup's confirmation email: best effort, never fails the signup."""
    if user.is_demo or not emailer.is_configured():
        return
    try:
        send_verification(db, user)
    except emailer.EmailSendFailed:
        pass  # the user can press "Resend" later


def _email_error(e: Exception):
    if isinstance(e, emailer.EmailNotConfigured):
        return HTTPException(status_code=503, detail="Email isn't set up on this server yet.")
    return HTTPException(status_code=502, detail="Couldn't send the email right now. Try again in a few minutes.")


# --- Routes --------------------------------------------------------------------------------

@router.post("/send-verification")
def resend_verification(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.is_demo:
        raise HTTPException(status_code=400, detail="Demo accounts don't have a real email address.")
    if current_user.email_verified:
        return {"sent": False, "already_verified": True}
    if _links_sent_recently(db, current_user, "verify") >= MAX_LINKS_PER_HOUR:
        raise HTTPException(status_code=429, detail="We've sent a few links already - check your inbox (and spam), or try again in an hour.")
    try:
        send_verification(db, current_user)
    except (emailer.EmailNotConfigured, emailer.EmailSendFailed) as e:
        raise _email_error(e)
    return {"sent": True, "to": current_user.email}


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@router.post("/verify-email")
def verify_email(payload: TokenIn, db: Session = Depends(get_db)):
    # No login needed: the link may be opened on another device. Holding a
    # valid link IS the proof.
    user = _redeem(db, payload.token, "verify")
    if not user:
        raise HTTPException(status_code=400, detail="This confirmation link is invalid or has expired. Send yourself a new one from the app.")
    user.email_verified = True
    db.commit()
    return {"verified": True, "email": user.email}


class ForgotPasswordIn(BaseModel):
    email: EmailStr


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordIn, db: Session = Depends(get_db)):
    if not emailer.is_configured():
        raise HTTPException(status_code=503, detail="Password reset by email isn't available yet.")
    user = find_user_by_email(db, payload.email)
    # Same answer whether or not the account exists, so this can't be used
    # to find out who has an InnerStock account.
    if user and not user.is_demo and _links_sent_recently(db, user, "reset") < MAX_LINKS_PER_HOUR:
        raw = _issue_token(db, user, "reset", RESET_TTL)
        link = f"{emailer.app_url()}/?reset={raw}"
        try:
            _send(user.email, email_templates.reset_email(user.name, link, int(RESET_TTL.total_seconds() // 60)))
        except emailer.EmailSendFailed as e:
            raise _email_error(e)
    return {"sent": True}


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


@router.post("/reset-password")
def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)):
    user = _redeem(db, payload.token, "reset")
    if not user:
        raise HTTPException(status_code=400, detail="This reset link is invalid, already used or expired. Ask for a new one.")
    user.hashed_password = hash_password(payload.password)
    user.email_verified = True  # they just proved they read this inbox
    # Any other reset links still out there stop working too.
    db.query(EmailToken).filter(EmailToken.user_id == user.id, EmailToken.purpose == "reset",
                                EmailToken.used_at.is_(None)).update({EmailToken.used_at: _utcnow()})
    db.commit()
    clear_login_failures(user.email)
    return {"access_token": create_access_token(user.id), "token_type": "bearer"}
