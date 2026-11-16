# Shared test setup. Every test gets:
#   - a brand-new, empty in-memory SQLite database (nothing touches your
#     real innerstock.db, and tests can't leak data into each other)
#   - fake stock prices instead of real yfinance calls (fast, deterministic,
#     works offline and in CI)
#   - fake API keys, so a test can never accidentally spend real Claude money
import os

# Must be set BEFORE main/config are imported - config.py reads these at
# import time. load_dotenv() never overrides variables that already exist,
# so these win over anything in a developer's local .env.
os.environ["JWT_SECRET_KEY"] = "test-secret-key"
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
# Blank (not unset) so a developer's real SnapTrade keys in .env can never
# be loaded - load_dotenv() skips variables that already exist. Brokerage
# tests opt in with fake values via the fake_snaptrade fixture.
for _name in ("SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY", "SNAPTRADE_OWNER_EMAIL",
              "SNAPTRADE_KEY_TYPE", "FIELD_ENCRYPTION_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
    os.environ[_name] = ""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import accounts
import demo
import earnings_recaps
import emailer
import grading
import holdings_news
import main
from database import Base


@pytest.fixture
def db_session_factory():
    # StaticPool + check_same_thread=False keeps ONE shared connection, so
    # the in-memory database survives across the threads TestClient uses.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def prices(monkeypatch):
    """Fake price table. Tests fill it in: prices["AAPL"] = (current, previous_close).

    Replaces get_or_fetch_price, the one helper every route uses for prices,
    so /holdings and /watchlist never reach yfinance. A ticker that isn't in
    the table behaves like a failed yfinance lookup (returns None).
    """
    table = {}

    def fake_get_or_fetch_price(ticker, db):
        ticker = ticker.upper()
        if ticker not in table:
            return None
        current, previous_close = table[ticker]
        return SimpleNamespace(ticker=ticker, current_price=current, previous_close=previous_close)

    monkeypatch.setattr(main, "get_or_fetch_price", fake_get_or_fetch_price)
    return table


@pytest.fixture
def history(monkeypatch):
    """Fake daily closes for the grading scorecard: history["AAPL"] = [(date, close), ...].

    Replaces grading.fetch_daily_closes (its only yfinance call). A ticker
    that isn't in the table behaves like a failed download (no data), so
    tests that don't care about prices still grade "from memory".
    """
    table = {}

    def fake_fetch_daily_closes(ticker, start, end):
        return [(d, c) for d, c in sorted(table.get(ticker.upper(), [])) if start <= d <= end]

    monkeypatch.setattr(grading, "fetch_daily_closes", fake_fetch_daily_closes)
    return table


@pytest.fixture
def market_events(monkeypatch):
    """Fake yfinance data for /holdings/news: profiles and news per ticker.

    market_events.profiles["AAPL"] = {"name": "Apple Inc.", "quote_type": "EQUITY",
                                      "earnings_start": <unix ts>, ...}
    market_events.news["AAPL"] = [<yfinance-shaped news item>, ...]
    market_events.calendar["GOOG"] = [{"ts": <unix ts>, "eps_estimate": 2.98,
                                       "eps_actual": 2.67 or None, "surprise_pct": ...}, ...]

    Replaces holdings_news.load_profile / load_news / load_calendar_earnings
    (its only network calls). A ticker missing from a table behaves like a failed lookup.
    The module's in-memory cache is cleared so tests can't leak into each
    other.
    """
    tables = SimpleNamespace(profiles={}, news={}, calendar={})
    monkeypatch.setattr(holdings_news, "load_profile", lambda ticker: tables.profiles.get(ticker))
    monkeypatch.setattr(holdings_news, "load_news", lambda ticker: tables.news.get(ticker))
    monkeypatch.setattr(holdings_news, "load_calendar_earnings", lambda ticker: tables.calendar.get(ticker))
    holdings_news.clear_cache()
    yield tables
    holdings_news.clear_cache()


@pytest.fixture
def recap_writer(monkeypatch):
    """Fake Claude for earnings recaps: records each call's facts and returns a canned recap.

    Set recap_writer.result = None to simulate Claude failing. Also lifts
    the AI rate limit so tests aren't affected by earlier tests' calls.
    """
    state = SimpleNamespace(calls=[], result={
        "headline": "Results beat estimates",
        "what_happened": "EPS came in above the estimate and the stock rose.",
        "what_it_means": "Demand looks healthy.",
        "sentiment": "positive",
    })

    def fake_write_recap(facts):
        state.calls.append(facts)
        return dict(state.result) if state.result else None

    monkeypatch.setattr(earnings_recaps, "write_recap", fake_write_recap)
    monkeypatch.setattr(earnings_recaps, "_enforce_rate_limit", lambda user_id: None)
    return state


@pytest.fixture
def outbox(monkeypatch):
    """Fake email: nothing is really sent; each email lands in this list as
    {"to", "subject", "html", "text"}. Email counts as configured - a test
    can monkeypatch.delenv("GMAIL_ADDRESS") to simulate it not being set up."""
    sent = []
    monkeypatch.setenv("GMAIL_ADDRESS", "innerstock.test@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "fake fake fake fake")
    monkeypatch.setenv("APP_URL", "https://innerstock.test")
    monkeypatch.setattr(emailer, "send_email",
                        lambda to, subject, html, text: sent.append({"to": to, "subject": subject, "html": html, "text": text}))
    return sent


@pytest.fixture
def client(db_session_factory, prices, history, market_events, recap_writer, outbox, monkeypatch):
    def override_get_db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    # /thesis-reviews runs a FOMO check that downloads price history from
    # yfinance - stub it out so tests stay offline.
    monkeypatch.setattr(
        main, "check_fomo_for_trade", lambda trade: {"fomo_flag": False, "reason": "stubbed in tests"}
    )

    # In-memory limits are global; start every test with a clean slate.
    accounts._login_failures.clear()
    demo._demo_starts.clear()
    main._ai_call_log.clear()

    main.app.dependency_overrides[main.get_db] = override_get_db
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


def signup(client, email, name="Test User", password="password123"):
    """Creates a user and returns ready-to-use auth headers for them."""
    response = client.post("/auth/signup", json={"name": name, "email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def alice(client):
    return signup(client, "alice@example.com", name="Alice")


@pytest.fixture
def bob(client):
    return signup(client, "bob@example.com", name="Bob")


LONG_AGO = date.today() - timedelta(days=30)


def log_trade(client, headers, ticker, action, quantity, price, trade_date=LONG_AGO,
              conviction=3, review_date=None, reason_tags=None):
    """Logs a trade through the real POST /trades endpoint and returns its JSON."""
    response = client.post("/trades", headers=headers, json={
        "ticker": ticker,
        "action": action,
        "quantity": quantity,
        "price_per_share": price,
        "trade_date": trade_date.isoformat(),
        "trade_time": "10:30:00",
        "thesis_text": f"Test thesis for {ticker}",
        "conviction_score": conviction,
        "review_date": (review_date or date.today() + timedelta(days=30)).isoformat(),
        "reason_tags": reason_tags or [],
    })
    assert response.status_code == 200, response.text
    return response.json()
