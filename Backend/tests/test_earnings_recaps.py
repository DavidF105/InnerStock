# Earnings recaps + notifications (earnings_recaps.py). yfinance comes from
# the market_events / history fixtures, Claude from recap_writer.
from datetime import datetime, timedelta
from types import SimpleNamespace

import earnings_recaps
from conftest import log_trade
from test_holdings_news import article, et_stamp, stock

ET = earnings_recaps.hn.MARKET_TZ


def et_day(days_from_today):
    return datetime.now(ET).date() + timedelta(days=days_from_today)


def reported(market_events, ticker, name, days_ago=2, eps_actual=2.10, eps_estimate=2.00, hour=16):
    """A company that reported `days_ago` days ago (after the close by default)."""
    market_events.profiles[ticker] = stock(name, start_days=75)
    market_events.calendar[ticker] = [
        {"ts": et_stamp(75), "eps_estimate": 2.40, "eps_actual": None, "surprise_pct": None},
        {"ts": et_stamp(-days_ago, hour=hour), "eps_estimate": eps_estimate, "eps_actual": eps_actual,
         "surprise_pct": 5.0 if eps_actual is not None else None},
    ]


def get_notifications(client, headers):
    response = client.get("/notifications", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_holder_gets_a_recap_of_a_report(client, alice, market_events, history, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.")
    market_events.news["AAPL"] = [article("Apple beats earnings estimates on iPhone strength")]
    # After-close report two days ago: report-day close 100 -> next day 110 -> today 121
    history["AAPL"] = [(et_day(-3), 98.0), (et_day(-2), 100.0), (et_day(-1), 110.0), (et_day(0), 121.0)]
    log_trade(client, alice, "AAPL", "buy", 5, 90)

    data = get_notifications(client, alice)
    assert data["unread_count"] == 1
    note = data["notifications"][0]
    assert note["ticker"] == "AAPL"
    assert note["headline"] == "Results beat estimates"
    assert note["eps_actual"] == 2.10 and note["eps_estimate"] == 2.00
    assert round(note["reaction_pct"], 1) == 10.0
    assert round(note["move_since_pct"], 1) == 10.0
    assert note["sources"][0]["title"] == "Apple beats earnings estimates on iPhone strength"
    assert note["unread"] is True

    facts = recap_writer.calls[0]
    assert facts["report_timing"] == "after market close"
    assert facts["company_name"] == "Apple Inc."


def test_before_open_report_measures_from_the_previous_close(client, alice, market_events, history, recap_writer):
    reported(market_events, "JPM", "JPMorgan Chase & Co.", hour=7)
    history["JPM"] = [(et_day(-3), 200.0), (et_day(-2), 190.0), (et_day(-1), 195.0)]
    log_trade(client, alice, "JPM", "buy", 1, 180)
    get_notifications(client, alice)
    assert round(recap_writer.calls[0]["reaction_pct"], 1) == -5.0


def test_recap_is_written_once_and_shared_by_every_holder(client, alice, bob, market_events, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.")
    log_trade(client, alice, "AAPL", "buy", 5, 90)
    log_trade(client, bob, "AAPL", "buy", 1, 95)
    assert get_notifications(client, alice)["unread_count"] == 1
    assert get_notifications(client, bob)["unread_count"] == 1
    get_notifications(client, alice)  # reloading doesn't duplicate anything
    assert len(recap_writer.calls) == 1
    assert len(get_notifications(client, alice)["notifications"]) == 1


def test_cdr_reads_the_us_report(client, alice, market_events, recap_writer):
    market_events.profiles["GOOG.TO"] = stock("Alphabet Inc.", start_days=-400, short_name="ALPHABET CDR (CAD HEDGED)")
    reported(market_events, "GOOG", "Alphabet Inc.")
    log_trade(client, alice, "GOOG.TO", "buy", 4, 40)
    note = get_notifications(client, alice)["notifications"][0]
    assert (note["ticker"], note["source_ticker"]) == ("GOOG.TO", "GOOG")


def test_holding_cdr_and_us_listing_gives_one_notification(client, alice, market_events, recap_writer):
    # Same company, same report: one recap and one notification, not two copies.
    market_events.profiles["GOOG.TO"] = stock("Alphabet Inc.", start_days=-400, short_name="ALPHABET CDR (CAD HEDGED)")
    reported(market_events, "GOOG", "Alphabet Inc.")
    log_trade(client, alice, "GOOG.TO", "buy", 4, 40)
    log_trade(client, alice, "GOOG", "buy", 1, 150)
    assert len(get_notifications(client, alice)["notifications"]) == 1
    assert len(recap_writer.calls) == 1


def test_no_recap_until_the_reported_eps_is_out(client, alice, market_events, recap_writer):
    # Expected date passed but Yahoo has no actual EPS - maybe the date was
    # only an estimate and the company hasn't reported. Don't invent a recap.
    reported(market_events, "AAPL", "Apple Inc.", days_ago=1, eps_actual=None)
    log_trade(client, alice, "AAPL", "buy", 5, 90)
    assert get_notifications(client, alice)["notifications"] == []
    assert recap_writer.calls == []


def test_old_reports_are_ignored(client, alice, market_events, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.", days_ago=earnings_recaps.REPORT_LOOKBACK_DAYS + 3)
    log_trade(client, alice, "AAPL", "buy", 5, 90)
    assert get_notifications(client, alice)["notifications"] == []


def test_no_recap_if_bought_after_the_report(client, alice, market_events, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.", days_ago=3)
    log_trade(client, alice, "AAPL", "buy", 5, 90, trade_date=et_day(-1))
    assert get_notifications(client, alice)["notifications"] == []


def test_selling_out_and_rebuying_after_the_report_counts_from_the_rebuy(client, alice, market_events, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.", days_ago=3)
    log_trade(client, alice, "AAPL", "buy", 5, 90, trade_date=et_day(-30))
    log_trade(client, alice, "AAPL", "sell", 5, 95, trade_date=et_day(-10))
    log_trade(client, alice, "AAPL", "buy", 2, 99, trade_date=et_day(-1))
    assert get_notifications(client, alice)["notifications"] == []


def test_etfs_get_no_recaps(client, alice, market_events, recap_writer):
    market_events.profiles["VFV.TO"] = stock("Vanguard S&P 500 Index ETF", quote_type="ETF")
    market_events.calendar["VFV.TO"] = [{"ts": et_stamp(-2), "eps_estimate": 1, "eps_actual": 1, "surprise_pct": 0}]
    log_trade(client, alice, "VFV.TO", "buy", 10, 100)
    assert get_notifications(client, alice)["notifications"] == []
    assert recap_writer.calls == []


def test_claude_failure_is_retried_on_the_next_load(client, alice, market_events, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.")
    log_trade(client, alice, "AAPL", "buy", 5, 90)
    recap_writer.result = None
    assert get_notifications(client, alice)["notifications"] == []
    recap_writer.result = {"headline": "Solid quarter", "what_happened": "x", "what_it_means": "y", "sentiment": "neutral"}
    assert get_notifications(client, alice)["notifications"][0]["headline"] == "Solid quarter"
    assert len(recap_writer.calls) == 2


def test_new_recaps_per_load_are_capped(client, alice, market_events, recap_writer):
    names = {"AAPL": "Apple Inc.", "MSFT": "Microsoft Corporation", "NVDA": "NVIDIA Corporation", "AMZN": "Amazon.com, Inc."}
    for ticker, name in names.items():
        reported(market_events, ticker, name)
        log_trade(client, alice, ticker, "buy", 1, 100)
    assert len(get_notifications(client, alice)["notifications"]) == earnings_recaps.MAX_RECAPS_PER_REQUEST
    assert len(get_notifications(client, alice)["notifications"]) == 4


def test_marking_read(client, alice, bob, market_events, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.")
    log_trade(client, alice, "AAPL", "buy", 5, 90)
    note_id = get_notifications(client, alice)["notifications"][0]["id"]

    assert client.post(f"/notifications/{note_id}/read", headers=bob).status_code == 404
    assert get_notifications(client, alice)["unread_count"] == 1
    assert client.post(f"/notifications/{note_id}/read", headers=alice).status_code == 200
    assert get_notifications(client, alice)["unread_count"] == 0


def test_read_all(client, alice, market_events, recap_writer):
    for ticker, name in {"AAPL": "Apple Inc.", "MSFT": "Microsoft Corporation"}.items():
        reported(market_events, ticker, name)
        log_trade(client, alice, ticker, "buy", 1, 100)
    assert get_notifications(client, alice)["unread_count"] == 2
    assert client.post("/notifications/read-all", headers=alice).status_code == 200
    data = get_notifications(client, alice)
    assert data["unread_count"] == 0
    assert all(not n["unread"] for n in data["notifications"])


def test_sidebar_row_links_to_its_recap(client, alice, market_events, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.")
    log_trade(client, alice, "AAPL", "buy", 5, 90)
    assert client.get("/holdings/news", headers=alice).json()["holdings"][0]["recap"] is None  # not written yet
    note_id = get_notifications(client, alice)["notifications"][0]["id"]

    recap = client.get("/holdings/news", headers=alice).json()["holdings"][0]["recap"]
    assert recap["notification_id"] == note_id
    assert recap["headline"] == "Results beat estimates"
    assert recap["unread"] is True


# --- Parsing Claude's reply --------------------------------------------------------

def _message(blocks, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(**b) for b in blocks])


GOOD = '{"headline": "Beat", "what_happened": "a", "what_it_means": "b", "sentiment": "positive"}'


def test_parse_recap_reads_the_json_answer():
    assert earnings_recaps.parse_recap(_message([{"type": "text", "text": GOOD}]))["headline"] == "Beat"


def test_parse_recap_keeps_only_text_after_a_fallback():
    message = _message([
        {"type": "text", "text": '{"headline": "partial'},
        {"type": "fallback"},
        {"type": "text", "text": GOOD},
    ])
    assert earnings_recaps.parse_recap(message)["sentiment"] == "positive"


def test_parse_recap_rejects_refusals_and_bad_output():
    assert earnings_recaps.parse_recap(_message([], stop_reason="refusal")) is None
    assert earnings_recaps.parse_recap(_message([{"type": "text", "text": GOOD}], stop_reason="max_tokens")) is None
    assert earnings_recaps.parse_recap(_message([{"type": "text", "text": "not json"}])) is None
    assert earnings_recaps.parse_recap(_message([{"type": "text", "text": '{"headline": "x"}'}])) is None
