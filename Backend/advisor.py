# The advisor chat (POST /chat) and its suggested questions (GET /chat/suggestions).
#
# What makes this different from a brokerage app's chatbot: InnerStock
# knows WHY the user made each trade (thesis, reasons, conviction, review
# date) and how those calls turned out against the S&P 500. The advisor's
# job is to connect the market to that record.
#
# How it works:
#   - Every message starts from a snapshot of the user's situation
#     (main.build_advisor_context: profile, holdings, fundamentals, recent
#     trades, reasons stats, open items).
#   - Claude can then call read-only TOOLS to look up anything the question
#     needs - any ticker's quote/fundamentals, performance vs the S&P 500,
#     news, earnings (including our recaps), the user's own trades and
#     review scorecards, the portfolio's shape, and "what if I bought X"
#     simulations. Tools only ever read the current user's own data.
#   - The loop runs at most MAX_TOOL_ROUNDS rounds of lookups, then Claude
#     must answer. Only the final answer is stored in chat history.
#
# A manual tool loop (not the SDK's beta tool runner) because each tool
# needs this request's DB session and user, and the round cap is enforced
# here.
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import fomo
import grading
import holdings_news as hn
import thesis
from deps import get_current_user, get_db
from models import ChatMessage, EarningsNotification, EarningsRecap, Trade, User, Watchlist

router = APIRouter(tags=["advisor"])

# Per-million-token pricing for ADVISOR_MODEL, for the cost line printed
# after each message. Update all of these if the model changes. Cache
# writes cost 1.25x input, cache reads 0.05x.
ADVISOR_MODEL = "claude-opus-5-5"
CLAUDE_OPUS_5_5_INPUT_COST_PER_MTOK = 4.00
CLAUDE_OPUS_5_5_OUTPUT_COST_PER_MTOK = 20.00
CLAUDE_OPUS_5_5_CACHE_WRITE_COST_PER_MTOK = 5.00
CLAUDE_OPUS_5_5_CACHE_READ_COST_PER_MTOK = 0.20

# How many stored messages go back to Claude each time (the NEWEST ones -
# see recent_history).
HISTORY_MESSAGES = 40
MAX_TOOL_ROUNDS = 6
MAX_MESSAGE_CHARS = 4000
MAX_SUGGESTIONS = 5


def _main():
    # The app's one Anthropic client, the rate limiter and the holdings /
    # price helpers live in main.py, which imports this module - so it's
    # imported here at call time instead of at the top.
    import main
    return main


# --- The instructions (stable, so they're prompt-cached) ------------------------------

ADVISOR_INSTRUCTIONS = """You are the advisor inside InnerStock, a trading journal and portfolio tracker.

What sets InnerStock apart from a brokerage app is that it knows *why* this user made each trade: their written theses, the reason categories they picked, conviction scores, the review dates they committed to, and how their past calls actually turned out against the S&P 500. Your value is connecting the market to that record - be the sharp, honest friend who has read their whole journal and checked the numbers.

How to work:
- Look things up instead of guessing. Use the tools whenever an answer depends on current prices, fundamentals, performance, news, earnings, the user's own trades or how a trade would change their portfolio. The snapshot below is current as of this message; the tools have the detail. Call several tools at once when they're independent.
- Ground what you say in numbers you actually have, and make clear where they come from (for example "down 12% over 3 months while the S&P 500 is up 4%"). If a lookup fails or data is missing, say so plainly. Never invent prices, dates, figures or news.
- Tie answers back to the user's own record whenever it's relevant: what their thesis for a position was and whether it still holds, how trades made for the same reason have gone, whether their conviction scores have been well calibrated, FOMO flags, concentration, and grades they gave against the evidence. Point out patterns they may not see, kindly but directly.
- Give your actual read. Lay out the reasoning and, for judgment calls, what would change your mind. One short line on uncertainty is enough when it matters - no boilerplate disclaimers. Weigh options against their profile (risk tolerance, horizon, goals, biggest fear).
- For "should I buy/sell X" questions, check the facts (snapshot, performance, news, earnings), their history with that stock or that kind of reason, run check_fomo on buys (if it says chasing, lead with that and their own record of chasing), and use simulate_trade to show what it would do to their portfolio. Then give a clear view of the case for and against, and what a sensible next step would be.
- You can't place trades or see their brokerage beyond what's logged in InnerStock.

Style:
- Markdown. Open with the direct answer in one sentence, then short paragraphs or bullets. Bold sparingly.
- Usually under 200 words; go longer only when the user asks for depth or the question genuinely needs it.
- When you mention a news story, link its headline.
- Prices are in each listing's own currency: TSX listings (.TO) are in CAD, US listings in USD."""


# --- Tools ------------------------------------------------------------------------------

def _ticker_schema(description="Ticker symbol as Yahoo Finance writes it, e.g. AAPL, SHOP.TO, VFV.TO, BRK-B."):
    return {"type": "string", "description": description}


TOOLS = [
    {
        "name": "get_stock_snapshot",
        "description": "Current quote and fundamentals for any ticker (held or not): price, day change, market cap, sector, P/E, forward P/E, price/book, profit margin, revenue growth, debt/equity, dividend yield, next earnings date, plus the user's own position in it and whether it's on their watchlist.",
        "input_schema": {"type": "object", "properties": {"ticker": _ticker_schema()}, "required": ["ticker"]},
    },
    {
        "name": "get_price_performance",
        "description": "How a ticker has performed: returns over 1 week, 1/3/6 months, year-to-date and 1 year next to the S&P 500 over the same windows (currency-matched), 52-week high/low, distance from the high, annualized volatility and 1-year max drawdown.",
        "input_schema": {"type": "object", "properties": {"ticker": _ticker_schema()}, "required": ["ticker"]},
    },
    {
        "name": "get_company_news",
        "description": "The most important news from the last 7 days that actually names the company, ranked (results, guidance, analyst calls, deals, legal issues and big moves first). Each story has a title, publisher, time and link.",
        "input_schema": {"type": "object", "properties": {"ticker": _ticker_schema()}, "required": ["ticker"]},
    },
    {
        "name": "get_earnings",
        "description": "Earnings for a ticker: the next report date, recent reports with EPS vs the analyst estimate, and InnerStock's latest earnings recap for it (what happened, what it means, stock reaction) if one exists.",
        "input_schema": {"type": "object", "properties": {"ticker": _ticker_schema()}, "required": ["ticker"]},
    },
    {
        "name": "get_my_trades",
        "description": "The user's own logged trades, newest first: date, buy/sell, quantity, price, the reasons and thesis they gave, conviction (1-5), review date, the grade they gave once reviewed (correct/incorrect/mixed), their review notes, and the trade's return vs the S&P 500 at grading. Optionally only one ticker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": _ticker_schema("Only this ticker's trades. Leave out for all tickers."),
                "limit": {"type": "integer", "description": "How many trades, newest first (default 20, max 50)."},
            },
        },
    },
    {
        "name": "get_trade_review",
        "description": "The evidence for one of the user's trades (by trade id from get_my_trades): return since the trade and at its review date, the S&P 500 over the same days, best/worst points since, the FOMO check, InnerStock's suggested grade, and the thesis they wrote.",
        "input_schema": {"type": "object", "properties": {"trade_id": {"type": "integer"}}, "required": ["trade_id"]},
    },
    {
        "name": "get_portfolio_breakdown",
        "description": "The shape of the user's portfolio: every holding with value, weight, average cost, gain/loss and today's move, plus concentration (largest and top-3 weights), sector weights, CAD vs USD split, and biggest winners/losers.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "simulate_trade",
        "description": "What a hypothetical trade would do to the user's portfolio, without placing anything: the position's new size and weight, the largest position, sector weights before and after, and whether it crosses the 20% single-position limit. Give either shares or dollars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": _ticker_schema(),
                "action": {"type": "string", "enum": ["buy", "sell"]},
                "shares": {"type": "number", "description": "Number of shares."},
                "dollars": {"type": "number", "description": "Amount in the listing's currency, instead of shares."},
            },
            "required": ["ticker", "action"],
        },
    },
    {
        "name": "check_fomo",
        "description": "InnerStock's FOMO check for buying a ticker right now (or on a given date): a 0-100 score and band (chasing / warm / calm / dip) from the stock's run-up vs the market measured against its own normal weekly move, closeness to its 20-day high, volume spike, one-day jumps, the time of day, size vs the user's usual buy, and whether they're buying back something they sold higher. Also returns the user's own record: how their past chasing buys did vs the S&P 500, the FOMO tax in dollars, and how often the stock was cheaper a week later. Use it whenever the user is thinking about buying something that has been moving.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": _ticker_schema(),
                "dollars": {"type": "number", "description": "How much they're thinking of buying, if known (checks size vs their usual)."},
            },
            "required": ["ticker"],
        },
    },
]

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")


class ToolError(Exception):
    """A tool couldn't answer - the message goes back to Claude as an error result."""


def _clean_ticker(value):
    ticker = str(value or "").strip().upper()
    if not _TICKER_RE.match(ticker):
        raise ToolError(f"'{value}' isn't a valid ticker symbol.")
    return ticker


def _round(value, digits=2):
    return round(float(value), digits) if value is not None else None


def _currency(ticker):
    return "CAD" if ticker.endswith(grading.CANADIAN_SUFFIXES) else "USD"


class AdvisorContext:
    """Per-request state shared by the tools: the DB session, the user, and
    the holdings (computed once, on first use)."""

    def __init__(self, db, user):
        self.db = db
        self.user = user
        self.now = datetime.now(timezone.utc)
        self.today = self.now.astimezone(hn.MARKET_TZ).date()
        self._holdings = None

    def holdings(self):
        if self._holdings is None:
            self._holdings = _main().get_holdings(db=self.db, current_user=self.user)
        return self._holdings

    def holding(self, ticker):
        return next((h for h in self.holdings()["holdings"] if h["ticker"] == ticker), None)

    def price_row(self, ticker):
        return _main().get_or_fetch_price(ticker, self.db)


def _next_earnings(ticker, profile, today):
    if not profile or profile.get("quote_type") == "ETF":
        return None
    source, source_profile = hn.resolve_source(ticker, profile)
    return hn.upcoming_earnings(source_profile, today) or hn.calendar_earnings(hn.get_calendar_earnings(source), today)


def tool_stock_snapshot(ctx, ticker):
    ticker = _clean_ticker(ticker)
    row = ctx.price_row(ticker)
    if not row:
        raise ToolError(f"No price data for {ticker}. Check the symbol (TSX listings end in .TO).")
    price = float(row.current_price)
    previous = getattr(row, "previous_close", None)
    profile = hn.get_profile(ticker)

    def field(name):
        value = getattr(row, name, None)
        return float(value) if value is not None else None

    margins, growth = field("profit_margins"), field("revenue_growth")
    holding = ctx.holding(ticker)
    on_watchlist = ctx.db.query(Watchlist).filter(Watchlist.user_id == ctx.user.id, Watchlist.ticker == ticker).first() is not None
    return {
        "ticker": ticker,
        "name": profile.get("name") if profile else None,
        "type": profile.get("quote_type") if profile else None,
        "currency": _currency(ticker),
        "price": _round(price, 4),
        "day_change_pct": _round((price / float(previous) - 1) * 100) if previous else None,
        "market_cap": field("market_cap"),
        "sector": getattr(row, "sector", None),
        "pe_ratio": _round(field("pe_ratio")),
        "forward_pe": _round(field("forward_pe")),
        "price_to_book": _round(field("price_to_book")),
        "profit_margin_pct": _round(margins * 100, 1) if margins is not None else None,
        "revenue_growth_pct": _round(growth * 100, 1) if growth is not None else None,
        "debt_to_equity": _round(field("debt_to_equity")),
        "dividend_yield_pct": _round(field("dividend_yield")),
        "next_earnings": _next_earnings(ticker, profile, ctx.today),
        "your_position": {
            "shares": holding["shares"], "avg_cost": holding["avg_cost"], "value": _round(holding["value"]),
            "weight_pct": holding["percentage"], "gain_loss_pct": holding["total_gain_loss_percent"],
        } if holding else None,
        "on_your_watchlist": on_watchlist,
    }


def _window_returns(closes, today, last):
    windows = {"1w": today - timedelta(days=7), "1m": today - timedelta(days=30), "3m": today - timedelta(days=91),
               "6m": today - timedelta(days=182), "ytd": date(today.year, 1, 1) - timedelta(days=1),
               "1y": today - timedelta(days=365)}
    out = {}
    for name, start_day in windows.items():
        start = grading.close_on_or_before(closes, start_day)
        out[name] = grading.pct_change(start[1], last) if start else None
    return out


def tool_price_performance(ctx, ticker):
    ticker = _clean_ticker(ticker)
    closes = grading.fetch_daily_closes(ticker, ctx.today - timedelta(days=400), ctx.today)
    if len(closes) < 2:
        raise ToolError(f"No price history for {ticker}.")
    last_day, last = closes[-1]
    year = [c for c in closes if c[0] >= ctx.today - timedelta(days=365)]
    high, low = max(c for _, c in year), min(c for _, c in year)

    daily = [math.log(b[1] / a[1]) for a, b in zip(year, year[1:]) if a[1] > 0 and b[1] > 0]
    mean = sum(daily) / len(daily) if daily else 0
    volatility = math.sqrt(sum((r - mean) ** 2 for r in daily) / (len(daily) - 1)) * math.sqrt(252) * 100 if len(daily) > 1 else None

    peak, max_drawdown = year[0][1], 0.0
    for _, close in year:
        peak = max(peak, close)
        max_drawdown = min(max_drawdown, (close / peak - 1) * 100)

    result = {
        "ticker": ticker,
        "as_of": last_day.isoformat(),
        "price": _round(last, 4),
        "returns_pct": _window_returns(closes, ctx.today, last),
        "high_52w": _round(high, 4),
        "low_52w": _round(low, 4),
        "pct_below_52w_high": _round((last / high - 1) * 100),
        "annualized_volatility_pct": _round(volatility, 1),
        "max_drawdown_1y_pct": _round(max_drawdown, 1),
    }
    benchmark = grading.benchmark_for(ticker)
    if benchmark:
        bench = grading.fetch_daily_closes(benchmark[0], ctx.today - timedelta(days=400), ctx.today)
        if len(bench) >= 2:
            result["benchmark"] = {"ticker": benchmark[0], "label": benchmark[1],
                                   "returns_pct": _window_returns(bench, ctx.today, bench[-1][1])}
    else:
        result["benchmark_note"] = "This fund tracks the S&P 500 itself, so there's no separate benchmark."
    return result


def _source(ticker):
    profile = hn.get_profile(ticker)
    source, source_profile = hn.resolve_source(ticker, profile)
    return profile, source, source_profile


def tool_company_news(ctx, ticker):
    ticker = _clean_ticker(ticker)
    profile, source, source_profile = _source(ticker)
    if profile and profile.get("quote_type") == "ETF":
        return {"ticker": ticker, "stories": [], "note": "ETFs have no company news of their own."}
    raw = hn.get_news(source)
    if raw is None:
        raise ToolError(f"News for {ticker} is unavailable right now.")
    stories = hn.rank_news(raw, source, source_profile, ctx.now, limit=8)
    return {"ticker": ticker, "news_ticker": source, "stories": stories,
            "note": None if stories else "No stories naming the company in the last 7 days."}


def tool_earnings(ctx, ticker):
    ticker = _clean_ticker(ticker)
    profile, source, source_profile = _source(ticker)
    if profile and profile.get("quote_type") == "ETF":
        return {"ticker": ticker, "note": "ETFs don't report earnings."}
    reports = hn.get_calendar_earnings(source) or []
    past = sorted((r for r in reports if r["ts"] <= ctx.now.timestamp()), key=lambda r: -r["ts"])[:4]
    recap = ctx.db.query(EarningsRecap).filter(EarningsRecap.ticker == source) \
        .order_by(EarningsRecap.report_date.desc()).first()
    return {
        "ticker": ticker,
        "earnings_ticker": source,
        "next_report": hn.upcoming_earnings(source_profile, ctx.today) or hn.calendar_earnings(reports, ctx.today),
        "recent_reports": [{
            "date": datetime.fromtimestamp(r["ts"], tz=timezone.utc).astimezone(hn.MARKET_TZ).date().isoformat(),
            "eps_estimate": r.get("eps_estimate"), "eps_actual": r.get("eps_actual"), "surprise_pct": r.get("surprise_pct"),
        } for r in past],
        "latest_recap": {
            "report_date": recap.report_date.isoformat(), "headline": recap.headline,
            "what_happened": recap.what_happened, "what_it_means": recap.what_it_means,
            "sentiment": recap.sentiment, "stock_reaction_pct": recap.reaction_pct,
        } if recap else None,
    }


def _describe_trade(trade):
    tags = thesis.parse_tags(trade)
    return {
        "trade_id": trade.id,
        "date": trade.trade_date.isoformat(),
        "action": trade.action,
        "ticker": trade.ticker,
        "quantity": float(trade.quantity),
        "price": float(trade.price_per_share),
        "source": trade.source,
        "reasons": [thesis.label_for(trade.action, t) for t in tags],
        "thesis": trade.thesis_text,
        "conviction": trade.conviction_score,
        "review_date": trade.review_date.isoformat() if trade.review_date else None,
        "grade": trade.outcome_tag,
        "review_notes": trade.review_notes,
        "return_at_grading_pct": trade.graded_return_pct,
        "sp500_return_at_grading_pct": trade.graded_benchmark_return_pct,
    }


def tool_my_trades(ctx, ticker=None, limit=20):
    query = ctx.db.query(Trade).filter(Trade.user_id == ctx.user.id)
    if ticker:
        query = query.filter(Trade.ticker == _clean_ticker(ticker))
    total = query.count()
    limit = max(1, min(int(limit or 20), 50))
    trades = query.order_by(Trade.trade_date.desc(), Trade.id.desc()).limit(limit).all()
    return {"total_matching": total, "trades": [_describe_trade(t) for t in trades]}


def tool_trade_review(ctx, trade_id):
    trade = ctx.db.query(Trade).filter(Trade.id == int(trade_id), Trade.user_id == ctx.user.id).first()
    if not trade:
        raise ToolError(f"No trade #{trade_id} in this user's journal.")
    card = grading.scorecard(trade, ctx.today)
    series = card.pop("series", None) or []
    if series:  # a few points is enough to describe the path
        step = max(1, len(series) // 10)
        card["price_path"] = series[::step][-10:]
    card["trade"] = _describe_trade(trade)
    return card


def _sector(ctx, ticker):
    row = ctx.price_row(ticker)
    return (getattr(row, "sector", None) if row else None) or "Unknown / fund"


def _sector_weights(ctx, values):
    total = sum(values.values())
    weights = {}
    for ticker, value in values.items():
        sector = _sector(ctx, ticker)
        weights[sector] = weights.get(sector, 0) + (value / total * 100 if total else 0)
    return {s: round(w, 1) for s, w in sorted(weights.items(), key=lambda kv: -kv[1])}


def tool_portfolio_breakdown(ctx):
    data = ctx.holdings()
    holdings = sorted(data["holdings"], key=lambda h: -h["value"])
    if not holdings:
        return {"holdings": [], "note": "The user has no current holdings."}
    values = {h["ticker"]: h["value"] for h in holdings}
    cad = sum(v for t, v in values.items() if _currency(t) == "CAD")
    total = sum(values.values())
    by_gain = sorted(holdings, key=lambda h: h["total_gain_loss_percent"])
    return {
        "position_count": len(holdings),
        "total_value_mixed_currency": _round(total),
        "holdings": [{
            "ticker": h["ticker"], "shares": h["shares"], "value": _round(h["value"]), "weight_pct": h["percentage"],
            "avg_cost": h["avg_cost"], "gain_loss_pct": h["total_gain_loss_percent"],
            "day_change_pct": h["day_gain_loss_percent"], "sector": _sector(ctx, h["ticker"]),
            "currency": _currency(h["ticker"]), "over_20pct_limit": h["overweight_flag"],
        } for h in holdings],
        "largest_position_pct": holdings[0]["percentage"],
        "top3_weight_pct": _round(sum(h["percentage"] for h in holdings[:3]), 1),
        "sector_weights_pct": _sector_weights(ctx, values),
        "currency_split_pct": {"CAD": _round(cad / total * 100, 1) if total else None,
                               "USD": _round((total - cad) / total * 100, 1) if total else None},
        "biggest_winners": [h["ticker"] for h in by_gain[::-1][:3] if h["total_gain_loss_percent"] > 0],
        "biggest_losers": [h["ticker"] for h in by_gain[:3] if h["total_gain_loss_percent"] < 0],
        "note": "Values are in each listing's own currency and the total adds them as-is.",
    }


def tool_simulate_trade(ctx, ticker, action, shares=None, dollars=None):
    ticker = _clean_ticker(ticker)
    if action not in ("buy", "sell"):
        raise ToolError("action must be 'buy' or 'sell'.")
    row = ctx.price_row(ticker)
    if not row:
        raise ToolError(f"No price data for {ticker}.")
    price = float(row.current_price)
    if shares is None and dollars is None:
        raise ToolError("Give either shares or dollars.")
    qty = float(shares) if shares is not None else float(dollars) / price
    if qty <= 0:
        raise ToolError("The amount must be positive.")

    values = {h["ticker"]: h["value"] for h in ctx.holdings()["holdings"]}
    held_shares = (ctx.holding(ticker) or {}).get("shares", 0)
    if action == "sell" and qty > held_shares + hn.SHARE_QTY_EPSILON:
        raise ToolError(f"The user only holds {held_shares:g} shares of {ticker}.")

    after = dict(values)
    change = qty * price * (1 if action == "buy" else -1)
    after[ticker] = after.get(ticker, 0) + change
    if after[ticker] <= 0.005:
        after.pop(ticker)

    def shape(vals):
        total = sum(vals.values())
        weights = {t: v / total * 100 for t, v in vals.items()} if total else {}
        largest = max(weights.items(), key=lambda kv: kv[1]) if weights else (None, 0)
        return {
            "total_value": _round(total),
            "position_weight_pct": _round(weights.get(ticker, 0), 1),
            "largest_position": {"ticker": largest[0], "weight_pct": _round(largest[1], 1)},
            "positions_over_20pct": sorted(t for t, w in weights.items() if w > 20),
            "sector_weights_pct": _sector_weights(ctx, vals),
        }

    past_trades = ctx.db.query(Trade).filter(Trade.user_id == ctx.user.id, Trade.ticker == ticker).count()
    return {
        "trade": {"action": action, "ticker": ticker, "shares": round(qty, 4), "price": _round(price, 4),
                  "amount": _round(qty * price), "currency": _currency(ticker), "sector": _sector(ctx, ticker)},
        "before": shape(values),
        "after": shape(after),
        "shares_after": round(held_shares + (qty if action == "buy" else -qty), 4),
        "user_has_traded_this_before": past_trades > 0,
        "note": "Hypothetical only - nothing was placed. Values mix CAD and USD listings as-is.",
    }


def tool_check_fomo(ctx, ticker, dollars=None):
    ticker = _clean_ticker(ticker)
    try:
        result = fomo.check_entry(ctx.db, ctx.user, ticker)
        if dollars and result["price"]:
            result = fomo.check_entry(ctx.db, ctx.user, ticker, price=result["price"], quantity=float(dollars) / result["price"])
    except HTTPException as e:
        raise ToolError(str(e.detail))
    result["record"] = {k: v for k, v in result["record"].items() if k != "groups"} |         {"chasing": result["record"]["groups"]["chasing"], "calm": result["record"]["groups"]["calm"]}
    return result


_TOOL_FUNCTIONS = {
    "get_stock_snapshot": lambda ctx, a: tool_stock_snapshot(ctx, a.get("ticker")),
    "get_price_performance": lambda ctx, a: tool_price_performance(ctx, a.get("ticker")),
    "get_company_news": lambda ctx, a: tool_company_news(ctx, a.get("ticker")),
    "get_earnings": lambda ctx, a: tool_earnings(ctx, a.get("ticker")),
    "get_my_trades": lambda ctx, a: tool_my_trades(ctx, a.get("ticker"), a.get("limit", 20)),
    "get_trade_review": lambda ctx, a: tool_trade_review(ctx, a.get("trade_id")),
    "get_portfolio_breakdown": lambda ctx, a: tool_portfolio_breakdown(ctx),
    "simulate_trade": lambda ctx, a: tool_simulate_trade(ctx, a.get("ticker"), a.get("action"), a.get("shares"), a.get("dollars")),
    "check_fomo": lambda ctx, a: tool_check_fomo(ctx, a.get("ticker"), a.get("dollars")),
}


def tool_label(name, args):
    """Short "looked up" label shown under the reply."""
    ticker = str(args.get("ticker") or "").upper()
    return {
        "get_stock_snapshot": f"{ticker} quote & fundamentals",
        "get_price_performance": f"{ticker} performance vs S&P 500",
        "get_company_news": f"{ticker} news",
        "get_earnings": f"{ticker} earnings",
        "get_my_trades": f"your {ticker} trades" if ticker else "your trade history",
        "get_trade_review": f"trade #{args.get('trade_id')} review",
        "get_portfolio_breakdown": "portfolio breakdown",
        "simulate_trade": f"what-if: {args.get('action', '')} {ticker}".strip(),
        "check_fomo": f"FOMO check on {ticker}",
    }.get(name, name)


def run_tool(ctx, name, args):
    """(result JSON text, is_error) - never raises, so one bad lookup can't sink the answer."""
    function = _TOOL_FUNCTIONS.get(name)
    if not function:
        return f"Unknown tool '{name}'.", True
    try:
        return json.dumps(function(ctx, args or {}), default=str), False
    except ToolError as e:
        return str(e), True
    except Exception as e:  # a yfinance hiccup etc. - tell Claude, keep going
        print(f"[advisor] tool {name} failed: {e}")
        return f"The {name} lookup failed: {e}", True


# --- The conversation ------------------------------------------------------------------------

def recent_history(db, user):
    """The NEWEST HISTORY_MESSAGES messages, oldest first, starting with a user turn.

    (Bug fix: this used to sort oldest-first and take the first 40, so once
    a conversation passed 40 messages the advisor only ever saw the start
    of it - and not the question just asked.)
    """
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.id.desc())
        .limit(HISTORY_MESSAGES)
        .all()
    )[::-1]
    messages = [{"role": r.role, "content": r.content} for r in rows if r.content]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)  # the API needs the first message to be the user's
    return messages


def _final_text(message):
    # With the refusal fallback, a declined model's partial text can come
    # before a "fallback" block; only text after the last one is the answer.
    text = []
    for block in message.content:
        if block.type == "fallback":
            text = []
        elif block.type == "text":
            text.append(block.text)
    return "".join(text).strip()


class AdvisorUnavailable(Exception):
    pass


def run_advisor(db, user, history, facts):
    """Runs the tool loop. Returns (reply text, [tool labels used])."""
    client = _main().claude_client
    ctx = AdvisorContext(db, user)
    system = [
        # Same every time, so it (plus the tool definitions before it) is
        # served from the prompt cache after the first message.
        {"type": "text", "text": ADVISOR_INSTRUCTIONS, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "Snapshot of the user's situation right now:\n\n" + facts},
    ]
    messages = list(history)
    tools_used = []
    usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    message = None

    for round_number in range(MAX_TOOL_ROUNDS + 1):
        last_round = round_number == MAX_TOOL_ROUNDS
        try:
            message = client.beta.messages.create(
                model=ADVISOR_MODEL,
                max_tokens=16000,
                system=system,
                messages=messages,
                tools=TOOLS,
                # Out of lookup rounds: answer with what's been gathered.
                tool_choice={"type": "none"} if last_round else {"type": "auto"},
                output_config={"effort": "medium"},
                # Caches the conversation so far, so each lookup round
                # re-reads it at the cache price.
                cache_control={"type": "ephemeral"},
                # A declined request is retried on Anthropic's recommended
                # fallback model inside the same call instead of failing.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIStatusError as e:
            print(f"[advisor] Claude API error: {e.status_code} {e.message}")
            raise AdvisorUnavailable() from e
        except anthropic.APIConnectionError as e:
            print(f"[advisor] could not reach Claude: {e}")
            raise AdvisorUnavailable() from e

        u = message.usage
        usage["input"] += u.input_tokens
        usage["output"] += u.output_tokens
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0

        tool_calls = [b for b in message.content if b.type == "tool_use"]
        if message.stop_reason != "tool_use" or not tool_calls:
            break

        # Echo Claude's turn back unchanged (thinking blocks included), then
        # answer every tool call in ONE user message.
        messages.append({"role": "assistant", "content": message.content})
        results = []
        for call in tool_calls:
            content, is_error = run_tool(ctx, call.name, call.input)
            if not is_error:
                tools_used.append(tool_label(call.name, call.input or {}))
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    cost = (usage["input"] * CLAUDE_OPUS_5_5_INPUT_COST_PER_MTOK + usage["output"] * CLAUDE_OPUS_5_5_OUTPUT_COST_PER_MTOK
            + usage["cache_write"] * CLAUDE_OPUS_5_5_CACHE_WRITE_COST_PER_MTOK
            + usage["cache_read"] * CLAUDE_OPUS_5_5_CACHE_READ_COST_PER_MTOK) / 1_000_000
    print(f"[advisor] user_id={user.id} rounds={round_number + 1} tools={len(tools_used)} ~${cost:.4f} "
          f"(in {usage['input']}, out {usage['output']}, cache write {usage['cache_write']}, cache read {usage['cache_read']})")

    if message.stop_reason == "refusal":
        return "I can't help with that one. Try asking it a different way.", tools_used
    reply = _final_text(message)
    if not reply:
        raise AdvisorUnavailable()
    if message.stop_reason == "max_tokens":
        reply += "\n\n*(Cut short - ask me to continue.)*"
    # Same lookup twice (e.g. a retry) shouldn't show twice.
    return reply, list(dict.fromkeys(tools_used))


class ChatMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


# PROTECTED + SCOPED: only ever sees and stores this user's own data.
@router.post("/chat")
def chat(payload: ChatMessageIn, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    main = _main()
    # Checked before saving anything, so a rate-limited message isn't
    # stored in history without a reply to go with it. One message counts
    # once, however many lookups it takes.
    main.enforce_ai_rate_limit(current_user.id, "demo_chat" if current_user.is_demo else "chat")
    text = payload.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")

    # Stored before calling Claude, so it's never lost if something fails.
    db.add(ChatMessage(user_id=current_user.id, role="user", content=text))
    db.commit()

    history = recent_history(db, current_user)
    # Rebuilt every message - holdings and stats can change mid-conversation.
    facts = main.build_advisor_context(db, current_user)
    try:
        reply, tools_used = run_advisor(db, current_user, history, facts)
    except AdvisorUnavailable:
        raise HTTPException(status_code=500, detail="Could not generate a response right now")

    db.add(ChatMessage(user_id=current_user.id, role="assistant", content=reply))
    db.commit()
    return {"reply": reply, "tools_used": tools_used}


# --- Suggested questions ------------------------------------------------------------------
# Built from the user's current situation with plain rules (no Claude call),
# so they're instant and free: what reported, what's about to report, what's
# due for review, what moved, what's too big, what's down, which habit is
# costing them. Generic questions fill any remaining slots.

def _when(days):
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"


def build_suggestions(db, user):
    ctx = AdvisorContext(db, user)
    holdings = ctx.holdings()["holdings"]
    found = []  # (priority, ticker or None, label, prompt)

    unread = (
        db.query(EarningsNotification, EarningsRecap)
        .join(EarningsRecap, EarningsNotification.recap_id == EarningsRecap.id)
        .filter(EarningsNotification.user_id == user.id, EarningsNotification.read_at.is_(None))
        .order_by(EarningsRecap.report_date.desc())
        .limit(2)
        .all()
    )
    for notification, recap in unread:
        t = notification.display_ticker
        found.append((100, t, f"What {t}'s earnings mean for me",
                      f"{t} just reported earnings. What does the report mean for my position and for the thesis I bought it on?"))

    # Earnings lookups are yfinance calls (cached for hours, but slow when
    # cold), so they run in parallel like the sidebar's.
    if holdings:
        with ThreadPoolExecutor(max_workers=min(hn.MAX_WORKERS, len(holdings))) as pool:
            upcoming = list(pool.map(lambda h: _next_earnings(h["ticker"], hn.get_profile(h["ticker"]), ctx.today), holdings))
    else:
        upcoming = []
    for h, earnings in zip(holdings, upcoming):
        if earnings and earnings["days_until"] <= 7:
            t, when = h["ticker"], _when(earnings["days_until"])
            found.append((95 - earnings["days_until"], t, f"{t} reports {when}",
                          f"{t} reports earnings {when}. What should I watch for, what's expected, and how exposed am I?"))

    due = db.query(Trade).filter(Trade.user_id == user.id, Trade.review_date <= ctx.today, Trade.outcome_tag.is_(None)).count()
    if due:
        found.append((85, None, f"Review my {due} due trade{'s' if due != 1 else ''}",
                      "Walk me through my trades that are due for review: was each thesis right, how did they do against the S&P 500, and what should I learn?"))

    # NEW: trades InnerStock graded automatically in the last week - worth a
    # look, since the user never saw those grades being made.
    week_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    auto = db.query(Trade).filter(Trade.user_id == user.id, Trade.auto_graded.is_(True), Trade.graded_at >= week_ago).all()
    if auto:
        tickers = ", ".join(sorted({t.ticker for t in auto}))
        label = "What my new grade says" if len(auto) == 1 else f"What my {len(auto)} new grades say"
        found.append((82, None, label,
                      f"InnerStock just graded {len(auto)} of my trades automatically ({tickers}). What do they say about my reasoning, and does any grade look wrong to you?"))

    for h in holdings:
        move = h["day_gain_loss_percent"]
        if abs(move) >= 4:
            t, direction = h["ticker"], "up" if move > 0 else "down"
            found.append((80 + min(abs(move), 10) / 10, t, f"Why is {t} {direction} {abs(move):.0f}% today?",
                          f"{t} is {direction} {abs(move):.1f}% today. What's driving it, and does it change my thesis?"))

    for h in holdings:
        if h["overweight_flag"]:
            t = h["ticker"]
            found.append((70 + h["percentage"] / 100, t, f"Is {t} too big a position?",
                          f"{t} is {h['percentage']:.0f}% of my portfolio. Is that too concentrated for my risk profile, and what would trimming look like?"))

    for h in sorted(holdings, key=lambda h: h["total_gain_loss_percent"]):
        if h["total_gain_loss_percent"] <= -15:
            t = h["ticker"]
            found.append((65, t, f"Does my {t} thesis still hold?",
                          f"{t} is down {abs(h['total_gain_loss_percent']):.0f}% from what I paid. Does my original thesis still hold, or am I holding on hope?"))
            break

    # NEW: chasing has measurably cost them - worth a conversation.
    chasing = fomo.personal_record(db, user)["groups"]["chasing"]
    if chasing["graded"] >= 2 and chasing["avg_vs_market_pct"] is not None and chasing["avg_vs_market_pct"] < 0:
        found.append((62, None, "What has chasing cost me?",
                      f"My FOMO check says my chasing buys have averaged {chasing['avg_vs_market_pct']:+.1f} points vs the S&P 500. "
                      "Which trades were they, what did they have in common, and what rule should I set for myself?"))

    summary = thesis.reasons_summary(db, user)
    lagging = [e for e in summary["by_reason"]
               if e["action"] == "buy" and e["graded"] >= 2 and e["avg_vs_market_pct"] is not None and e["avg_vs_market_pct"] < 0]
    if lagging:
        worst = min(lagging, key=lambda e: e["avg_vs_market_pct"])
        found.append((60, None, f"Why my \"{worst['label']}\" buys lag",
                      f"My buys where the reason was \"{worst['label']}\" have averaged {worst['avg_vs_market_pct']:+.1f} points vs the S&P 500. What's going wrong and what should I change?"))

    # Generic, but still about THEIR record and portfolio.
    if holdings:
        found.append((20, None, "Which holdings are lagging the market?",
                      "Which of my holdings have done worst against the S&P 500 recently, and do my theses for them still make sense?"))
        found.append((19, None, "Biggest risks in my portfolio",
                      "What are the biggest risks in my portfolio right now - concentration, sectors, currency, upcoming events?"))
    if summary["answered"]:
        found.append((18, None, "What does my record say about me?",
                      "Looking at my reasons, convictions and grades, what kind of investor am I really, and what's my biggest blind spot?"))
    watchlist = [w.ticker for w in db.query(Watchlist).filter(Watchlist.user_id == user.id).all()]
    if watchlist:
        found.append((17, None, "Anything on my watchlist worth a look?",
                      f"Of the stocks on my watchlist ({', '.join(watchlist[:8])}), which looks most interesting right now and why?"))
    found.append((10, None, "How should I think about my next buy?",
                  "Based on my profile and current portfolio, what should I look for in my next buy, and what should I avoid?"))

    suggestions, seen_tickers = [], set()
    for _, ticker, label, prompt in sorted(found, key=lambda f: -f[0]):
        if ticker and ticker in seen_tickers:
            continue  # one suggestion per stock
        if ticker:
            seen_tickers.add(ticker)
        suggestions.append({"label": label, "prompt": prompt})
        if len(suggestions) == MAX_SUGGESTIONS:
            break
    return suggestions


@router.get("/chat/suggestions")
def chat_suggestions(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return {"suggestions": build_suggestions(db, current_user)}
