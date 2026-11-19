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
from datetime import date, datetime, time, timedelta, timezone
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

def _t(days, ticker, action, qty, reasons=None, thesis_text=None, conviction=None, review=None,
       graded=False, fallback=100.0, place=None, synced=False, written_after=0):
    """One sample trade.

    days      how many days ago (moved to the last trading day if needed)
    reasons   reason slugs; None = arrived from the brokerage with no reason
              (-> the Needs a reason inbox); ["dont_remember"] = answered
              "I don't remember" from the inbox
    review    months after the trade until it's due for review
    graded    True: "the user" graded it with the numbers' suggestion, so
              Recently reviewed and the reasons stats have history.
              "disagree": graded against the numbers, with a note (shows
              up as "graded against the evidence").
              False + review date passed: auto-graded on the first visit,
              unless prices can't judge it (rebalance / need-cash sells
              stay in Due for review for the visitor to grade).
    place     ("hot", (from, to)): moved to the hottest real day in that
              window of days ago, bought at the opening bell - a genuine
              chase for the FOMO check. ("dip", (from, to)): moved to the
              sharpest real drop in the window. Stays put if none.
    synced    came from the brokerage (reason added later in the inbox)
    written_after  days after the trade the reason was written; 7+ counts
              as hindsight in the reasons stats
    """
    return dict(days=days, ticker=ticker, action=action, qty=qty, reasons=reasons, thesis_text=thesis_text,
                conviction=conviction, review=review, graded=graded, fallback=fallback, place=place,
                synced=synced or reasons is None, written_after=written_after)


# NEW: a fuller sample journal (~28 trades) so every feature has something
# to show: US + TSX stocks, an ETF and a CDR (GOOG.TO), dividends and
# growth, a closed position (TSLA), trims, every grading path (graded by
# the user, graded against the numbers, auto-graded on first visit, left
# for the visitor, still upcoming), a real chase the user admitted (AMD)
# and one they didn't (PLTR), a real dip buy, a "don't remember" answer,
# a reason written in hindsight, and four synced trades waiting for a reason.
# Plus 1-2 holdings in companies that JUST reported earnings (see
# _recent_reporters), so the notifications bell has fresh recaps.
DEMO_TRADES = [
    _t(400, "XEQT.TO", "buy", 60, ["dca", "long_term"],
       "Core holding: one global all-equity ETF, bought a bit every month and left alone.", 5, 12, True, 30.0),
    _t(370, "RY.TO", "buy", 25, ["valuation", "long_term"],
       "Canada's biggest bank at about 11x earnings with a 4% dividend - paid to wait.", 4, 6, True, 140.0),
    _t(340, "MSFT", "buy", 5, ["long_term"],
       "Azure growth plus Office price increases; the safest way to own AI.", 5, 6, True, 400.0),
    _t(300, "AAPL", "buy", 8, ["long_term", "valuation"],
       "Services keep growing and buybacks shrink the share count; fair price for the quality.", 4, 6, True, 220.0),
    _t(280, "ENB.TO", "buy", 40, ["long_term"],
       "Pipelines are a toll road: a 7% dividend covered by long-term contracts.", 3, 6, True, 50.0),
    _t(250, "NVDA", "buy", 6, ["momentum", "news_earnings"],
       "Data-center demand keeps beating estimates - riding the AI spending cycle.", 4, 3, True, 120.0),
    _t(240, "TSLA", "buy", 4, ["tip_social", "speculation"],
       "Robotaxi hype is everywhere - a small bet in case it's real.", 2, 2, "disagree", 250.0),
    _t(200, "SHOP.TO", "buy", 15, ["dip_buy"],
       "Dropped hard on a guidance wobble, but merchant growth is still above 25%.", 3, 3, True, 140.0,
       place=("dip", (170, 230))),
    _t(190, "TSLA", "sell", 4, ["stop_loss"],
       "Story felt shaky and I'd broken my own rule on position size - cut it.", 3, 2, True, 240.0),
    _t(180, "GOOG.TO", "buy", 30, ["valuation"],
       "Search fears look overdone; cloud is speeding up and it trades below the market multiple.", 4, 6, False, 35.0),
    _t(150, "AMD", "buy", 10, ["tip_social", "fomo"],
       "Everyone online says it's the next Nvidia and I didn't want to miss it.", 2, 1, True, 150.0,
       place=("hot", (120, 200))),
    _t(140, "PLTR", "buy", 12, ["momentum"],
       "Breaking out to new highs on government AI contracts.", 3, 3, False, 100.0,
       place=("hot", (95, 175))),
    _t(130, "LLY", "buy", 2, ["news_earnings"],
       "Weight-loss drug sales doubling and supply is finally catching up.", 4, 3, False, 800.0),
    _t(110, "ENB.TO", "sell", 15, ["rebalance"],
       "Energy grew past my 10% target - trimmed back to it.", 3, 3, False, 55.0),
    _t(100, "AAPL", "sell", 3, ["take_profit"],
       "Trimmed after a strong run to keep it from getting too big a slice of the portfolio.", 3, 3, False, 230.0),
    _t(80, "RY.TO", "sell", 10, ["need_cash"],
       "Needed the money for a car repair.", 2, 2, False, 160.0),
    _t(75, "SHOP.TO", "sell", 5, ["take_profit"],
       "Up a lot since the dip - locked some of it in.", 3, 2, False, 180.0),
    _t(70, "NVDA", "sell", 2, ["dont_remember"], fallback=170.0, synced=True, written_after=3),
    _t(65, "JPM", "buy", 5, ["valuation"],
       "Best-run US bank, and lower rates should help loan growth.", 4, 2, False, 280.0),
    _t(60, "XEQT.TO", "buy", 20, ["dca"],
       "Monthly automatic buy.", 5, 12, False, 32.0),
    _t(45, "COST", "buy", 3, ["long_term"],
       "Membership renewals near 90% - the steadiest compounder I know.", 4, 6, False, 900.0),
    _t(40, "MSFT", "buy", 2, ["dip_buy"],
       "Fell on worries about AI spending - added to a core holding.", 4, 6, False, 420.0,
       place=("dip", (20, 90))),
    _t(35, "AMZN", "buy", 4, ["news_earnings"],
       "AWS growth re-accelerated in the last report.", 3, 6, False, 220.0, synced=True, written_after=25),
    _t(20, "NVDA", "buy", 2, fallback=180.0),
    _t(12, "SHOP.TO", "buy", 5, fallback=150.0),
    _t(8, "AAPL", "sell", 2, fallback=240.0),
    _t(5, "XEQT.TO", "buy", 10, fallback=33.0),
]
DEMO_WATCHLIST = ["META", "TD.TO", "V", "UBER"]

# NEW: big, well-known companies spread across the earnings calendar. When
# a demo starts, any of these that reported in the last few days (and
# isn't already in the journal) becomes a holding bought ~3 months ago, so
# the visitor gets a real, fresh earnings recap. At most RECENT_REPORTERS_MAX.
EARNINGS_POOL = ["NVDA", "HD", "DIS", "CSCO", "JPM", "NFLX", "MSFT", "AMZN", "WMT", "ORCL",
                 "ADBE", "NKE", "MU", "FDX", "KO", "UNH", "META", "TSLA", "CRM", "AVGO",
                 "LULU", "TGT", "LOW", "COST", "RY.TO", "TD.TO", "SHOP.TO"]
RECENT_REPORTERS_MAX = 2
RECENT_REPORTER_DOLLARS = 2500


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


def _real_prices(today: date, trades=DEMO_TRADES) -> dict:
    """{ticker: [(date, close), ...]} for every demo ticker, fetched in parallel."""
    tickers = sorted({t["ticker"] for t in trades})
    start = today - timedelta(days=max(t["days"] for t in trades) + 10)
    with ThreadPoolExecutor(max_workers=min(12, len(tickers))) as pool:
        histories = list(pool.map(lambda t: grading.fetch_daily_closes(t, start, today), tickers))
    return dict(zip(tickers, histories))


def _recent_reporters(today: date, exclude: set) -> list:
    """Up to RECENT_REPORTERS_MAX tickers from EARNINGS_POOL whose results
    came out in the last few days (earnings_recaps' own rule), as extra
    holdings bought ~3 months ago. Never raises - it's a nice-to-have."""
    import earnings_recaps  # lazy, like main's own use of it
    now = datetime.now(timezone.utc)
    held_since = today - timedelta(days=95)

    def reported(ticker):
        try:
            return earnings_recaps.find_recent_report(ticker, held_since, now)
        except Exception:
            return None

    pool_tickers = [t for t in EARNINGS_POOL if t not in exclude]
    # One thread per ticker: it's all waiting on Yahoo, and it's cached after.
    with ThreadPoolExecutor(max_workers=max(1, len(pool_tickers))) as pool:
        found = [r for r in pool.map(reported, pool_tickers) if r]
    found.sort(key=lambda r: r.report_date, reverse=True)  # freshest first
    extra = []
    for r in found[:RECENT_REPORTERS_MAX]:
        name = (r.profile or {}).get("name") or r.display_ticker
        extra.append(_t(95, r.display_ticker, "buy", 0, ["long_term"],
                        f"Long-term position in {name}; holding through earnings to see if the story holds up.",
                        3, 6, False, 0.0))
    return extra


# NEW: trades with place=("hot", window) - the admitted FOMO buy (AMD) and
# the unadmitted chase (PLTR) - are moved onto a REAL hot day for that stock
# (the highest FOMO score in that window of days ago, bought at the opening
# bell), so the FOMO check has genuine chases to show; place=("dip", ...)
# finds the sharpest real drop instead. If no such day exists in the
# window, the trade stays on its default date.
FOMO_DEMO_WINDOW = (120, 200)
FOMO_DEMO_TIME = time(9, 40)


def _checked_days(ticker: str, closes: list, today: date, window) -> list:
    """[(day, close, check)]: the FOMO check (bought at the opening bell) on
    every trading day in the window of days ago."""
    if not closes:
        return []
    start = closes[0][0]
    volumes = grading.fetch_daily_volumes(ticker, start, today)
    benchmark = grading.benchmark_for(ticker)
    bench = grading.fetch_daily_closes(benchmark[0], start, today) if benchmark else []
    checked = []
    for day, close in closes:
        if not window[0] <= (today - day).days <= window[1]:
            continue
        checked.append((day, close, fomo.score_buy(trade_date=day, trade_time=FOMO_DEMO_TIME, price=close, quantity=0,
                                                   closes=closes, volumes=volumes, bench_closes=bench)))
    return checked


def _hot_days(ticker: str, closes: list, today: date, window=FOMO_DEMO_WINDOW) -> list:
    """[(day, close, check)] for every warm/chasing day in the window."""
    return [c for c in _checked_days(ticker, closes, today, window) if c[2]["band"] in ("warm", "chasing")]


def _sharpest_dip(ticker: str, closes: list, today: date, window):
    """The day in the window that was the deepest "dip" (lowest z), or None."""
    dips = [c for c in _checked_days(ticker, closes, today, window) if c[2]["band"] == "dip"]
    return min(dips, key=lambda c: c[2]["stats"]["z"], default=None)


def _hottest(ticker: str, closes: list, today: date, window=FOMO_DEMO_WINDOW):
    """(day, close, check) for the highest warm/chasing score in the window, or None."""
    return max(_hot_days(ticker, closes, today, window), key=lambda h: h[2]["score"], default=None)



# NEW: one answered "Wait 3 days" so the demo shows how it works: a real
# warm/chasing day across the demo's stocks in the last couple of months
# (with 3 sessions after it), as if the demo investor waited instead of
# buying. A wait that barely moved teaches nothing, so the pick prefers a
# day where waiting made a real difference: first one where the stock was
# DEMO_WAIT_MIN_MOVE % cheaper 3 sessions later (waiting paid off), then
# one where it was that much dearer, then simply the hottest day.
# Skipped when no recent day was even warm.
DEMO_WAIT_WINDOW = (7, 60)
DEMO_WAIT_DOLLARS = 2000
DEMO_WAIT_MIN_MOVE = 2.0


def _three_days_later_pct(closes: list, day: date):
    after = [c for c in closes if c[0] > day]
    if len(after) < fomo.WAIT_SESSIONS:
        return None
    close_then = dict(closes)[day]
    return (after[fomo.WAIT_SESSIONS - 1][1] - close_then) / close_then * 100


def _pick_demo_wait(candidates: list):
    """candidates: [(ticker, day, close, check, move_pct)]. See the comment above."""
    paid_off = [c for c in candidates if c[4] <= -DEMO_WAIT_MIN_MOVE]
    if paid_off:
        return max(paid_off, key=lambda c: (c[3]["score"], -c[4]))
    moved = [c for c in candidates if abs(c[4]) >= DEMO_WAIT_MIN_MOVE]
    if moved:
        return max(moved, key=lambda c: (c[3]["score"], abs(c[4])))
    return max(candidates, key=lambda c: c[3]["score"], default=None)


def _seed_wait(db: Session, user: User, histories: dict, today: date):
    """The best "waiting paid off" pick (see _pick_demo_wait), plus - if
    there is one - a different stock where waiting COST money, so the demo
    shows both answers honestly."""
    tickers = [t for t, closes in histories.items() if closes]
    if not tickers:
        return
    with ThreadPoolExecutor(max_workers=min(12, len(tickers))) as pool:
        found = list(pool.map(lambda t: (t, _hot_days(t, histories[t], today, DEMO_WAIT_WINDOW)), tickers))
    candidates = []
    for ticker, days in found:
        for day, close, check in days:
            move = _three_days_later_pct(histories[ticker], day)
            if move is not None:
                candidates.append((ticker, day, close, check, move))
    first = _pick_demo_wait(candidates)
    if not first:
        return
    picks = [first]
    cost = [c for c in candidates if c[4] >= DEMO_WAIT_MIN_MOVE and c[0] != first[0]]
    if first[4] < 0 and cost:
        picks.append(max(cost, key=lambda c: (c[3]["score"], c[4])))
    for ticker, day, close, check, _ in picks:
        db.add(FomoWait(
            user_id=user.id, ticker=ticker, price=round(close, 2), quantity=round(DEMO_WAIT_DOLLARS / close, 4),
            score=check["score"], band=check["band"], summary=check["summary"],
            check_date=day, remind_on=fomo.add_sessions(day, fomo.WAIT_SESSIONS),
        ))
    db.commit()
    fomo.resolve_waits(db, user, today)


# "The user" graded this against the numbers - and had to write why.
DISAGREE_NOTES = {
    "correct": "I still think the bet was right - the market just hasn't seen it yet.",
    "incorrect": "It went up, but I got lucky - I didn't really know why I bought it.",
}


def _placed(spec: dict, histories: dict, today: date):
    """(trade_day, trade_time) for a sample trade, after any hot/dip placement."""
    trade_day, trade_time = today - timedelta(days=spec["days"]), time(10, 30)
    if spec["place"]:
        kind, window = spec["place"]
        closes = histories.get(spec["ticker"]) or []
        found = (_hottest(spec["ticker"], closes, today, window) if kind == "hot"
                 else _sharpest_dip(spec["ticker"], closes, today, window))
        if found:
            trade_day, trade_time = found[0], FOMO_DEMO_TIME if kind == "hot" else time(11, 15)
    return trade_day, trade_time


def seed(db: Session, user: User, today: date = None):
    today = today or date.today()
    journal_tickers = {t["ticker"] for t in DEMO_TRADES}
    # The earnings lookup (~25 tickers, slow on a cold start, then cached)
    # runs alongside the price downloads and hot/dip placement.
    with ThreadPoolExecutor(max_workers=1) as background:
        reporters = background.submit(_recent_reporters, today, journal_tickers | set(DEMO_WATCHLIST))
        histories = _real_prices(today)
        # Placement runs the FOMO check over windows of real prices - in parallel.
        with ThreadPoolExecutor(max_workers=8) as pool:
            placements = list(pool.map(lambda spec: _placed(spec, histories, today), DEMO_TRADES))
        extra = reporters.result()
    if extra:
        histories.update(_real_prices(today, extra))
        placements += [_placed(spec, histories, today) for spec in extra]
    trades = DEMO_TRADES + extra

    db.add(UserProfile(user_id=user.id, **DEMO_PROFILE))
    for ticker in DEMO_WATCHLIST:
        db.add(Watchlist(user_id=user.id, ticker=ticker))

    to_grade = []
    for spec, (trade_day, trade_time) in zip(trades, placements):
        close = grading.close_on_or_before(histories.get(spec["ticker"]) or [], trade_day)
        price = round(close[1], 2) if close else spec["fallback"]
        if close:
            trade_day = close[0]  # a weekend date moves to the last trading day
        qty = spec["qty"]
        if not qty:  # recent-earnings holdings are sized in dollars
            if not price:
                continue
            qty = max(1, round(RECENT_REPORTER_DOLLARS / price))
        reasons = spec["reasons"]
        trade = Trade(
            user_id=user.id, ticker=spec["ticker"], action=spec["action"], quantity=qty, price_per_share=price,
            trade_date=trade_day, trade_time=trade_time,
            source="snaptrade" if spec["synced"] else "manual",
            thesis_text=spec["thesis_text"], conviction_score=spec["conviction"],
            reason_tags=",".join(reasons) if reasons else None,
            review_date=thesis.add_months(trade_day, spec["review"]) if spec["review"] else None,
            # Written at the time of the trade unless written_after says otherwise.
            thesis_written_at=None if reasons is None
            else datetime.combine(trade_day + timedelta(days=spec["written_after"]), time(15, 0)),
        )
        db.add(trade)
        if spec["graded"]:
            to_grade.append((trade, spec["graded"]))
    db.commit()

    # Past grades "the user" gave: the numbers' own suggestion, with the
    # snapshot saved exactly like a real grade - except ONE graded against
    # the numbers, with the note that requires. Only a clear verdict
    # (correct / incorrect) can be contradicted, so if the trade marked
    # "disagree" came out "mixed" on real prices, another graded trade with
    # a clear verdict takes its place.
    for trade, _ in to_grade:
        db.refresh(trade)  # load the committed rows here: the session isn't thread-safe
    with ThreadPoolExecutor(max_workers=8) as pool:  # scorecard() only reads the trade's own columns
        found = list(pool.map(lambda item: grading.scorecard(item[0], today), to_grade))
    cards = [(trade, how, card) for (trade, how), card in zip(to_grade, found)]
    cards = [(t, how, c) for t, how, c in cards if c.get("available") and c.get("suggestion")]
    clear = [t for t, _, c in cards if c["suggestion"]["outcome"] in ("correct", "incorrect")]
    wanted = [t for t, how, _ in cards if how == "disagree"]
    against = next((t for t in wanted if t in clear), clear[-1] if clear else None)
    for trade, _, card in cards:
        outcome, notes = card["suggestion"]["outcome"], None
        if trade is against:
            outcome = "correct" if outcome == "incorrect" else "incorrect"
            notes = DISAGREE_NOTES[outcome]
        grading._save_grade(trade, outcome, notes, card, auto=False)
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
