# NEW: the FOMO check - "was this buy a chase?"
#
# The old check was one rule: a buy was flagged when the stock had moved
# more than 8% over the 5 trading days before it. That missed the point in
# four ways: 8% is a huge move for a bank and a normal Tuesday for a meme
# stock; it flagged crashes as FOMO too (buying a dip is the opposite habit);
# it ignored the market (if everything rose 8% you didn't chase THIS stock);
# and it only ever showed up on the review card months later.
#
# Now every buy gets a FOMO score from 0 to 100, built from signals that can
# each be explained in one sentence:
#
#   Was the stock "hot"? (from daily prices and volume)
#     run-up       its 5-day move minus the market's, measured in units of
#                  the stock's OWN normal 5-day swing (60-day volatility):
#                  a z-score. 2+ = a move this stock rarely makes.    +30 / +15
#     near_high    bought within 3% of its 20-day high, after a run-up   +10
#     volume       the day's volume was 2x+ its 20-day average           +15
#     jump         a one-day jump of 3+ normal days in the last 2 days   +10
#                  (news / earnings style move)
#
#   Was the user rushed? (from their own journal - a brokerage can't see this)
#     opening      bought 9:30-10:00 ET, the most emotional half hour    +10
#     oversized    2x+ their usual (median) buy                          +10
#     new_name     a ticker they've never traded and don't watch         +5
#     chase_back   buying back something they sold in the last 30 days,
#                  at a higher price                                     +10
#
# Bands: 60+ chasing, 30-59 warm, under 30 calm. A sharp DROP (z <= -1.5)
# is labelled "dip" instead - it's its own habit, tracked separately.
#
# What makes it personal (personal_record):
#   - the FOMO tax: what chasing buys cost vs just buying the S&P 500, in
#     dollars, from the grading snapshot (return vs benchmark at review)
#   - the patience test: was the stock cheaper 5 trading days after the
#     buy? (week_later_pct) - "would waiting a week have helped?"
#   - self-labels: trades the user tagged "Fear of missing out" count as
#     chasing whatever the score says, and the detector is checked against
#     them ("it caught 2 of the 3 you admitted")
#   - shrinkage: with only a few graded chasing buys, the user's average is
#     blended with everyone else's (expected_excess_pct), so one lucky trade
#     can't make chasing look smart. Their own record takes over as it grows.
#
# NEW - "what would waiting have done?", two ways:
#   - cool-off (past buys): for every chasing/warm buy, the market half of
#     the check is re-run on each of the next 10 sessions. The first one
#     that scores calm is "when it cooled off"; buying at that close instead
#     is what waiting would have cost or saved, in dollars (cool_off()).
#   - "Wait 3 days" (future buys): when the check flags a buy before it's
#     made, the user can wait instead. The price is saved (FomoWait) and 3
#     sessions later the close is filled in and shown as a notification:
#     "waiting saved $14 a share". Buys the user DIDN'T make become data.
#
# Scores are stored on the trade (Trade.fomo_*), worked out lazily by
# ensure_scored() the first time something needs them.
import json
import math
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import grading
import thesis
from deps import get_current_user, get_db
from models import FomoWait, Trade, User, Watchlist

router = APIRouter(tags=["fomo"])

MARKET_TZ = ZoneInfo("America/New_York")

# --- The rules ---------------------------------------------------------------------

HISTORY_DAYS = 120         # calendar days of prices before the trade (~80 sessions)
MIN_SESSIONS = 25          # fewer closes than this before the trade = "unknown"
RUNUP_SESSIONS = 5         # "the week before" the buy
VOL_SESSIONS = 60          # how the stock normally moves
HIGH_SESSIONS = 20
VOLUME_SESSIONS = 20
MIN_DAILY_VOL = 0.0025     # floor on daily volatility (0.25%) so near-flat funds don't explode the z-score

RUNUP_STRONG_Z = 2.0
RUNUP_MILD_Z = 1.5
DIP_Z = -1.5
NEAR_HIGH_PCT = 3.0
NEAR_HIGH_MIN_Z = 1.0      # "near the top" only matters after a run-up
VOLUME_SPIKE = 2.0
JUMP_SIGMAS = 3.0
OPENING_START, OPENING_END = time(9, 30), time(10, 0)
OVERSIZED_RATIO = 2.0
MIN_PRIOR_BUYS = 3         # need a few buys before "your usual size" means anything
NEW_NAME_MIN_TRADES = 3    # ...and before "a name you've never traded" does
CHASE_BACK_DAYS = 30

POINTS = {
    "runup_strong": 30, "runup_mild": 15, "near_high": 10, "volume": 15, "jump": 10,
    "opening": 10, "oversized": 10, "new_name": 5, "chase_back": 10,
}
CHASING_SCORE = 60
WARM_SCORE = 30

# Patience test: the price 5 sessions after the buy. Looked for once this
# many calendar days have passed (a week of sessions plus a weekend/holiday).
WEEK_SESSIONS = 5
WEEK_LATER_DAYS = 9

# Cool-off: how long to look for the stock calming down after a hot buy,
# and when to look (10 sessions = two weeks, plus room for holidays).
COOL_OFF_SESSIONS = 10
COOL_OFF_DAYS = 16
HOT_BANDS = ("chasing", "warm")

# "Wait 3 days": the answer is the close this many sessions after the check.
WAIT_SESSIONS = 3
NO_REAL_DIFFERENCE_PCT = 1.0   # a move smaller than this is "about the same", not a win or a loss
MAX_OPEN_WAITS = 20
MAX_RESOLVE_PER_REQUEST = 10
WAITS_LISTED = 30

RESCORE_UNKNOWN_AFTER = timedelta(hours=1)  # retry "not enough history" (and cool-off gaps) hourly - cheap, prices are cached
MAX_SCORE_PER_REQUEST = 30
MAX_WORKERS = 8

# Shrinkage strength: the user's own average counts as much as the
# population's once they have this many graded chasing buys.
SHRINK_K = 5
MIN_POPULATION = 10        # below this, the prior is "no difference" (0 points)

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")


# --- The score (pure: no database, no network) ---------------------------------------

@dataclass
class Habits:
    """What the user's own journal says about this buy (see habits_for)."""
    prior_buy_values: list
    prior_trade_count: int
    traded_before: bool
    on_watchlist: bool
    recent_sell: Optional[tuple]  # (date, price) of the latest sell of this ticker in the last 30 days


NO_HABITS = Habits([], 0, False, False, None)


def _pct(start, end):
    return (end - start) / start * 100


def _close_on_or_before(closes, day):
    candidates = [c for c in closes if c[0] <= day]
    return candidates[-1] if candidates else None


def _clock(t: time) -> str:
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _signal(key, label, detail, points):
    return {"key": key, "label": label, "detail": detail, "points": points}


def entry_price(closes, trade_date, price):
    """The price to measure from: what was paid, unless it's wildly off the
    trade day's close (a later stock split - see grading.scorecard)."""
    ref = _close_on_or_before(closes, trade_date)
    if not ref:
        return price
    if price and grading.SPLIT_RATIO_RANGE[0] <= price / ref[1] <= grading.SPLIT_RATIO_RANGE[1]:
        return price
    return ref[1]


def score_buy(*, trade_date, trade_time, price, quantity, closes, volumes, bench_closes, habits=NO_HABITS) -> dict:
    """The FOMO check for one buy.

    closes / bench_closes: [(date, close)] oldest first, at least up to the
    trade date (later rows are ignored). volumes: {date: shares}. price may
    be None (then the trade day's close is used).
    """
    before = [c for c in closes if c[0] < trade_date]
    if len(before) < MIN_SESSIONS:
        return {"score": None, "band": "unknown", "signals": [], "stats": None,
                "summary": "Not enough price history to check this buy."}

    entry = entry_price(closes, trade_date, price)
    # "Normal" is measured on the sessions BEFORE the week being judged, so
    # a big run-up can't inflate its own yardstick and hide itself.
    yardstick = before[:-(RUNUP_SESSIONS - 1)]
    returns = [b / a - 1 for (_, a), (_, b) in zip(yardstick, yardstick[1:])]
    sigma = max(statistics.pstdev(returns[-VOL_SESSIONS:]), MIN_DAILY_VOL)
    normal_week = sigma * math.sqrt(RUNUP_SESSIONS) * 100

    base_date, base_close = before[-RUNUP_SESSIONS]
    runup = _pct(base_close, entry)
    market = None
    bench_start = _close_on_or_before(bench_closes, base_date) if bench_closes else None
    bench_end = _close_on_or_before(bench_closes, trade_date) if bench_closes else None
    if bench_start and bench_end and bench_end[0] > bench_start[0]:
        market = _pct(bench_start[1], bench_end[1])
    excess = runup - (market or 0)
    z = excess / normal_week

    signals = []
    vs_market = f", {excess:+.1f} points vs the market" if market is not None else ""
    dip = z <= DIP_Z
    if dip:
        signals.append(_signal("dip", "Sharp drop",
                               f"Down {abs(runup):.1f}% in the 5 trading days before{vs_market} - {abs(z):.1f}x a normal week's move", 0))
    else:
        if z >= RUNUP_MILD_Z:
            strong = z >= RUNUP_STRONG_Z
            signals.append(_signal("runup", "Unusual run-up",
                                   f"Up {runup:.1f}% in 5 trading days{vs_market} - {z:.1f}x a normal week for this stock",
                                   POINTS["runup_strong" if strong else "runup_mild"]))

        high = max(c for _, c in before[-HIGH_SESSIONS:])
        gap = (high - entry) / high * 100
        if gap <= NEAR_HIGH_PCT and z >= NEAR_HIGH_MIN_Z:
            detail = "At a new 20-day high" if gap <= 0 else f"Within {gap:.1f}% of its 20-day high"
            signals.append(_signal("near_high", "Bought near the top", detail, POINTS["near_high"]))

        day_volume = volumes.get(trade_date) if volumes else None
        usual = [volumes[d] for d, _ in before[-VOLUME_SESSIONS:] if volumes and volumes.get(d)]
        if day_volume and len(usual) >= VOLUME_SESSIONS // 2 and z > 0:
            ratio = day_volume / statistics.mean(usual)
            if ratio >= VOLUME_SPIKE:
                signals.append(_signal("volume", "Volume spike", f"{ratio:.1f}x its normal trading volume that day", POINTS["volume"]))

        jumps = [before[-1][1] / before[-2][1] - 1, entry / before[-1][1] - 1]
        biggest = max(jumps)
        if biggest > 0 and biggest >= JUMP_SIGMAS * sigma:
            signals.append(_signal("jump", "One-day jump",
                                   f"Jumped {biggest * 100:.1f}% in a single day - {biggest / sigma:.0f}x a normal day (news or earnings?)",
                                   POINTS["jump"]))

    if trade_time and OPENING_START <= trade_time < OPENING_END:
        signals.append(_signal("opening", "Opening-bell buy",
                               f"Bought at {_clock(trade_time)}, in the first half hour of trading", POINTS["opening"]))

    value = float(quantity or 0) * float(price or entry)
    if value > 0 and len(habits.prior_buy_values) >= MIN_PRIOR_BUYS:
        usual_size = statistics.median(habits.prior_buy_values)
        if usual_size > 0 and value / usual_size >= OVERSIZED_RATIO:
            signals.append(_signal("oversized", "Bigger than usual",
                                   f"${value:,.0f} - {value / usual_size:.1f}x your usual buy", POINTS["oversized"]))

    if habits.prior_trade_count >= NEW_NAME_MIN_TRADES and not habits.traded_before and not habits.on_watchlist:
        signals.append(_signal("new_name", "New name",
                               "First time you've traded it, and it wasn't on your watchlist", POINTS["new_name"]))

    if habits.recent_sell and entry > habits.recent_sell[1]:
        sold_on, sold_at = habits.recent_sell
        days = (trade_date - sold_on).days
        signals.append(_signal("chase_back", "Buying back higher",
                               f"You sold at ${sold_at:,.2f} {days} day{'s' if days != 1 else ''} before and bought back "
                               f"{_pct(sold_at, entry):.1f}% higher", POINTS["chase_back"]))

    score = min(100, sum(s["points"] for s in signals))
    band = "dip" if dip else "chasing" if score >= CHASING_SCORE else "warm" if score >= WARM_SCORE else "calm"
    return {
        "score": score,
        "band": band,
        "signals": signals,
        "stats": {"runup_pct": round(runup, 2), "market_pct": round(market, 2) if market is not None else None,
                  "excess_pct": round(excess, 2), "normal_week_pct": round(normal_week, 2), "z": round(z, 2)},
        "summary": _summary(band, signals),
    }


def _summary(band, signals):
    scored = sorted((s for s in signals if s["points"]), key=lambda s: -s["points"])
    if band == "dip":
        return "Dip buy: " + signals[0]["detail"][0].lower() + signals[0]["detail"][1:] + "."
    if band == "calm":
        return "Calm entry: no unusual move or rush before this buy." if not scored \
            else f"Calm entry (only: {scored[0]['label'].lower()})."
    lead = "Chasing" if band == "chasing" else "A bit warm"
    return f"{lead}: " + "; ".join(s["detail"] for s in scored[:2]) + "."


def week_later_pct(closes, trade_date, entry):
    """Patience test: % move from the entry to the close 5 sessions later
    (negative = it was cheaper a week later). None until that close exists."""
    after = [c for c in closes if c[0] > trade_date]
    if len(after) < WEEK_SESSIONS or not entry:
        return None
    return round(_pct(entry, after[WEEK_SESSIONS - 1][1]), 2)


def cool_off(closes, volumes, bench_closes, trade_date, entry):
    """What waiting would have done for a hot buy: re-runs the market half
    of the check on each of the next 10 sessions (as if buying at that
    day's close). The first one that comes out calm (or a dip) is when the
    stock "cooled off"; if none does, the 10th session is used.

    change_pct < 0 means buying then would have been cheaper. None until 10
    sessions after the buy exist."""
    after = [c for c in closes if c[0] > trade_date]
    if len(after) < COOL_OFF_SESSIONS or not entry:
        return None
    window = after[:COOL_OFF_SESSIONS]
    cooled = None
    for n, (day, _) in enumerate(window, start=1):
        result = score_buy(trade_date=day, trade_time=None, price=None, quantity=None,
                           closes=closes, volumes=volumes, bench_closes=bench_closes)
        if result["band"] in ("calm", "dip"):
            cooled = (n, day)
            break
    sessions, day = cooled or (COOL_OFF_SESSIONS, window[-1][0])
    price = dict(window)[day]
    low_day, low = min(window, key=lambda c: c[1])
    return {
        "cooled": cooled is not None,
        "sessions": sessions,
        "date": day.isoformat(),
        "price": round(price, 4),
        "change_pct": round(_pct(entry, price), 2),
        "low_date": low_day.isoformat(),
        "low_price": round(low, 4),
        "low_pct": round(_pct(entry, low), 2),
    }


def waiting_saved(trade: Trade, cool: dict):
    """Dollars buying at the cool-off price would have saved (negative =
    waiting would have cost). Measured in %, so a later split doesn't matter."""
    value = float(trade.quantity) * float(trade.price_per_share)
    return round(-value * cool["change_pct"] / 100, 2)


# --- The user's habits -----------------------------------------------------------------

def habits_for(ticker, trade_date, trade_id, trades, watchlist) -> Habits:
    """Habits from the trades made BEFORE this one (same day counts when
    it's a preview with no id yet, or an earlier id)."""
    def is_prior(t):
        if t.id == trade_id:
            return False
        if t.trade_date < trade_date:
            return True
        return t.trade_date == trade_date and (trade_id is None or t.id < trade_id)

    prior = [t for t in trades if is_prior(t)]
    sells = [t for t in prior if t.ticker == ticker and t.action == "sell"
             and (trade_date - t.trade_date).days <= CHASE_BACK_DAYS]
    latest_sell = max(sells, key=lambda t: (t.trade_date, t.id), default=None)
    return Habits(
        prior_buy_values=[float(t.quantity) * float(t.price_per_share) for t in prior if t.action == "buy"],
        prior_trade_count=len(prior),
        traded_before=any(t.ticker == ticker for t in prior),
        on_watchlist=ticker in watchlist,
        recent_sell=(latest_sell.trade_date, float(latest_sell.price_per_share)) if latest_sell else None,
    )


# --- Prices -----------------------------------------------------------------------------

def market_data(ticker, trade_date, today):
    """(closes, volumes, bench_closes) around a trade. One cached download
    per ticker (grading._fetch_bars serves both closes and volume)."""
    start = trade_date - timedelta(days=HISTORY_DAYS)
    end = min(today, trade_date + timedelta(days=COOL_OFF_DAYS + 5))
    closes = grading.fetch_daily_closes(ticker, start, end)
    volumes = grading.fetch_daily_volumes(ticker, start, end) if closes else {}
    benchmark = grading.benchmark_for(ticker)
    bench = grading.fetch_daily_closes(benchmark[0], start, end) if benchmark and closes else []
    return closes, volumes, bench


# --- Stored results ----------------------------------------------------------------------

def stored_result(trade: Trade):
    """The saved FOMO check for a buy, shaped like score_buy's result. None
    for sells or before it has been run."""
    if trade.action != "buy" or not trade.fomo_checked_at:
        return None
    saved = _saved(trade)
    cool = saved.get("cool_off")
    return {
        "score": trade.fomo_score,
        "band": trade.fomo_band,
        "signals": saved.get("signals", []),
        "stats": saved.get("stats"),
        "summary": saved.get("summary"),
        "week_later_pct": trade.week_later_pct,
        "cool_off": {**cool, "saved": waiting_saved(trade, cool)} if cool else None,
        "self_tagged": "fomo" in thesis.parse_tags(trade),
    }


def _saved(trade: Trade) -> dict:
    return json.loads(trade.fomo_signals) if trade.fomo_signals else {}


def is_hot(t: Trade) -> bool:
    """Chasing or warm - or admitted FOMO, whatever the score said."""
    return t.fomo_band in HOT_BANDS or "fomo" in thesis.parse_tags(t)


def _needs_check(t: Trade, today: date, now: datetime) -> bool:
    if t.action != "buy":
        return False
    if not t.fomo_checked_at:
        return True
    if t.fomo_band == "unknown":
        return now - t.fomo_checked_at > RESCORE_UNKNOWN_AFTER
    checked_on = t.fomo_checked_at.date()
    week_due = t.trade_date + timedelta(days=WEEK_LATER_DAYS)
    # Scored before a week had passed: run once more to fill the patience test.
    if t.week_later_pct is None and today >= week_due and checked_on < week_due:
        return True
    # Hot buys get the cool-off once two weeks of closes exist. Checks made
    # before this existed (or that failed) are retried at most once an hour.
    cool_due = t.trade_date + timedelta(days=COOL_OFF_DAYS)
    if is_hot(t) and today >= cool_due and "cool_off" not in _saved(t):
        return checked_on < cool_due or now - t.fomo_checked_at > RESCORE_UNKNOWN_AFTER
    return False


def ensure_scored(db: Session, user: User, trades=None, today: date = None, limit: int = MAX_SCORE_PER_REQUEST):
    """Runs (or re-runs) the FOMO check for the user's buys that need it,
    newest first, max `limit` per call. Price downloads run in parallel;
    a failure leaves that trade for next time. Never raises."""
    today = today or date.today()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    everything = db.query(Trade).filter(Trade.user_id == user.id).all()
    pool_of = trades if trades is not None else everything
    todo = sorted((t for t in pool_of if _needs_check(t, today, now)),
                  key=lambda t: (t.trade_date, t.id), reverse=True)[:limit]
    if not todo:
        return 0

    watchlist = {w.ticker for w in db.query(Watchlist).filter(Watchlist.user_id == user.id).all()}
    jobs = [{
        "id": t.id, "ticker": t.ticker, "trade_date": t.trade_date, "trade_time": t.trade_time,
        "price": float(t.price_per_share), "quantity": float(t.quantity),
        "habits": habits_for(t.ticker, t.trade_date, t.id, everything, watchlist),
        "self_tagged": "fomo" in thesis.parse_tags(t),
    } for t in todo]

    def run(job):
        try:
            closes, volumes, bench = market_data(job["ticker"], job["trade_date"], today)
            if not closes:
                # No prices at all = the download failed (Yahoo often refuses
                # cloud servers for a while). That's not "not enough history":
                # leave the buy unchecked so the next request tries again.
                print(f"[fomo] no prices for {job['ticker']} - will retry")
                return None
            result = score_buy(trade_date=job["trade_date"], trade_time=job["trade_time"], price=job["price"],
                               quantity=job["quantity"], closes=closes, volumes=volumes,
                               bench_closes=bench, habits=job["habits"])
            entry = entry_price(closes, job["trade_date"], job["price"]) if closes else None
            hot = result["band"] in HOT_BANDS or job["self_tagged"]
            cool = cool_off(closes, volumes, bench, job["trade_date"], entry) if hot else None
            return result, week_later_pct(closes, job["trade_date"], entry), cool
        except Exception as e:  # one bad ticker shouldn't stop the rest
            print(f"[fomo] check failed for {job['ticker']} on {job['trade_date']}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs))) as pool:
        outcomes = list(pool.map(run, jobs))

    by_id = {t.id: t for t in todo}
    for job, outcome in zip(jobs, outcomes):
        if not outcome:
            continue
        result, week_later, cool = outcome
        t = by_id[job["id"]]
        t.fomo_score = result["score"]
        t.fomo_band = result["band"]
        blob = {"signals": result["signals"], "stats": result["stats"], "summary": result["summary"]}
        if cool:
            blob["cool_off"] = cool
        t.fomo_signals = json.dumps(blob)
        t.fomo_checked_at = now
        t.week_later_pct = week_later
    db.commit()
    return len(jobs)


# --- The personal record ------------------------------------------------------------------

GROUPS = ("chasing", "warm", "calm", "dip")


def group_of(t: Trade):
    """Which bucket a scored buy counts in. Admitting FOMO beats the score."""
    if "fomo" in thesis.parse_tags(t):
        return "chasing"
    return t.fomo_band if t.fomo_band in GROUPS else None


def _excess(t: Trade):
    if t.graded_return_pct is None or t.graded_benchmark_return_pct is None:
        return None
    return t.graded_return_pct - t.graded_benchmark_return_pct


def _population_chasing(db: Session, user: User) -> list:
    """Graded chasing buys of everyone else (real accounts only), as excess %."""
    others = (
        db.query(Trade).join(User, User.id == Trade.user_id)
        .filter(Trade.user_id != user.id, User.is_demo.is_(False), Trade.action == "buy",
                Trade.graded_return_pct.isnot(None), Trade.graded_benchmark_return_pct.isnot(None))
        .all()
    )
    return [_excess(t) for t in others if group_of(t) == "chasing"]


def personal_record(db: Session, user: User) -> dict:
    """How the user's buys did, split by how they bought: the FOMO tax, the
    patience test, the detector vs what they admitted, and a shrunk
    estimate of what a chasing buy costs THEM."""
    buys = db.query(Trade).filter(Trade.user_id == user.id, Trade.action == "buy").all()
    groups = {}
    for name in GROUPS:
        members = [t for t in buys if group_of(t) == name]
        graded = [e for e in (_excess(t) for t in members) if e is not None]
        weeks = [t.week_later_pct for t in members if t.week_later_pct is not None]
        correct = [t for t in members if t.outcome_tag]
        groups[name] = {
            "count": len(members),
            "graded": len(graded),
            "avg_vs_market_pct": round(statistics.mean(graded), 2) if graded else None,
            "correct_rate": round(sum(t.outcome_tag == "correct" for t in correct) / len(correct) * 100, 1) if correct else None,
            "week_checked": len(weeks),
            "cheaper_week_later": sum(w < 0 for w in weeks),
            "avg_week_later_pct": round(statistics.mean(weeks), 2) if weeks else None,
        }

    # FOMO tax: dollars won/lost vs the S&P 500 on graded chasing buys
    # (negative = it cost money). Mixes CAD and USD listings as-is.
    chasing = [t for t in buys if group_of(t) == "chasing" and _excess(t) is not None]
    tax = sum(float(t.quantity) * float(t.price_per_share) * _excess(t) / 100 for t in chasing)

    # Shrinkage: blend the user's average with everyone else's.
    own = [_excess(t) for t in chasing]
    population = _population_chasing(db, user)
    prior = statistics.mean(population) if len(population) >= MIN_POPULATION else 0.0
    expected = (sum(own) + SHRINK_K * prior) / (len(own) + SHRINK_K) if own or population else None

    # Cool-off: what waiting until each hot buy calmed down would have done.
    cooled = [(t, _saved(t)["cool_off"]) for t in buys if group_of(t) in HOT_BANDS and _saved(t).get("cool_off")]
    waiting = {
        "checked": len(cooled),
        "cheaper": sum(c["change_pct"] < 0 for _, c in cooled),
        "saved": round(sum(waiting_saved(t, c) for t, c in cooled), 2) if cooled else None,
        "avg_change_pct": round(statistics.mean(c["change_pct"] for _, c in cooled), 2) if cooled else None,
        "avg_sessions": round(statistics.mean(c["sessions"] for _, c in cooled), 1) if cooled else None,
    }

    tagged = [t for t in buys if "fomo" in thesis.parse_tags(t) and t.fomo_band and t.fomo_band != "unknown"]
    return {
        "buys": len(buys),
        "unchecked": sum(1 for t in buys if not t.fomo_band or t.fomo_band == "unknown"),
        "waiting": waiting,
        "waits": waits_summary(db, user),
        "groups": groups,
        "scored_buys": sum(1 for t in buys if t.fomo_band and t.fomo_band != "unknown"),
        "fomo_tax": round(tax, 2) if chasing else None,
        "fomo_tax_trades": len(chasing),
        "expected_vs_market_pct": round(expected, 2) if expected is not None else None,
        "expected_based_on": {"yours": len(own), "others": len(population) if len(population) >= MIN_POPULATION else 0},
        "self_tagged": len(tagged),
        "self_tagged_caught": sum(t.fomo_band in ("chasing", "warm") for t in tagged),
    }


def record_message(record: dict) -> str:
    """One sentence for the check card: what chasing has meant for this user."""
    chasing = record["groups"]["chasing"]
    parts = []
    if chasing["graded"]:
        parts.append(f"Your {chasing['graded']} graded chasing buy{'s' if chasing['graded'] != 1 else ''} "
                     f"averaged {chasing['avg_vs_market_pct']:+.1f} points vs the S&P 500")
    if chasing["week_checked"]:
        parts.append(f"{chasing['cheaper_week_later']} of {chasing['week_checked']} were cheaper a week later")
    waiting = record["waiting"]
    if waiting["checked"]:
        saved = waiting["saved"]
        parts.append(f"waiting until it cooled off would have {'saved' if saved >= 0 else 'cost'} you "
                     f"${abs(saved):,.0f} across {waiting['checked']} hot buy{'s' if waiting['checked'] != 1 else ''}")
    if parts:
        text = "; ".join(parts) + "."
        return text[0].upper() + text[1:]
    if record["expected_based_on"]["others"]:
        return f"Across InnerStock, chasing buys have averaged {record['expected_vs_market_pct']:+.1f} points vs the S&P 500."
    return "No graded chasing buys yet - this check gets personal as your record grows."


def advisor_context_section(db: Session, user: User) -> str:
    """The FOMO lines in the advisor's facts block (stored data only)."""
    record = personal_record(db, user)
    if not record["scored_buys"] and not record["self_tagged"]:
        return "FOMO check: no buys checked yet."
    lines = [f"FOMO check on {record['scored_buys']} buys (chasing = bought after an unusual run-up/volume spike or "
             "in a rush; dip = bought after a sharp drop):"]
    for name in GROUPS:
        g = record["groups"][name]
        if g["count"]:
            lines.append(f"- {name}: {g['count']} buys, {g['graded']} graded, avg vs S&P 500 "
                         f"{g['avg_vs_market_pct'] if g['avg_vs_market_pct'] is not None else 'n/a'} points, "
                         f"cheaper a week later {g['cheaper_week_later']}/{g['week_checked']}")
    if record["fomo_tax"] is not None:
        lines.append(f"- FOMO tax (graded chasing buys vs S&P 500): ${record['fomo_tax']:,.0f}")
    if record["self_tagged"]:
        lines.append(f"- User admitted FOMO on {record['self_tagged']} buys; the detector flagged {record['self_tagged_caught']} of them")
    waiting = record["waiting"]
    if waiting["checked"]:
        lines.append(f"- Cool-off on {waiting['checked']} hot buys: buying when the stock next scored calm "
                     f"(avg {waiting['avg_sessions']} sessions later) would have been {waiting['avg_change_pct']:+.1f}% "
                     f"on average, ${waiting['saved']:,.0f} saved in total (negative = waiting would have cost); "
                     f"cheaper {waiting['cheaper']}/{waiting['checked']} times")
    waits = record["waits"]
    if waits["resolved"]:
        lines.append(f"- 'Wait 3 days' used {waits['resolved']} times instead of buying a flagged stock: "
                     f"cheaper 3 sessions later {waits['cheaper']}/{waits['resolved']} times, avg {waits['avg_change_pct']:+.1f}%")
    if waits["waiting"]:
        lines.append(f"- Currently waiting on: {', '.join(waits['waiting_tickers'])}")
    return "\n".join(lines)


# --- Checking a buy before (or as) it happens ------------------------------------------------

def check_entry(db: Session, user: User, ticker: str, trade_date: date = None, trade_time: time = None,
                price: float = None, quantity: float = None) -> dict:
    """The FOMO check for a buy that isn't saved (yet): "am I chasing this?"
    Defaults to buying right now at the latest price."""
    ticker = (ticker or "").strip().upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="That isn't a valid ticker.")
    now_et = datetime.now(MARKET_TZ)
    today = now_et.date()
    trade_date = trade_date or today
    if trade_date > today:
        raise HTTPException(status_code=400, detail="That date is in the future.")
    if trade_time is None and trade_date == today:
        trade_time = now_et.time().replace(second=0, microsecond=0)

    trades = db.query(Trade).filter(Trade.user_id == user.id).all()
    watchlist = {w.ticker for w in db.query(Watchlist).filter(Watchlist.user_id == user.id).all()}
    closes, volumes, bench = market_data(ticker, trade_date, today)
    if not closes:
        raise HTTPException(status_code=404, detail=f"No price history for {ticker}.")
    if not price:
        last = _close_on_or_before(closes, trade_date)
        price = last[1] if last else None
    result = score_buy(trade_date=trade_date, trade_time=trade_time, price=price, quantity=quantity,
                       closes=closes, volumes=volumes, bench_closes=bench,
                       habits=habits_for(ticker, trade_date, None, trades, watchlist))
    record = personal_record(db, user)
    return {"ticker": ticker, "trade_date": trade_date.isoformat(), "price": round(price, 4) if price else None,
            **result, "record_message": record_message(record), "record": record}


class EntryCheck(BaseModel):
    ticker: str = Field(min_length=1, max_length=15)
    trade_date: Optional[date] = None
    trade_time: Optional[time] = None
    price: Optional[float] = Field(default=None, gt=0)
    quantity: Optional[float] = Field(default=None, gt=0)
    dollars: Optional[float] = Field(default=None, gt=0)  # instead of quantity


def run_check(db: Session, user: User, payload: EntryCheck) -> dict:
    """POST /fomo/check's logic, shared with "Wait 3 days"."""
    quantity = payload.quantity
    if quantity is None and payload.dollars and payload.price:
        quantity = payload.dollars / payload.price
    result = check_entry(db, user, payload.ticker, payload.trade_date, payload.trade_time,
                         payload.price, quantity)
    if quantity is None and payload.dollars and result["price"]:
        # No price given: size it at the latest price and re-check (cheap - cached).
        quantity = payload.dollars / result["price"]
        result = check_entry(db, user, payload.ticker, payload.trade_date, payload.trade_time,
                             result["price"], quantity)
    return {**result, "quantity": round(quantity, 6) if quantity else None}


@router.post("/fomo/check")
def post_check(payload: EntryCheck, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return run_check(db, current_user, payload)


LEDGER_SIZE = 6


@router.get("/fomo/summary")
def get_summary(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The dashboard card: the personal record, the latest flagged buys,
    what waiting would have done on each, and the user's "Wait 3 days"."""
    ensure_scored(db, current_user)
    waits = list_waits(db, current_user)  # resolves due waits first, so the record counts them
    record = personal_record(db, current_user)
    flagged = (
        db.query(Trade)
        .filter(Trade.user_id == current_user.id, Trade.action == "buy", Trade.fomo_checked_at.isnot(None))
        .order_by(Trade.trade_date.desc(), Trade.id.desc())
        .all()
    )
    recent = [t for t in flagged if group_of(t) == "chasing"][:5]
    ledger = [t for t in flagged if group_of(t) in HOT_BANDS and _saved(t).get("cool_off")][:LEDGER_SIZE]
    return {
        "record": record,
        "message": record_message(record),
        "recent_chasing": [{
            "id": t.id, "ticker": t.ticker, "trade_date": t.trade_date.isoformat(),
            "score": t.fomo_score, "summary": stored_result(t)["summary"],
            "self_tagged": "fomo" in thesis.parse_tags(t), "week_later_pct": t.week_later_pct,
            "vs_market_pct": round(_excess(t), 2) if _excess(t) is not None else None,
        } for t in recent],
        # NEW: "if you'd waited until it cooled off", newest hot buys first.
        "waiting_ledger": [{
            "id": t.id, "ticker": t.ticker, "trade_date": t.trade_date.isoformat(),
            "band": t.fomo_band, "score": t.fomo_score, "self_tagged": "fomo" in thesis.parse_tags(t),
            "price": float(t.price_per_share), "quantity": float(t.quantity),
            "cool_off": stored_result(t)["cool_off"],
        } for t in ledger],
        "waits": waits,
    }


# --- "Wait 3 days" ---------------------------------------------------------------------------
# NEW: instead of buying a flagged stock, the user waits. The price is
# saved now; once 3 more sessions have closed, the third close is the
# answer: was it cheaper? It shows up in the notifications (bell) and on
# the dashboard card, and counts in the personal record (record["waits"]).

def add_sessions(day: date, n: int) -> date:
    """n weekdays after `day` (holidays aren't known - the answer simply
    arrives a day later if one falls in between)."""
    while n:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def _wait_numbers(w: FomoWait):
    """(change_pct, saved_dollars) once answered; saved > 0 = waiting saved money."""
    if w.result_price is None:
        return None, None
    change = _pct(w.price, w.result_price)
    saved = (w.price - w.result_price) * w.quantity if w.quantity else None
    return round(change, 2), round(saved, 2) if saved is not None else None


def wait_message(w: FomoWait) -> str:
    change, saved = _wait_numbers(w)
    when = w.check_date.strftime("%b %d").replace(" 0", " ")
    if change is None:
        return (f"Waiting on {w.ticker} (it was ${w.price:,.2f} on {when}). The answer comes after the close on "
                f"{w.remind_on.strftime('%a %b %d').replace(' 0', ' ')}.")
    later = f"{WAIT_SESSIONS} trading days later it closed at ${w.result_price:,.2f} ({change:+.1f}%)"
    per_share = abs(w.price - w.result_price)
    if abs(change) < NO_REAL_DIFFERENCE_PCT:
        verdict = "about the same - no real difference either way"
    elif change < 0:
        verdict = f"waiting saved you ${saved:,.0f}" if saved is not None else f"waiting saved ${per_share:,.2f} a share"
    else:
        verdict = (f"waiting cost ${abs(saved):,.0f} this time" if saved is not None
                   else f"waiting cost ${per_share:,.2f} a share this time")
    return f"You almost bought {w.ticker} at ${w.price:,.2f} on {when}. {later}: {verdict}."


def serialize_wait(w: FomoWait) -> dict:
    change, saved = _wait_numbers(w)
    return {
        "id": w.id, "ticker": w.ticker, "price": w.price, "quantity": w.quantity,
        "dollars": round(w.price * w.quantity, 2) if w.quantity else None,
        "score": w.score, "band": w.band, "summary": w.summary,
        "check_date": w.check_date.isoformat(), "remind_on": w.remind_on.isoformat(),
        "status": "waiting" if w.result_price is None else "answered",
        "result_date": w.result_date.isoformat() if w.result_date else None,
        "result_price": w.result_price, "change_pct": change, "saved": saved,
        # paid_off / cost / same: how the answer reads (a tiny move is "same")
        "verdict": None if change is None else "same" if abs(change) < NO_REAL_DIFFERENCE_PCT
                   else "paid_off" if change < 0 else "cost",
        "unread": w.result_price is not None and w.seen_at is None,
        "message": wait_message(w),
    }


def resolve_waits(db: Session, user: User, today: date = None) -> int:
    """Fills in the answer for waits whose 3 sessions have closed. Never raises."""
    today = today or datetime.now(MARKET_TZ).date()
    due = (db.query(FomoWait)
           .filter(FomoWait.user_id == user.id, FomoWait.result_price.is_(None), FomoWait.remind_on <= today)
           .order_by(FomoWait.remind_on).limit(MAX_RESOLVE_PER_REQUEST).all())
    if not due:
        return 0

    def answer(w):
        try:
            closes = grading.fetch_daily_closes(w.ticker, w.check_date - timedelta(days=5), today)
            after = [c for c in closes if c[0] > w.check_date]
            return after[WAIT_SESSIONS - 1] if len(after) >= WAIT_SESSIONS else None
        except Exception as e:
            print(f"[fomo] wait answer failed for {w.ticker}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(due))) as pool:
        answers = list(pool.map(answer, due))
    filled = 0
    for w, found in zip(due, answers):
        if found:
            w.result_date, w.result_price = found[0], round(found[1], 4)
            filled += 1
    if filled:
        db.commit()
    return filled


def waits_summary(db: Session, user: User) -> dict:
    waits = db.query(FomoWait).filter(FomoWait.user_id == user.id).all()
    answered = [w for w in waits if w.result_price is not None]
    numbers = [_wait_numbers(w) for w in answered]
    saved = [s for _, s in numbers if s is not None]
    return {
        "resolved": len(answered),
        "cheaper": sum(c < 0 for c, _ in numbers),
        "avg_change_pct": round(statistics.mean(c for c, _ in numbers), 2) if numbers else None,
        "saved": round(sum(saved), 2) if saved else None,
        "saved_on": len(saved),
        "waiting": len(waits) - len(answered),
        "waiting_tickers": sorted({w.ticker for w in waits if w.result_price is None}),
    }


def list_waits(db: Session, user: User) -> dict:
    resolve_waits(db, user)
    rows = (db.query(FomoWait).filter(FomoWait.user_id == user.id)
            .order_by(FomoWait.check_date.desc(), FomoWait.id.desc()).limit(WAITS_LISTED).all())
    return {"waits": [serialize_wait(w) for w in rows], "summary": waits_summary(db, user)}


def answered_waits_for_notifications(db: Session, user: User):
    """(answered waits newest first, how many are unseen) for GET /notifications."""
    resolve_waits(db, user)
    rows = (db.query(FomoWait)
            .filter(FomoWait.user_id == user.id, FomoWait.result_price.isnot(None))
            .order_by(FomoWait.result_date.desc(), FomoWait.id.desc()).limit(WAITS_LISTED).all())
    return [serialize_wait(w) for w in rows], sum(w.seen_at is None for w in rows)


def mark_waits_seen(db: Session, user: User):
    db.query(FomoWait).filter(
        FomoWait.user_id == user.id, FomoWait.result_price.isnot(None), FomoWait.seen_at.is_(None)
    ).update({FomoWait.seen_at: datetime.utcnow()}, synchronize_session=False)


class WaitCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=15)
    price: Optional[float] = Field(default=None, gt=0)      # default: the latest price
    quantity: Optional[float] = Field(default=None, gt=0)
    dollars: Optional[float] = Field(default=None, gt=0)    # instead of quantity


@router.post("/fomo/waits", status_code=201)
def create_wait(payload: WaitCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    ticker = payload.ticker.strip().upper()
    open_waits = db.query(FomoWait).filter(FomoWait.user_id == current_user.id, FomoWait.result_price.is_(None)).all()
    same = next((w for w in open_waits if w.ticker == ticker), None)
    if same:
        raise HTTPException(status_code=409, detail=f"You're already waiting on {ticker} - "
                            f"the answer comes after the close on {same.remind_on.strftime('%a %b %d').replace(' 0', ' ')}.")
    if len(open_waits) >= MAX_OPEN_WAITS:
        raise HTTPException(status_code=429, detail="You have too many waits open - let some finish first.")

    # Always "buying right now": the check is re-run here, so what's stored
    # is the server's own score, not whatever the page sent.
    check = run_check(db, current_user, EntryCheck(ticker=ticker, price=payload.price,
                                                     quantity=payload.quantity, dollars=payload.dollars))
    if check["band"] == "unknown" or not check["price"]:
        raise HTTPException(status_code=400, detail="Not enough price history to check this one.")
    check_date = date.fromisoformat(check["trade_date"])
    wait = FomoWait(
        user_id=current_user.id, ticker=check["ticker"], price=float(check["price"]), quantity=check["quantity"],
        score=check["score"], band=check["band"], summary=check["summary"],
        check_date=check_date, remind_on=add_sessions(check_date, WAIT_SESSIONS),
    )
    db.add(wait)
    db.commit()
    db.refresh(wait)
    return serialize_wait(wait)


@router.get("/fomo/waits")
def get_waits(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return list_waits(db, current_user)


@router.delete("/fomo/waits/{wait_id}")
def delete_wait(wait_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    wait = db.query(FomoWait).filter(FomoWait.id == wait_id, FomoWait.user_id == current_user.id).first()
    if not wait:
        raise HTTPException(status_code=404, detail="Wait not found")
    db.delete(wait)
    db.commit()
    return {"success": True}
