# The dashboard's portfolio chart (portfolio_history.py): the portfolio's
# value rebuilt day by day from trades, money put in, and the same
# deposits made into the S&P 500 instead.
from datetime import date, timedelta

import pytest

import portfolio_history as ph
from conftest import log_trade


D0 = date(2026, 3, 2)  # a Monday


def days(n):
    return [D0 + timedelta(days=i) for i in range(n)]


def test_value_follows_shares_times_close():
    closes = {"AAPL": [(d, 100.0 + i) for i, d in enumerate(days(5))],
              "SPY": [(d, 500.0) for d in days(5)]}
    points = ph.build_series([(D0, "AAPL", "buy", 10, 100.0)], closes)
    assert [p["value"] for p in points] == [1000.0, 1010.0, 1020.0, 1030.0, 1040.0]
    assert all(p["invested"] == 1000.0 for p in points)
    # The S&P 500 didn't move, so the index version stays at what went in.
    assert all(p["sp500"] == 1000.0 for p in points)


def test_sells_reduce_shares_and_invested():
    closes = {"AAPL": [(d, 100.0) for d in days(4)], "SPY": [(d, 500.0) for d in days(4)]}
    trades = [(D0, "AAPL", "buy", 10, 100.0), (D0 + timedelta(days=2), "AAPL", "sell", 4, 100.0)]
    points = ph.build_series(trades, closes)
    assert [p["value"] for p in points] == [1000.0, 1000.0, 600.0, 600.0]
    assert [p["invested"] for p in points] == [1000.0, 1000.0, 600.0, 600.0]
    assert points[-1]["sp500"] == pytest.approx(600.0)


def test_the_index_version_grows_with_the_index():
    closes = {"AAPL": [(d, 100.0) for d in days(3)],
              "SPY": [(D0, 500.0), (D0 + timedelta(days=1), 550.0), (D0 + timedelta(days=2), 600.0)]}
    points = ph.build_series([(D0, "AAPL", "buy", 10, 100.0)], closes)
    assert [p["sp500"] for p in points] == [1000.0, 1100.0, 1200.0]
    assert points[-1]["value"] == 1000.0  # the stock went nowhere: the index won


def test_canadian_stocks_use_the_canadian_sp500_fund():
    closes = {"RY.TO": [(d, 200.0) for d in days(2)],
              "VFV.TO": [(D0, 100.0), (D0 + timedelta(days=1), 110.0)]}
    points = ph.build_series([(D0, "RY.TO", "buy", 5, 200.0)], closes)
    assert points[-1]["sp500"] == pytest.approx(1100.0)


def test_missing_benchmark_prices_hide_the_index_line():
    closes = {"AAPL": [(d, 100.0) for d in days(2)]}
    points = ph.build_series([(D0, "AAPL", "buy", 1, 100.0)], closes)
    assert points[-1]["sp500"] is None and points[-1]["value"] == 100.0


def test_route_builds_the_history_from_the_users_trades(client, alice, bob, history):
    start = date.today() - timedelta(days=20)
    history["NVDA"] = [(start + timedelta(days=i), 100.0 + i) for i in range(21)]
    history["SPY"] = [(start + timedelta(days=i), 500.0) for i in range(21)]
    log_trade(client, alice, "NVDA", "buy", 10, 100.0, trade_date=start)

    body = client.get("/portfolio/history", headers=alice).json()
    assert body["points"][0]["date"] == start.isoformat()
    assert body["points"][-1]["value"] == pytest.approx(10 * 120.0)
    assert body["points"][-1]["invested"] == 1000.0
    assert body["mixed_currency"] is False
    assert client.get("/portfolio/history", headers=bob).json()["points"] == []
