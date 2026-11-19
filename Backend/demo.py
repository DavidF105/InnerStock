# "Try the demo": POST /auth/demo creates a private, throwaway account full
# of sample data and logs the visitor straight into it.
#
# Why a fresh account per visitor instead of one shared demo login: with a
# shared one, the first visitor could answer every trade or grade
# everything (ruining it for the next), and everyone would read each
# other's advisor chats.
#
# The sample journal uses REAL prices: every trade is dated relative to
# today ("9 months ago") and priced at that day's actual close, so gains,
# S&P 500 comparisons and grades are all real. If price history can't be
# fetched, fallback prices keep the demo working (the grades just come
# out "from memory").
#
# Limits: 24-hour lifetime (old demos are deleted whenever a new one
# starts - no scheduled job needed), at most DEMOS_PER_IP_PER_HOUR new
# demos per visitor address, a smaller advisor allowance (main.py
# "demo_chat"), and no email or brokerage features.
import secrets
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import fomo
import grading
import thesis
from auth import create_access_token, hash_password
from deps import get_db
from models import (BrokerageAccount, ChatMessage, EarningsNotification, EmailToken, FomoWait, SnapTradeUser, Trade, User,
                    UserProfile, Watchlist)

router = APIRouter(prefix="/auth", tags=["demo"])

DEMO_LIFETIME = timedelta(hours=24)
DEMOS_PER_IP_PER_HOUR = 5
_demo_starts = defaultdict(deque)
_demo_lock = threading.Lock()

DEMO_PROFILE = {
    "risk_tolerance": "medium",
    "investing_goals": "growth",
    "trading_style": "buy_and_hold,swing",
    "time_horizon": "5y_plus",
    "income_bracket": "50k_100k",
    "experience_level": "intermediate",
    "sectors_of_interest": "tech,financials,consumer",
    "biggest_fear": "Selling my winners too early and holding my losers too long.",
}

# (days ago, ticker, action, quantity, reasons, thesis, conviction, review months, user grades it?, fallback price)
# - user grades it: graded by "the user" with the numbers' suggested grade,
#   so the Recently reviewed list and the reasons stats have history
# - due but not graded: auto-graded the first time the visitor opens
#   Due for review, so they see automatic grading happen
# - reasons None: arrived "from the brokerage" without a reason -> inbox
DEMO_TRADES = [
    (330, "XEQT.TO", "buy", 60, ["dca", "long_term"],
     "Core holding: one global all-equity ETF, bought a bit every month and left alone.", 5, 12, False, 30.0),
    (300, "AAPL", "buy", 8, ["long_term", "valuation"],
     "Services keep growing and buybacks shrink the share count; fair price for the quality.", 4, 6, True, 220.0),
    (250, "NVDA", "buy", 6, ["momentum", "news_earnings"],
     "Data-center demand keeps beating estimates - riding the AI spending cycle.", 4, 3, True, 120.0),
    (200, "SHOP.TO", "buy", 15, ["dip_buy"],
     "Dropped 20% on a guidance wobble, but merchant growth is still above 25%.", 3, 3, True, 140.0),
    (180, "GOOG.TO", "buy", 30, ["valuation"],
     "Search fears look overdone; cloud is speeding up and it trades below the market multiple.", 4, 6, False, 35.0),
    (150, "AMD", "buy", 10, ["tip_social", "fomo"],
     "Everyone online says it's the next Nvidia and I didn't want to miss it.", 2, 1, True, 150.0),
    (100, "AAPL", "sell", 3, ["take_profit"],
     "Trimmed after a strong run to keep it from getting too big a slice of the portfolio.", 3, 3, False, 230.0),
    (60, "XEQT.TO", "buy", 20, ["dca"],
     "Monthly automatic buy.", 5, 12, False, 32.0),
    (45, "COST", "buy", 3, ["long_term"],
     "Membership renewals near 90% - the steadiest compounder I know.", 4, 6, False, 900.0),
    (20, "NVDA", "buy", 2, None, None, None, None, False, 180.0),
    (12, "SHOP.TO", "buy", 5, None, None, None, None, False, 150.0),
    (5, "XEQT.TO", "buy", 10, None, None, None, None, False, 33.0),
]
DEMO_WATCHLIST = ["MSFT", "AMZN", "RY.TO"]


def _client_ip(request: Request) -> str:
    # On Vercel the visitor's address is the first entry of X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_ip_limit(ip: str):
    now = monotonic()
    with _demo_lock:
        starts = _demo_starts[ip]
        while starts and now - starts[0] >= 3600:
            starts.popleft()
        if len(starts) >= DEMOS_PER_IP_PER_HOUR:
            raise HTTPException(status_code=429, detail="You've started several demos already - try again in a bit, or create a free account.")
        starts.append(now)


def delete_users(db: Session, user_ids: list):
    """Removes these users and everything that belongs to them."""
    if not user_ids:
        return
    for model in (EarningsNotification, FomoWait, ChatMessage, Watchlist, EmailToken, UserProfile, Trade, BrokerageAccount, SnapTradeUser):
        db.query(model).filter(model.user_id.in_(user_ids)).delete(synchronize_session=False)
    db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.commit()


def cleanup_expired(db: Session):
    cutoff = datetime.utcnow() - DEMO_LIFETIME
    expired = [u.id for u in db.query(User.id).filter(User.is_demo.is_(True), User.created_at < cutoff).all()]
    delete_users(db, expired)
    return len(expired)


def _real_prices(today: date) -> dict:
    """{ticker: [(date, close), ...]} for every demo ticker, fetched in parallel."""
    tickers = sorted({t[1] for t in DEMO_TRADES})
    start = today - timedelta(days=max(t[0] for t in DEMO_TRADES) + 10)
    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        histories = list(pool.map(lambda t: grading.fetch_daily_closes(t, start, today), tickers))
    return dict(zip(tickers, histories))


# NEW: the buy tagged "Fear of missing out" is placed on a REAL hot day for
# that stock (the highest FOMO score in this window of days ago, bought at
# the opening bell), so the demo's FOMO check has a genuine chase to show.
# If no day in the window was even "warm", it stays on its default date.
FOMO_DEMO_WINDOW = (120, 200)
FOMO_DEMO_TIME = time(9, 40)


def _hottest(ticker: str, closes: list, today: date, window=FOMO_DEMO_WINDOW):
    """(day, close, check) for the highest warm/chasing score in the window, or None."""
    if not closes:
        return None
    start = closes[0][0]
    volumes = grading.fetch_daily_volumes(ticker, start, today)
    benchmark = grading.benchmark_for(ticker)
    bench = grading.fetch_daily_closes(benchmark[0], start, today) if benchmark else []
    best = None
    for day, close in closes:
        if not window[0] <= (today - day).days <= window[1]:
            continue
        result = fomo.score_buy(trade_date=day, trade_time=FOMO_DEMO_TIME, price=close, quantity=0,
                                closes=closes, volumes=volumes, bench_closes=bench)
        if result["band"] in ("warm", "chasing") and (not best or result["score"] > best[2]["score"]):
            best = (day, close, result)
    return best


def _hottest_day(ticker: str, closes: list, today: date):
    best = _hottest(ticker, closes, today)
    return best[0] if best else None


# NEW: one answered "Wait 3 days" so the demo shows how it works: the
# hottest real day across the demo's stocks in the last few weeks (with 3
# sessions after it), as if the demo investor waited instead of buying.
# Skipped when no recent day was even warm.
DEMO_WAIT_WINDOW = (7, 40)
DEMO_WAIT_DOLLARS = 1500


def _seed_wait(db: Session, user: User, histories: dict, today: date):
    tickers = [t for t, closes in histories.items() if closes]
    if not tickers:
        return
    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        found = list(pool.map(lambda t: (t, _hottest(t, histories[t], today, DEMO_WAIT_WINDOW)), tickers))
    found = [(t, best) for t, best in found if best]
    if not found:
        return
    ticker, (day, close, check) = max(found, key=lambda f: f[1][2]["score"])
    db.add(FomoWait(
        user_id=user.id, ticker=ticker, price=round(close, 2), quantity=round(DEMO_WAIT_DOLLARS / close, 4),
        score=check["score"], band=check["band"], summary=check["summary"],
        check_date=day, remind_on=fomo.add_sessions(day, fomo.WAIT_SESSIONS),
    ))
    db.commit()
    fomo.resolve_waits(db, user, today)


def seed(db: Session, user: User, today: date = None):
    today = today or date.today()
    histories = _real_prices(today)

    db.add(UserProfile(user_id=user.id, **DEMO_PROFILE))
    for ticker in DEMO_WATCHLIST:
        db.add(Watchlist(user_id=user.id, ticker=ticker))

    to_grade = []
    for days_ago, ticker, action, qty, reasons, thesis_text, conviction, review_months, user_grades, fallback in DEMO_TRADES:
        trade_day = today - timedelta(days=days_ago)
        trade_time = time(10, 30)
        if reasons and "fomo" in reasons:
            hot_day = _hottest_day(ticker, histories.get(ticker) or [], today)
            if hot_day:
                trade_day, trade_time = hot_day, FOMO_DEMO_TIME
        close = grading.close_on_or_before(histories.get(ticker) or [], trade_day)
        price = round(close[1], 2) if close else fallback
        if close:
            trade_day = close[0]  # a weekend date moves to the last trading day
        synced = reasons is None
        trade = Trade(
            user_id=user.id, ticker=ticker, action=action, quantity=qty, price_per_share=price,
            trade_date=trade_day, trade_time=trade_time,
            source="snaptrade" if synced else "manual",
            thesis_text=thesis_text, conviction_score=conviction,
            reason_tags=",".join(reasons) if reasons else None,
            review_date=thesis.add_months(trade_day, review_months) if review_months else None,
            # Written at the time of the trade, so none count as hindsight.
            thesis_written_at=None if synced else datetime.combine(trade_day, time(15, 0)),
        )
        db.add(trade)
        if user_grades:
            to_grade.append(trade)
    db.commit()

    # Past grades "the user" gave: the numbers' own suggestion, with the
    # snapshot saved exactly like a real grade.
    for trade in to_grade:
        card = grading.scorecard(trade, today)
        suggestion = card.get("suggestion")
        if card.get("available") and suggestion:
            grading._save_grade(trade, suggestion["outcome"], None, card, auto=False)
            # Graded shortly after its review date, not today.
            trade.graded_at = datetime.combine(min(trade.review_date + timedelta(days=2), today), time(18, 0))
    db.commit()

    try:
        _seed_wait(db, user, histories, today)
    except Exception as e:  # a nice-to-have: never break the demo over it
        db.rollback()
        print(f"[demo] couldn't seed a wait: {e}")


@router.post("/demo")
def start_demo(request: Request, db: Session = Depends(get_db)):
    _check_ip_limit(_client_ip(request))
    cleanup_expired(db)

    user = User(
        name="Demo investor",
        email=f"demo-{secrets.token_hex(5)}@demo.innerstock",
        # Random and never shown: nobody can log into a demo with a password.
        hashed_password=hash_password(secrets.token_urlsafe(24)),
        is_demo=True,
    )
    db.add(user)
    db.commit()
    seed(db, user)
    return {"access_token": create_access_token(user.id), "token_type": "bearer"}
