# /holdings/news: upcoming earnings and company-specific news for each held
# stock. yfinance is replaced by the market_events fixture (conftest.py).
from datetime import datetime, time, timedelta, timezone

import holdings_news
from conftest import log_trade

ET = holdings_news.MARKET_TZ


def et_stamp(days_from_today, hour=16, minute=30):
    """Unix timestamp for a market-time moment `days_from_today` days away."""
    day = datetime.now(ET).date() + timedelta(days=days_from_today)
    return datetime.combine(day, time(hour, minute), tzinfo=ET).timestamp()


def stock(name, start_days=None, end_days=None, hour=16, estimate=False, quote_type="EQUITY", short_name=None):
    return {
        "name": name,
        "short_name": short_name or name.upper(),
        "quote_type": quote_type,
        "earnings_start": et_stamp(start_days, hour) if start_days is not None else None,
        "earnings_end": et_stamp(end_days if end_days is not None else start_days, hour) if start_days is not None else None,
        "earnings_ts": None,
        "earnings_is_estimate": estimate,
    }


def article(title, summary="", hours_ago=1, url=None):
    published = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"content": {
        "title": title,
        "summary": summary,
        "pubDate": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": {"displayName": "Reuters"},
        "canonicalUrl": {"url": url or f"https://example.com/{abs(hash(title))}"},
    }}


def get_rows(client, headers):
    response = client.get("/holdings/news", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["holdings"]


def test_only_currently_held_tickers_are_listed(client, alice, market_events):
    log_trade(client, alice, "AAPL", "buy", 5, 100)
    log_trade(client, alice, "MSFT", "buy", 2, 300)
    log_trade(client, alice, "MSFT", "sell", 2, 310)
    assert [r["ticker"] for r in get_rows(client, alice)] == ["AAPL"]


def test_no_holdings_returns_empty_list(client, alice):
    assert get_rows(client, alice) == []


def test_upcoming_earnings_sorted_soonest_first(client, alice, market_events):
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=20)
    market_events.profiles["SHOP.TO"] = stock("Shopify Inc.", start_days=3, hour=7)
    market_events.profiles["RY.TO"] = stock("Royal Bank of Canada", start_days=-10)  # already reported
    for ticker in ("AAPL", "SHOP.TO", "RY.TO"):
        log_trade(client, alice, ticker, "buy", 1, 100)

    rows = get_rows(client, alice)
    assert [r["ticker"] for r in rows] == ["SHOP.TO", "AAPL", "RY.TO"]
    shop, aapl, ry = rows
    assert shop["earnings"]["days_until"] == 3
    assert shop["earnings"]["timing"] == "before_open"
    assert aapl["earnings"]["timing"] == "after_close"
    assert ry["earnings"] is None  # past report, next one not announced yet
    assert shop["name"] == "Shopify Inc."


def test_estimated_earnings_window_has_no_time_of_day(client, alice, market_events):
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=60, end_days=64)
    log_trade(client, alice, "AAPL", "buy", 1, 100)
    earnings = get_rows(client, alice)[0]["earnings"]
    assert earnings["is_estimate"] is True
    assert earnings["timing"] is None
    assert earnings["days_until"] == 60


def test_etfs_have_no_earnings_and_skip_news(client, alice, market_events):
    market_events.profiles["VFV.TO"] = stock("Vanguard S&P 500 Index ETF", quote_type="ETF")
    market_events.news["VFV.TO"] = [article("Vanguard S&P 500 Index ETF hits record")]
    log_trade(client, alice, "VFV.TO", "buy", 10, 100)
    row = get_rows(client, alice)[0]
    assert row["is_etf"] is True
    assert row["earnings"] is None
    assert row["news"] == []


def test_news_keeps_only_company_stories_and_ranks_important_first(client, alice, market_events):
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=20)
    market_events.news["AAPL"] = [
        article("Dow futures slip as traders await jobs report", hours_ago=1),           # not about Apple
        article("Markets wrap", summary="Apple and Microsoft led the gains.", hours_ago=2),  # passing mention
        article("Apple opens a new store in Toronto", hours_ago=3),
        article("Apple beats earnings estimates on iPhone strength", hours_ago=20),
        article("Analyst downgrades AAPL on China risk", hours_ago=5),
        article("Apple beats earnings estimates on iPhone strength", hours_ago=21),       # syndicated duplicate
        article("Apple recall announced", hours_ago=24 * 9),                               # too old
    ]
    log_trade(client, alice, "AAPL", "buy", 1, 100)

    titles = [n["title"] for n in get_rows(client, alice)[0]["news"]]
    # Important title mentions first (newest first among equals), then the
    # plain title mention; passing mentions only fill leftover slots.
    assert titles == [
        "Analyst downgrades AAPL on China risk",
        "Apple beats earnings estimates on iPhone strength",
        "Apple opens a new store in Toronto",
    ]


def test_news_entries_carry_link_publisher_and_time(client, alice, market_events):
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=20)
    market_events.news["AAPL"] = [article("Apple raises dividend", url="https://example.com/apple-dividend")]
    log_trade(client, alice, "AAPL", "buy", 1, 100)
    news = get_rows(client, alice)[0]["news"]
    assert news[0]["url"] == "https://example.com/apple-dividend"
    assert news[0]["publisher"] == "Reuters"
    assert news[0]["published_at"].endswith("+00:00")


def test_older_flat_news_format_is_understood(client, alice, market_events):
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=20)
    market_events.news["AAPL"] = [{
        "title": "Apple sued over App Store fees",
        "link": "https://example.com/flat",
        "publisher": "AP",
        "providerPublishTime": int(datetime.now(timezone.utc).timestamp()) - 3600,
    }]
    log_trade(client, alice, "AAPL", "buy", 1, 100)
    assert get_rows(client, alice)[0]["news"][0]["url"] == "https://example.com/flat"


def test_cdr_reads_earnings_and_news_from_the_us_listing(client, alice, market_events):
    market_events.profiles["AAPL.TO"] = stock("Apple Inc.", start_days=-200, short_name="APPLE CDR (CAD HEDGED)")
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=12)
    market_events.news["AAPL"] = [article("Apple unveils new chips")]
    log_trade(client, alice, "AAPL.TO", "buy", 3, 30)

    row = get_rows(client, alice)[0]
    assert row["ticker"] == "AAPL.TO"
    assert row["earnings"]["days_until"] == 12
    assert [n["title"] for n in row["news"]] == ["Apple unveils new chips"]


def test_tsx_stock_is_never_mapped_to_an_unrelated_us_ticker(client, alice, market_events):
    # T.TO is Telus; bare T is AT&T. Not a CDR, so no fallback.
    market_events.profiles["T.TO"] = stock("TELUS Corporation", start_days=-5)
    market_events.profiles["T"] = stock("AT&T Inc.", start_days=4)
    log_trade(client, alice, "T.TO", "buy", 10, 20)
    assert get_rows(client, alice)[0]["earnings"] is None


def test_failed_news_lookup_is_reported_not_an_error(client, alice, market_events):
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=20)
    # No market_events.news["AAPL"] -> the loader returns None (failed lookup)
    log_trade(client, alice, "AAPL", "buy", 1, 100)
    row = get_rows(client, alice)[0]
    assert row["news_available"] is False
    assert row["earnings"]["days_until"] == 20


def test_profile_lookups_are_cached(client, alice, market_events, monkeypatch):
    calls = []
    monkeypatch.setattr(holdings_news, "load_profile", lambda t: calls.append(t) or stock("Apple Inc.", start_days=5))
    log_trade(client, alice, "AAPL", "buy", 1, 100)
    get_rows(client, alice)
    get_rows(client, alice)
    assert calls == ["AAPL"]


def test_goog_cdr_gets_alphabets_next_date_from_the_earnings_calendar(client, alice, market_events):
    # The real Nov 2026 situation: GOOG's info only had the Oct 28 report
    # it had just made; the earnings calendar page already listed Feb 3.
    market_events.profiles["GOOG.TO"] = stock("Alphabet Inc.", start_days=-400, short_name="ALPHABET CDR (CAD HEDGED)")
    market_events.profiles["GOOG"] = stock("Alphabet Inc.", start_days=-18)
    market_events.calendar["GOOG"] = [{"ts": et_stamp(d)} for d in (80, -18, -116)]
    log_trade(client, alice, "GOOG.TO", "buy", 4, 40)

    earnings = get_rows(client, alice)[0]["earnings"]
    assert earnings["days_until"] == 80
    assert earnings["is_estimate"] is True  # not confirmed by the company yet
    assert earnings["timing"] is None


def test_calendar_is_only_asked_when_info_has_no_upcoming_date(client, alice, market_events, monkeypatch):
    calls = []
    monkeypatch.setattr(holdings_news, "load_calendar_earnings", lambda t: calls.append(t) or [{"ts": et_stamp(99)}])
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=10)
    log_trade(client, alice, "AAPL", "buy", 1, 100)
    assert get_rows(client, alice)[0]["earnings"]["days_until"] == 10
    assert calls == []


def test_berkshire_cdr_maps_to_the_dashed_class_b_listing(client, alice, market_events):
    # Yahoo has no bare "BRK" - Berkshire's B shares are "BRK-B".
    market_events.profiles["BRK.TO"] = stock("Berkshire Hathaway Inc.", start_days=-300,
                                             short_name="BERKSHIRE HATHAWAY CDR (CAD HED")
    market_events.profiles["BRK-B"] = stock("Berkshire Hathaway Inc.", start_days=30)
    log_trade(client, alice, "BRK.TO", "buy", 2, 30)
    assert get_rows(client, alice)[0]["earnings"]["days_until"] == 30
