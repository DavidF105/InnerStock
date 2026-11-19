# Automatic grading (grading.auto_grade_due): due trades the numbers can
# judge are graded with the suggested grade when the review list loads.
from datetime import timedelta

from conftest import log_trade
from test_grading import REVIEWED, TODAY, TRADED, closes, grade


def due_list(client, headers):
    response = client.get("/thesis-reviews", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["reviews"]


def recent(client, headers):
    return client.get("/thesis-reviews/recent", headers=headers).json()["reviews"]


def winning_trade(client, headers, history, ticker="AAPL"):
    """A buy that rose 20% by its review date while the S&P 500 rose 5%."""
    trade = log_trade(client, headers, ticker, "buy", 10, 100, trade_date=TRADED, review_date=REVIEWED)
    history[ticker] = closes((0, 100), (20, 120), (30, 125))
    history["SPY"] = closes((0, 400), (20, 420), (30, 430))
    return trade


def test_due_trade_is_graded_automatically_at_its_review_date(client, alice, history):
    trade = winning_trade(client, alice, history)
    assert due_list(client, alice) == []

    graded = recent(client, alice)[0]
    assert graded["id"] == trade["id"]
    assert graded["outcome_tag"] == "correct"
    assert graded["auto_graded"] is True
    # Measured at the review date (+20%), not today (+25%).
    assert graded["graded_return_pct"] == 20.0
    assert graded["graded_benchmark_return_pct"] == 5.0


def test_not_graded_until_there_is_a_close_for_the_review_date(client, alice, history):
    # History stops before the review date (e.g. it's today and the market
    # hasn't closed) - grading "as of now" could change, so wait.
    log_trade(client, alice, "AAPL", "buy", 10, 100, trade_date=TRADED, review_date=REVIEWED)
    history["AAPL"] = closes((0, 100), (10, 110))
    history["SPY"] = closes((0, 400), (10, 410))
    assert len(due_list(client, alice)) == 1


def test_trades_the_numbers_cant_judge_are_left_for_the_user(client, alice, history):
    # A sell for cash isn't a call on the stock; a ticker with no history
    # can only be graded from memory.
    log_trade(client, alice, "AAPL", "buy", 10, 100, trade_date=TRADED - timedelta(days=5),
              review_date=TODAY + timedelta(days=90))
    log_trade(client, alice, "AAPL", "sell", 5, 100, trade_date=TRADED, review_date=REVIEWED, reason_tags=["need_cash"])
    log_trade(client, alice, "ZZZZ", "buy", 1, 10, trade_date=TRADED, review_date=REVIEWED)
    history["AAPL"] = closes((0, 100), (20, 120), (30, 125))
    history["SPY"] = closes((0, 400), (20, 420), (30, 430))
    assert sorted(r["ticker"] for r in due_list(client, alice)) == ["AAPL", "ZZZZ"]


def test_trades_not_due_yet_are_untouched(client, alice, history):
    log_trade(client, alice, "AAPL", "buy", 10, 100, trade_date=TRADED, review_date=TODAY + timedelta(days=5))
    history["AAPL"] = closes((0, 100), (20, 120))
    due_list(client, alice)
    assert recent(client, alice) == []


def test_user_can_change_an_auto_grade(client, alice, history):
    trade = winning_trade(client, alice, history)
    due_list(client, alice)

    # Flatly disagreeing with the numbers still needs a note...
    assert grade(client, alice, trade["id"], "incorrect").status_code == 400
    # ...and once changed, it's the user's grade, not an automatic one.
    assert grade(client, alice, trade["id"], "incorrect", "Right for the wrong reason - it was a takeover rumour").status_code == 200
    changed = recent(client, alice)[0]
    assert changed["outcome_tag"] == "incorrect"
    assert changed["auto_graded"] is False


def test_confirming_an_auto_grade_makes_it_the_users(client, alice, history):
    trade = winning_trade(client, alice, history)
    due_list(client, alice)
    assert grade(client, alice, trade["id"], "correct").status_code == 200
    assert recent(client, alice)[0]["auto_graded"] is False


def test_auto_grades_feed_the_reasons_stats(client, alice, history):
    log_trade(client, alice, "AAPL", "buy", 10, 100, trade_date=TRADED, review_date=REVIEWED, reason_tags=["momentum"])
    history["AAPL"] = closes((0, 100), (20, 120))
    history["SPY"] = closes((0, 400), (20, 420))
    due_list(client, alice)
    momentum = next(r for r in client.get("/trades/reasons-summary", headers=alice).json()["by_reason"] if r["tag"] == "momentum")
    assert momentum["graded"] == 1 and momentum["avg_return_pct"] == 20.0


def test_auto_grading_only_touches_the_users_own_trades(client, alice, bob, history):
    winning_trade(client, alice, history)
    assert due_list(client, bob) == []
    assert len(due_list(client, alice)) == 0
    assert recent(client, bob) == []
