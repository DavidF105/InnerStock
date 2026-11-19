# Upcoming earnings dates and the most important company-specific news for
# every stock the user currently holds. Shown in the sidebar under the
# watchlist, so wherever you are in the app you can see "SHOP reports in 3
# days" and the two or three headlines that actually matter for it.
#
# Everything here comes from yfinance:
#   - Ticker.info gives the company name, the quote type (ETF vs stock) and
#     the next earnings window. One call per ticker, cached for hours since
#     an earnings date barely ever moves.
#   - Ticker.get_news gives the ticker's news stream. Yahoo mixes plenty of
#     general market stories into it ("Dow futures slip..."), so articles
#     are kept only when they actually name the company, then ranked so
#     earnings, analyst calls, deals, lawsuits etc. come first.
#
# No Claude calls here - ranking is plain keyword rules, so this route is
# free to hit and needs no AI rate limit.
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

import yfinance as yf
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from deps import get_current_user, get_db
from models import EarningsNotification, EarningsRecap, Trade, User

router = APIRouter(tags=["holdings"])

# Earnings dates are announced in US/Eastern market time, the same clock
# trades are stored in (see snaptrade_sync.MARKET_TZ).
MARKET_TZ = ZoneInfo("America/New_York")

# Same tolerance as main.SHARE_QTY_EPSILON - a position sold down to float
# rounding noise counts as fully sold.
SHARE_QTY_EPSILON = 1e-9

PROFILE_TTL_SECONDS = 12 * 60 * 60   # earnings dates rarely change
NEWS_TTL_SECONDS = 30 * 60           # news moves faster, but not by the minute
FAILURE_RETRY_SECONDS = 2 * 60       # after a failed yfinance call, wait before retrying

NEWS_FETCH_COUNT = 20      # pull more than we show, since most get filtered out
NEWS_PER_TICKER = 3
NEWS_MAX_AGE_DAYS = 7

# How long after a report its recap stays attached to the sidebar row.
RECAP_SHOWN_DAYS = 30

# Parallel yfinance lookups - one holding at a time would make a 10-stock
# portfolio take 10+ seconds on a cold cache.
MAX_WORKERS = 8


# --- Which tickers are held -------------------------------------------------------

def held_tickers(trades):
    """Tickers with shares still held: buys minus sells, same tally as /holdings."""
    shares = {}
    for trade in trades:
        ticker = trade.ticker.upper()
        qty = float(trade.quantity)
        shares[ticker] = shares.get(ticker, 0) + (qty if trade.action == "buy" else -qty)
    return sorted(ticker for ticker, qty in shares.items() if qty > SHARE_QTY_EPSILON)


# --- yfinance loaders (the only network calls; tests replace these two) --------------

def load_profile(ticker):
    """Name, quote type and raw earnings timestamps from Ticker.info, or None on failure."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        print(f"[holdings-news] yfinance info failed for {ticker}: {e}")
        return None
    if not info.get("quoteType"):
        return None
    return {
        "name": info.get("longName") or info.get("shortName"),
        "short_name": info.get("shortName"),
        "quote_type": info.get("quoteType"),
        # Yahoo's fields, as Unix timestamps. earningsTimestampStart/End is
        # the NEXT expected window (Start != End while it's only an
        # estimate); earningsTimestamp is often the LAST report instead, so
        # upcoming_earnings() keeps only the ones that are still ahead.
        "earnings_start": info.get("earningsTimestampStart"),
        "earnings_end": info.get("earningsTimestampEnd"),
        "earnings_ts": info.get("earningsTimestamp"),
        "earnings_is_estimate": bool(info.get("isEarningsDateEstimate")),
    }


def load_news(ticker):
    """Raw yfinance news items for a ticker, or None if every source failed.

    NEW: Yahoo's per-ticker news stream (Ticker.get_news) went down for a
    whole day in Nov 2026 ("YAHOO! FINANCE IS CURRENTLY DOWN!") while its
    search endpoint kept working, so search news is the fallback. Search
    results are looser (anything mentioning the symbol), but rank_news only
    keeps stories that name the company anyway.
    """
    try:
        items = yf.Ticker(ticker).get_news(count=NEWS_FETCH_COUNT)
        if items:
            return items
    except Exception as e:
        print(f"[holdings-news] yfinance news failed for {ticker}, trying search: {e}")
    try:
        return yf.Search(ticker, news_count=NEWS_FETCH_COUNT, max_results=1).news or []
    except Exception as e:
        print(f"[holdings-news] yfinance search news failed for {ticker}: {e}")
        return None


def _number(value):
    """A float, or None for missing/NaN values (pandas uses NaN for "not reported yet")."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else value


def load_calendar_earnings(ticker):
    """Reports from Yahoo's earnings calendar page, or None on failure.

    Each entry: {"ts": unix time, "eps_estimate", "eps_actual", "surprise_pct"}
    (eps_actual / surprise_pct are None until the company has reported).

    NEW: fallback for when Ticker.info has no upcoming date. For a few
    weeks after a report, Yahoo's info fields only carry the report that
    just happened (in Nov 2026 GOOG/AMZN still showed their Oct 28/29
    reports), while the calendar page already lists the next expected one.
    The reported numbers also feed the earnings recaps (earnings_recaps.py).
    yfinance parses that page with pandas, which needs lxml installed.
    """
    try:
        dates = yf.Ticker(ticker).get_earnings_dates(limit=4)
    except Exception as e:
        print(f"[holdings-news] yfinance earnings calendar failed for {ticker}: {e}")
        return None
    if dates is None or dates.empty:
        return []
    return [
        {
            "ts": moment.timestamp(),
            "eps_estimate": _number(row.get("EPS Estimate")),
            "eps_actual": _number(row.get("Reported EPS")),
            "surprise_pct": _number(row.get("Surprise(%)")),
        }
        for moment, row in dates.iterrows()
    ]


# --- Small TTL cache ---------------------------------------------------------------
# In-memory, like grading's price-history cache: on Vercel each warm
# instance keeps its own copy, which is fine for data that's this cheap to
# re-fetch. A failed lookup keeps serving the last good value (if any) and
# isn't retried for FAILURE_RETRY_SECONDS, so a Yahoo outage doesn't turn
# every sidebar load into a fresh round of slow failing calls.

_profile_cache = {}
_news_cache = {}
_calendar_cache = {}
_cache_lock = threading.Lock()


def clear_cache():
    with _cache_lock:
        _profile_cache.clear()
        _news_cache.clear()
        _calendar_cache.clear()


def _cached(cache, key, ttl, loader):
    """`ttl` is seconds, or a function of the loaded value returning seconds."""
    now = monotonic()
    with _cache_lock:
        entry = cache.get(key)
    if entry and entry[0] > now:
        return entry[1]

    # Network call outside the lock so other tickers aren't blocked on it.
    value = loader(key)
    with _cache_lock:
        if value is None:
            stale = entry[1] if entry else None
            cache[key] = (now + FAILURE_RETRY_SECONDS, stale)
            return stale
        cache[key] = (now + (ttl(value) if callable(ttl) else ttl), value)
        return value


def get_profile(ticker):
    return _cached(_profile_cache, ticker, PROFILE_TTL_SECONDS, load_profile)


def get_news(ticker):
    return _cached(_news_cache, ticker, NEWS_TTL_SECONDS, load_news)


def _calendar_ttl(reports):
    """Re-check hourly while a report from the last 2 days still has no
    numbers (results come out over the following hours); 12h otherwise."""
    cutoff = datetime.now(timezone.utc).timestamp() - 2 * 24 * 3600
    now = datetime.now(timezone.utc).timestamp()
    waiting = any(cutoff <= r["ts"] <= now and r.get("eps_actual") is None for r in reports)
    return 60 * 60 if waiting else PROFILE_TTL_SECONDS


def get_calendar_earnings(ticker):
    return _cached(_calendar_cache, ticker, _calendar_ttl, load_calendar_earnings)


# --- CDRs ----------------------------------------------------------------------------

def _normalize_name(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def resolve_source(ticker, profile):
    """Which ticker to read earnings and news from.

    CAD-hedged CDRs (AAPL.TO = "APPLE CDR (CAD HEDGED)") carry stale or no
    earnings dates on Yahoo; the real company's US listing has them. But
    stripping ".TO" is only safe when we can PROVE it's the same company -
    T.TO is Telus while T is AT&T - so the fallback needs both "CDR" in the
    name and an identical company name on the US listing.

    NEW: some US listings have a share class Yahoo writes with a dash -
    BRK.TO tracks Berkshire's B shares, which Yahoo calls BRK-B, and bare
    "BRK" doesn't exist - so the class variants are tried too, under the
    same name check.
    """
    if not (profile and ticker.endswith(".TO") and "CDR" in (profile.get("short_name") or "").upper()):
        return ticker, profile
    base = ticker[:-3]
    for candidate in (base, f"{base}-B", f"{base}-A"):
        candidate_profile = get_profile(candidate)
        if candidate_profile and _normalize_name(candidate_profile.get("name")) == _normalize_name(profile.get("name")):
            return candidate, candidate_profile
    return ticker, profile


# --- Earnings ----------------------------------------------------------------------

def upcoming_earnings(profile, today):
    """The next earnings report on or after `today`, or None if none is announced yet."""
    if not profile:
        return None
    moments = []
    for key in ("earnings_start", "earnings_end", "earnings_ts"):
        stamp = profile.get(key)
        if isinstance(stamp, (int, float)) and stamp > 0:
            moments.append(datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(MARKET_TZ))
    upcoming = sorted(m for m in moments if m.date() >= today)
    if not upcoming:
        return None

    first = upcoming[0]
    # Estimated if Yahoo says so, or if the window still spans several days.
    is_estimate = profile.get("earnings_is_estimate") or upcoming[-1].date() != first.date()

    # Only a confirmed date has a meaningful time of day; Yahoo's estimated
    # timestamps are placeholders.
    timing = None
    if not is_estimate:
        if first.hour < 12:
            timing = "before_open"
        elif first.hour >= 16:
            timing = "after_close"

    return {
        "date": first.date().isoformat(),
        "days_until": (first.date() - today).days,
        "is_estimate": bool(is_estimate),
        "timing": timing,
    }


def calendar_earnings(reports, today):
    """NEW: the next report from the earnings calendar page (load_calendar_earnings).

    Always marked as an estimate with no time of day: the calendar lists
    Yahoo's projected date before the company has confirmed one.
    """
    days = sorted(
        datetime.fromtimestamp(report["ts"], tz=timezone.utc).astimezone(MARKET_TZ).date()
        for report in reports or []
    )
    upcoming = [day for day in days if day >= today]
    if not upcoming:
        return None
    return {
        "date": upcoming[0].isoformat(),
        "days_until": (upcoming[0] - today).days,
        "is_estimate": True,
        "timing": None,
    }


# --- News: parse, keep company-specific stories, rank ------------------------------

# Corporate suffixes stripped before matching, so "Apple Inc." matches a
# headline that just says "Apple".
_NAME_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "holdings", "holding", "group", "sa", "ag", "nv", "se", "the", "class",
    "a", "b", "c", "cdr", "cad", "hedged", "com",
}
# Words too common to match a company on their own ("Royal" alone would
# match "royal wedding"); multi-word names still match as a phrase.
_GENERIC_WORDS = {
    "first", "american", "national", "general", "united", "global", "international",
    "canadian", "canada", "royal", "bank", "new", "great", "west", "western", "north",
    "south", "east", "pacific", "atlantic", "capital", "energy", "financial",
    "digital", "advanced", "applied", "power", "health", "data", "life", "brands",
}
# Companies the headlines rarely call by their legal name.
_ALIASES = {
    "GOOG": ["google"], "GOOGL": ["google"], "GOOG.TO": ["google"],
    "META": ["facebook"], "BRK-B": ["berkshire"], "BRK-A": ["berkshire"],
}

# Headlines about things that move a stock: results, guidance, analyst
# calls, deals, legal trouble, leadership, capital returns, big moves.
_IMPORTANT = re.compile(
    r"\b(earnings|results|quarter(ly)?|q[1-4]|guidance|outlook|forecast|revenue|profit|"
    r"downgrade[sd]?|upgrade[sd]?|price target|rating|analysts?|acqui\w*|merger|buyout|"
    r"takeover|deal|lawsuit|sues|sued|probe|investigation|sec|antitrust|recall|ceo|cfo|"
    r"resign\w*|layoffs?|job cuts|dividend|buyback|split|fda|approval|beats?|miss(es)?|"
    r"plunge[sd]?|soar(s|ed)?|surge[sd]?|tumble[sd]?|slump[sd]?|jump(s|ed)?|record)\b",
    re.IGNORECASE,
)


def match_patterns(ticker, profile):
    """Regexes that mean "this text is about this company"."""
    patterns = []
    root = ticker.upper().split(".")[0].split("-")[0]
    # The symbol itself, case-sensitive ("SHOP" or "$SHOP", not "shop").
    # Single letters are skipped, they'd match any capital "T".
    if len(root) >= 2:
        patterns.append(re.compile(rf"(?<![A-Za-z]){re.escape(root)}(?![A-Za-z])"))

    words = [w for w in _normalize_name(profile.get("name") if profile else "").split() if w not in _NAME_SUFFIXES]
    phrases = set(_ALIASES.get(ticker.upper(), []))
    if words:
        phrases.add(" ".join(words))
        if len(words) >= 2:
            phrases.add(" ".join(words[:2]))
        if len(words[0]) >= 4 and words[0] not in _GENERIC_WORDS:
            phrases.add(words[0])
    for phrase in phrases:
        patterns.append(re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE))
    return patterns


def parse_article(item):
    """Normalizes one yfinance news item (new nested 'content' shape or the older flat one)."""
    content = item.get("content") or {}
    if content:
        title = content.get("title")
        summary = content.get("summary") or content.get("description") or ""
        url = (content.get("clickThroughUrl") or {}).get("url") or (content.get("canonicalUrl") or {}).get("url")
        publisher = (content.get("provider") or {}).get("displayName")
        published = None
        if content.get("pubDate"):
            try:
                published = datetime.fromisoformat(content["pubDate"].replace("Z", "+00:00"))
            except ValueError:
                published = None
        editors_pick = bool((content.get("metadata") or {}).get("editorsPick"))
    else:
        title = item.get("title")
        summary = item.get("summary") or ""
        url = item.get("link")
        publisher = item.get("publisher")
        stamp = item.get("providerPublishTime")
        published = datetime.fromtimestamp(stamp, tz=timezone.utc) if isinstance(stamp, (int, float)) else None
        editors_pick = False

    if not title or not url:
        return None
    if published and published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return {
        "title": title.strip(),
        "summary": summary,
        "url": url,
        "publisher": publisher,
        "published_at": published,
        "editors_pick": editors_pick,
    }


def rank_news(raw_items, ticker, profile, now, limit=NEWS_PER_TICKER):
    """The `limit` most important recent articles that are actually about this company."""
    patterns = match_patterns(ticker, profile)
    cutoff = now - timedelta(days=NEWS_MAX_AGE_DAYS)
    scored = []
    seen_titles = set()
    for raw in raw_items or []:
        article = parse_article(raw)
        if not article:
            continue
        if article["published_at"] and article["published_at"] < cutoff:
            continue
        # The same story syndicated by several outlets shows up more than once.
        title_key = article["title"].lower()
        if title_key in seen_titles:
            continue

        in_title = any(p.search(article["title"]) for p in patterns)
        in_summary = any(p.search(article["summary"]) for p in patterns)
        if not (in_title or in_summary):
            continue  # general market story, not about this holding
        seen_titles.add(title_key)

        score = 0
        if in_title:
            score += 3
        if _IMPORTANT.search(article["title"]):
            score += 2
        if article["editors_pick"]:
            score += 1
        published_ts = article["published_at"].timestamp() if article["published_at"] else 0
        scored.append((score, published_ts, article))

    # Highest score first, newest first within the same score.
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [
        {
            "title": article["title"],
            "url": article["url"],
            "publisher": article["publisher"],
            "published_at": article["published_at"].isoformat() if article["published_at"] else None,
        }
        for _, _, article in scored[:limit]
    ]


# --- Route ---------------------------------------------------------------------------

def build_row(ticker, today, now):
    profile = get_profile(ticker)
    is_etf = bool(profile and profile.get("quote_type") == "ETF")
    row = {
        "ticker": ticker,
        "name": profile.get("name") if profile else None,
        "is_etf": is_etf,
        "earnings": None,
        "news": [],
        "news_available": True,
    }
    # ETFs don't report earnings and have no company news of their own (see
    # the sentiment route's notes on TSX ETFs), so skip both lookups.
    if is_etf:
        return row

    source_ticker, source_profile = resolve_source(ticker, profile)
    row["earnings"] = upcoming_earnings(source_profile, today)
    if row["earnings"] is None:
        # Info only had the last report - ask the earnings calendar page
        # for the next one (an extra call, so only when it's needed).
        row["earnings"] = calendar_earnings(get_calendar_earnings(source_ticker), today)
    raw_news = get_news(source_ticker)
    if raw_news is None:
        row["news_available"] = False
    else:
        row["news"] = rank_news(raw_news, source_ticker, source_profile, now)
    return row


# PROTECTED + SCOPED: only the logged-in user's own holdings.
@router.get("/holdings/news")
def holdings_news(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    tickers = held_tickers(db.query(Trade).filter(Trade.user_id == current_user.id).all())
    if not tickers:
        return {"holdings": []}

    today = datetime.now(MARKET_TZ).date()
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(tickers))) as pool:
        rows = list(pool.map(lambda t: build_row(t, today, now), tickers))

    # NEW: the latest earnings recap per holding (see earnings_recaps.py),
    # so the expanded sidebar row can say "Reported Nov 17: ..." and link to
    # it. Only recaps this user was notified about, i.e. ones for a report
    # that happened while they held the stock.
    recent = (
        db.query(EarningsNotification, EarningsRecap)
        .join(EarningsRecap, EarningsNotification.recap_id == EarningsRecap.id)
        .filter(EarningsNotification.user_id == current_user.id,
                EarningsRecap.report_date >= today - timedelta(days=RECAP_SHOWN_DAYS))
        .order_by(EarningsRecap.report_date.desc())
        .all()
    )
    latest_recap = {}
    for notification, recap in recent:
        latest_recap.setdefault(notification.display_ticker, {
            "notification_id": notification.id,
            "report_date": recap.report_date.isoformat(),
            "headline": recap.headline,
            "sentiment": recap.sentiment,
            "unread": notification.read_at is None,
        })
    for row in rows:
        row["recap"] = latest_recap.get(row["ticker"])

    # Soonest earnings first - that's what you need to act on - then
    # everything without an announced date, alphabetically.
    rows.sort(key=lambda r: (r["earnings"] is None, r["earnings"]["date"] if r["earnings"] else "", r["ticker"]))
    return {"holdings": rows}
