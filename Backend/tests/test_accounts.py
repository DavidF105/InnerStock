# Signup/login hardening, email confirmation and forgot/reset password
# (accounts.py). Emails go to the fake `outbox` fixture.
import re
from datetime import datetime, timedelta

import pytest

import accounts
from conftest import signup
from models import EmailToken, User


def link_token(email, param):
    """The ?verify= / ?reset= token from an email's text."""
    return re.search(rf"\?{param}=([A-Za-z0-9_\-]+)", email["text"]).group(1)


def me(client, headers):
    return client.get("/auth/me", headers=headers).json()


# --- Signup and login -----------------------------------------------------------------

def test_signup_requires_a_real_password(client):
    response = client.post("/auth/signup", json={"name": "A", "email": "a@example.com", "password": "short"})
    assert response.status_code == 422


def test_emails_are_case_insensitive(client):
    signup(client, "David@Example.com", password="password123")
    duplicate = client.post("/auth/signup", json={"name": "D", "email": "david@example.com", "password": "password123"})
    assert duplicate.status_code == 400
    assert client.post("/auth/login", json={"email": "DAVID@example.com", "password": "password123"}).status_code == 200


def test_signup_sends_a_confirmation_email(client, outbox):
    headers = signup(client, "new@example.com")
    assert outbox[-1]["to"] == "new@example.com"
    assert "?verify=" in outbox[-1]["text"]
    assert me(client, headers)["email_verified"] is False


def test_signup_still_works_when_email_is_not_set_up(client, outbox, monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS")
    signup(client, "new@example.com")
    assert outbox == []


def test_login_locks_after_too_many_wrong_passwords(client):
    signup(client, "a@example.com", password="password123")
    for _ in range(accounts.LOGIN_MAX_FAILURES):
        assert client.post("/auth/login", json={"email": "a@example.com", "password": "wrong-guess"}).status_code == 401
    locked = client.post("/auth/login", json={"email": "a@example.com", "password": "password123"})
    assert locked.status_code == 429  # even the right password waits out the lock


def test_successful_login_clears_earlier_failures(client):
    signup(client, "a@example.com", password="password123")
    for _ in range(accounts.LOGIN_MAX_FAILURES - 1):
        client.post("/auth/login", json={"email": "a@example.com", "password": "nope-nope"})
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "password123"}).status_code == 200
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "nope-nope"}).status_code == 401


# --- Confirming an email ------------------------------------------------------------------

def test_confirmation_link_verifies_the_email(client, outbox):
    headers = signup(client, "new@example.com")
    token = link_token(outbox[-1], "verify")
    assert client.post("/auth/verify-email", json={"token": token}).json()["verified"] is True
    assert me(client, headers)["email_verified"] is True
    # One use only.
    assert client.post("/auth/verify-email", json={"token": token}).status_code == 400


def test_bad_or_expired_confirmation_links_fail(client, outbox, db_session_factory):
    signup(client, "new@example.com")
    assert client.post("/auth/verify-email", json={"token": "x" * 40}).status_code == 400
    db = db_session_factory()
    db.query(EmailToken).update({EmailToken.expires_at: datetime.utcnow() - timedelta(minutes=1)})
    db.commit()
    db.close()
    assert client.post("/auth/verify-email", json={"token": link_token(outbox[-1], "verify")}).status_code == 400


def test_resending_confirmation_is_rate_limited(client, outbox):
    headers = signup(client, "new@example.com")  # sends link #1
    for _ in range(accounts.MAX_LINKS_PER_HOUR - 1):
        assert client.post("/auth/send-verification", headers=headers).status_code == 200
    assert client.post("/auth/send-verification", headers=headers).status_code == 429


def test_only_hashes_of_tokens_are_stored(client, outbox, db_session_factory):
    signup(client, "new@example.com")
    raw = link_token(outbox[-1], "verify")
    db = db_session_factory()
    stored = db.query(EmailToken).one().token_hash
    db.close()
    assert raw not in stored and len(stored) == 64


# --- Forgot / reset password --------------------------------------------------------------

def test_password_reset_flow(client, outbox):
    signup(client, "a@example.com", password="old-password")
    assert client.post("/auth/forgot-password", json={"email": "A@example.com"}).json() == {"sent": True}
    token = link_token(outbox[-1], "reset")
    assert outbox[-1]["subject"] == "Reset your InnerStock password"

    response = client.post("/auth/reset-password", json={"token": token, "password": "brand-new-pass"})
    assert response.status_code == 200
    new_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert me(client, new_headers)["email_verified"] is True  # they proved they read the inbox

    assert client.post("/auth/login", json={"email": "a@example.com", "password": "old-password"}).status_code == 401
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "brand-new-pass"}).status_code == 200
    # Single use.
    assert client.post("/auth/reset-password", json={"token": token, "password": "another-pass"}).status_code == 400


def test_forgot_password_does_not_reveal_who_has_an_account(client, outbox):
    before = len(outbox)
    assert client.post("/auth/forgot-password", json={"email": "nobody@example.com"}).json() == {"sent": True}
    assert len(outbox) == before


def test_new_reset_password_must_be_long_enough(client, outbox):
    signup(client, "a@example.com")
    client.post("/auth/forgot-password", json={"email": "a@example.com"})
    assert client.post("/auth/reset-password", json={"token": link_token(outbox[-1], "reset"), "password": "short"}).status_code == 422


def test_resetting_kills_other_outstanding_reset_links(client, outbox):
    signup(client, "a@example.com")
    client.post("/auth/forgot-password", json={"email": "a@example.com"})
    first = link_token(outbox[-1], "reset")
    client.post("/auth/forgot-password", json={"email": "a@example.com"})
    second = link_token(outbox[-1], "reset")
    assert client.post("/auth/reset-password", json={"token": second, "password": "brand-new-pass"}).status_code == 200
    assert client.post("/auth/reset-password", json={"token": first, "password": "sneaky-pass"}).status_code == 400


def test_forgot_password_when_email_not_set_up(client, monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS")
    assert client.post("/auth/forgot-password", json={"email": "a@example.com"}).status_code == 503


# --- "Email me this" ------------------------------------------------------------------------

@pytest.fixture
def recap_for(client, market_events):
    """Gives `headers`' user an Apple recap notification and returns its id."""
    from conftest import log_trade
    from test_earnings_recaps import reported

    def make(headers):
        reported(market_events, "AAPL", "Apple Inc.")
        log_trade(client, headers, "AAPL", "buy", 5, 90)
        return client.get("/notifications", headers=headers).json()["notifications"][0]["id"]
    return make


def verify(client, outbox, address="alice@example.com"):
    """Clicks the confirmation link sent to `address`."""
    email = next(e for e in reversed(outbox) if e["to"] == address and "?verify=" in e["text"])
    client.post("/auth/verify-email", json={"token": link_token(email, "verify")})


def test_email_me_this_sends_the_recap(client, alice, outbox, recap_for):
    verify(client, outbox)
    note_id = recap_for(alice)
    response = client.post(f"/notifications/{note_id}/email", headers=alice)
    assert response.json() == {"sent": True, "to": "alice@example.com"}
    email = outbox[-1]
    assert email["to"] == "alice@example.com"
    assert email["subject"] == "AAPL earnings: Results beat estimates"
    assert "What it means for Apple Inc." in email["html"] and "Demand looks healthy." in email["text"]
    assert client.get("/notifications", headers=alice).json()["notifications"][0]["emailed_at"]


def test_email_me_this_needs_a_confirmed_email(client, alice, recap_for):
    note_id = recap_for(alice)
    response = client.post(f"/notifications/{note_id}/email", headers=alice)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "email_not_verified"


def test_email_me_this_is_rate_limited_and_scoped(client, alice, bob, outbox, recap_for):
    verify(client, outbox)
    note_id = recap_for(alice)
    assert client.post(f"/notifications/{note_id}/email", headers=alice).status_code == 200
    assert client.post(f"/notifications/{note_id}/email", headers=alice).status_code == 429
    assert client.post(f"/notifications/{note_id}/email", headers=bob).status_code == 404


def test_recap_email_escapes_outside_text(client, alice, outbox, recap_for, recap_writer):
    recap_writer.result = {"headline": "<script>alert(1)</script>", "what_happened": "a & b",
                           "what_it_means": "c", "sentiment": "neutral"}
    verify(client, outbox)
    note_id = recap_for(alice)
    client.post(f"/notifications/{note_id}/email", headers=alice)
    assert "<script>" not in outbox[-1]["html"] and "&lt;script&gt;" in outbox[-1]["html"]
