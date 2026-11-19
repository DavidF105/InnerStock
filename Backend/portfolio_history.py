# NEW: the dashboard's big portfolio chart (GET /portfolio/history).
#
# The app never stored "what was my portfolio worth on March 3rd" - it only
# has trades. So the history is rebuilt from them: walk every trading day
# from the first trade to today, keep a running share count per stock
# (buys add, sells subtract), and value it at that day's close.
#
# Three lines per day:
#   value     what the holdings were worth at that day's closes
#   invested  money put in so far: buys minus what sells brought back
#             (value - invested = gain, in dollars)
#   sp500     the same deposits and withdrawals made into the S&P 500
#             instead (SPY for US stocks, VFV.TO for Canadian ones - the
#             same benchmarks grading uses): "what if I'd just bought the
#             index every time I bought a stock?"
#
# Like /holdings, CAD and USD amounts are added as-is (no currency
# conversion), so a mixed portfolio's total is approximate.
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import grading
from deps import get_current_user, get_db
from models import Trade, User

router = APIRouter(tags=["portfolio"])

LOOKBACK_DAYS = 10   # start price history a little before the first trade (weekends, holidays)
MAX_WORKERS = 8


def _bench_key(ticker: str) -> str:
    """Where this stock's money would have gone in the S&P 500 version.
    An S&P 500 fund is its own benchmark."""
    bench = grading.benchmark_for(ticker)
    return bench[0] if bench else ticker


def build_series(trades: list, closes: dict) -> list:
    """The day-by-day portfolio. Pure (no database, no network).

    trades: [(date, ticker, action, quantity, price)] oldest first.
    closes: {ticker: [(date, close)] oldest first} for every traded ticker
            and every benchmark key.
    Returns [{date, value, invested, sp500}] - one per trading day from the
    first trade's day. sp500 is None if a benchmark's prices are missing.
    """
    if not trades:
        return []
    first_day = trades[0][0]
    calendar = sorted({d for series in closes.values() for d, _ in series if d >= first_day})
    if not calendar or calendar[0] > first_day:
        calendar = sorted(set(calendar) | {first_day})
    by_day = {ticker: dict(series) for ticker, series in closes.items()}

    def price_on(ticker, day):
        """Last close on or before `day` (None if there's no history yet)."""
        return grading.close_on_or_before(closes.get(ticker) or [], day)

    shares, last_price = {}, {}
    shadow = {}            # benchmark units held in the S&P 500 version
    invested = 0.0
    shadow_ok = True
    points, i = [], 0
    for day in calendar:
        # Apply every trade made on or before this day.
        while i < len(trades) and trades[i][0] <= day:
            trade_day, ticker, action, qty, price = trades[i]
            sign = 1 if action == "buy" else -1
            shares[ticker] = shares.get(ticker, 0.0) + sign * qty
            last_price.setdefault(ticker, price)  # until its first close shows up
            invested += sign * qty * price
            key = _bench_key(ticker)
            if key == ticker:
                bench_price = price
            else:
                found = price_on(key, trade_day)
                bench_price = found[1] if found else None
            if bench_price:
                last_price.setdefault(key, bench_price)
                # Same cash in (or out) of the index. Never below zero: a big
                # winner's sale can bring back more than the index version holds.
                shadow[key] = max(0.0, shadow.get(key, 0.0) + sign * qty * price / bench_price)
            else:
                shadow_ok = False
            i += 1

        for ticker in list(last_price) + list(shadow):
            if day in by_day.get(ticker, {}):
                last_price[ticker] = by_day[ticker][day]
        value = sum(q * last_price.get(t, 0.0) for t, q in shares.items() if abs(q) > 1e-9)
        sp500 = sum(units * last_price[key] for key, units in shadow.items() if key in last_price) if shadow_ok else None
        points.append({
            "date": day.isoformat(),
            "value": round(value, 2),
            "invested": round(invested, 2),
            "sp500": round(sp500, 2) if sp500 is not None else None,
        })
    return points


@router.get("/portfolio/history")
def get_history(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (db.query(Trade).filter(Trade.user_id == current_user.id)
            .order_by(Trade.trade_date, Trade.id).all())
    if not rows:
        return {"points": [], "mixed_currency": False}
    trades = [(t.trade_date, t.ticker.upper(), t.action, float(t.quantity), float(t.price_per_share)) for t in rows]

    tickers = sorted({t[1] for t in trades} | {_bench_key(t[1]) for t in trades})
    start, today = trades[0][0] - timedelta(days=LOOKBACK_DAYS), date.today()
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(tickers))) as pool:
        histories = list(pool.map(lambda t: grading.fetch_daily_closes(t, start, today), tickers))
    closes = dict(zip(tickers, histories))

    canadian = {t[1].endswith(grading.CANADIAN_SUFFIXES) for t in trades}
    return {
        "points": build_series(trades, closes),
        "benchmark_label": "S&P 500",
        "mixed_currency": len(canadian) == 2,
    }
