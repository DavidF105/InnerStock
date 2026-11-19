# The FOMO check (fomo.py): "was this buy a chase?" - the score itself,
# storing it on trades, the personal record (FOMO tax, patience test,
# self-labels, shrinkage) and the routes.
from datetime import date, time, timedelta

import pytest

import fomo
from conftest import log_trade
from models import Trade


# --- Price helpers ------------------------------------------------------------

def sessions_ending(day, count):
    """`count` weekdays ending on `day` (inclusive), oldest first."""
    days, d = [], day
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def weekdays_after(day, count):
    days, d = [], day
    while len(days) < count:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return days


def wiggle(days, start=100.0, step=0.01):
    """A stock that goes +step, -step, +step... - its normal daily move is `step`."""
    out, price = [], start
    for i, d in enumerate(days):
        out.append((d, round(price, 4)))
        price *= (1 + step) if i % 2 == 0 else (1 - step)
    return out


def with_runup(trade_day, total_pct, step=0.01, sessions=70):
    """Calm history, then the last 5 sessions climb `total_pct` into the trade day."""
    days = sessions_ending(trade_day, sessions)
    calm = wiggle(days[:-5], step=step)
    base = calm[-1][1]
    climb = [(d, round(base * (1 + total_pct / 100) ** ((i + 1) / 5), 4)) for i, d in enumerate(days[-5:])]
    return calm + climb


def score(closes, day, price=None, volumes=None, bench=None, trade_time=time(11, 0), quantity=10, habits=fomo.NO_HABITS):
    price = price if price is not None else dict(closes)[day]
    return fomo.score_buy(trade_date=day, trade_time=trade_time, price=price, quantity=quantity,
                          closes=closes, volumes=volumes or {}, bench_closes=bench or [], habits=habits)


# A recent weekday with room for a week of closes after it.
DAY = sessions_ending(date.today() - timedelta(days=14), 1)[0]


# --- The score -------------------------------------------------------------------

def test_the_same_move_means_different_things_for_calm_and_wild_stocks():
    # +8% in a week: huge for a stock that moves 0.5% a day, normal for one
    # that moves 4% a day. The old fixed 8% rule treated them the same.
    calm = score(with_runup(DAY, 8, step=0.005), DAY)
    wild = score(with_runup(DAY, 8, step=0.04), DAY)
    assert any(s["key"] == "runup" for s in calm["signals"])
    assert calm["stats"]["z"] >= fomo.RUNUP_STRONG_Z
    assert not any(s["key"] == "runup" for s in wild["signals"])
    assert wild["band"] == "calm"


def test_a_move_the_whole_market_made_is_not_chasing():
    stock = with_runup(DAY, 8, step=0.005)
    market = with_runup(DAY, 8, step=0.005)
    result = score(stock, DAY, bench=market)
    assert result["stats"]["excess_pct"] == pytest.approx(0, abs=0.5)
    assert not any(s["key"] == "runup" for s in result["signals"])


def test_sharp_drops_are_dip_buys_not_fomo():
    result = score(with_runup(DAY, -10, step=0.01), DAY)
    assert result["band"] == "dip"
    assert result["score"] == 0
    assert result["summary"].startswith("Dip buy")


def test_everything_firing_is_chasing():
    closes = with_runup(DAY, 12, step=0.01)
    volumes = {d: 1_000_000 for d, _ in closes}
    volumes[DAY] = 3_500_000
    result = score(closes, DAY, volumes=volumes, trade_time=time(9, 41))
    keys = {s["key"] for s in result["signals"]}
    assert {"runup", "near_high", "volume", "opening"} <= keys
    assert result["band"] == "chasing" and result["score"] >= fomo.CHASING_SCORE
    assert "9:41 AM" in next(s["detail"] for s in result["signals"] if s["key"] == "opening")
    assert result["summary"].startswith("Chasing:")


def test_a_one_day_jump_is_flagged():
    closes = wiggle(sessions_ending(DAY, 70), step=0.01)
    closes[-1] = (DAY, round(closes[-2][1] * 1.06, 4))  # +6% on the day = 6 normal days
    result = score(closes, DAY)
    assert any(s["key"] == "jump" for s in result["signals"])


def test_habits_count_too():
    closes = wiggle(sessions_ending(DAY, 70), step=0.01)
    price = closes[-1][1]
    habits = fomo.Habits(prior_buy_values=[500, 600, 700], prior_trade_count=6, traded_before=False,
                         on_watchlist=False, recent_sell=(DAY - timedelta(days=12), price * 0.9))
    result = score(closes, DAY, quantity=3000 / price, habits=habits)
    keys = {s["key"] for s in result["signals"]}
    assert keys == {"oversized", "new_name", "chase_back"}
    assert result["band"] == "calm"  # behaviour alone (25 points) isn't a chase
    assert "5.0x your usual buy" in next(s["detail"] for s in result["signals"] if s["key"] == "oversized")


def test_new_name_needs_some_history_first():
    closes = wiggle(sessions_ending(DAY, 70))
    first_ever = fomo.Habits([], 0, False, False, None)
    assert not any(s["key"] == "new_name" for s in score(closes, DAY, habits=first_ever)["signals"])


def test_too_little_history_is_unknown():
    result = score(wiggle(sessions_ending(DAY, 10)), DAY)
    assert result["band"] == "unknown" and result["score"] is None


def test_patience_test_uses_the_close_five_sessions_later():
    days = sessions_ending(DAY, 30) + weekdays_after(DAY, 6)
    closes = [(d, 100.0) for d in days]
    fifth_after = weekdays_after(DAY, 5)[-1]
    closes = [(d, 95.0 if d == fifth_after else c) for d, c in closes]
    assert fomo.week_later_pct(closes, DAY, 100.0) == -5.0
    assert fomo.week_later_pct(closes[:-3], DAY, 100.0) is None  # not a week yet


def test_split_fill_prices_are_measured_from_the_close():
    closes = wiggle(sessions_ending(DAY, 70))
    assert fomo.entry_price(closes, DAY, 5 * closes[-1][1]) == closes[-1][1]


# --- Stored on trades --------------------------------------------------------------

def test_logging_a_buy_runs_and_stores_the_check(client, alice, history):
    history["NVDA"] = with_runup(DAY, 12, step=0.005)
    trade = log_trade(client, alice, "NVDA", "buy", 10, history["NVDA"][-1][1], trade_date=DAY)
    assert trade["fomo_band"] in ("warm", "chasing")
    assert trade["fomo_score"] >= fomo.WARM_SCORE

    sell = log_trade(client, alice, "NVDA", "sell", 5, history["NVDA"][-1][1], trade_date=DAY)
    assert sell["fomo_band"] is None  # only buys are checked


def test_patience_test_is_filled_in_once_a_week_has_passed(client, alice, history, db_session_factory):
    days = sessions_ending(DAY, 70)
    history["AAPL"] = wiggle(days) + [(d, 90.0) for d in weekdays_after(DAY, 6)]
    log_trade(client, alice, "AAPL", "buy", 10, dict(history["AAPL"])[DAY], trade_date=DAY)
    db = db_session_factory()
    trade = db.query(Trade).one()
    assert trade.week_later_pct is not None and trade.week_later_pct < 0
    db.close()


def test_scorecard_shows_the_stored_check(client, alice, history):
    history["AMD"] = with_runup(DAY, 12, step=0.005)
    trade = log_trade(client, alice, "AMD", "buy", 10, history["AMD"][-1][1], trade_date=DAY,
                      review_date=DAY + timedelta(days=7))
    card = client.get(f"/thesis-reviews/{trade['id']}/scorecard", headers=alice).json()
    assert card["fomo"]["band"] in ("warm", "chasing")
    assert card["fomo"]["signals"]


# --- The personal record ------------------------------------------------------------

def grade(db_session_factory, trade_id, ret, bench, outcome="incorrect"):
    db = db_session_factory()
    t = db.get(Trade, trade_id)
    t.graded_return_pct, t.graded_benchmark_return_pct, t.outcome_tag = ret, bench, outcome
    db.commit()
    db.close()


def test_fomo_tax_counts_chasing_buys_against_the_market(client, alice, history, db_session_factory):
    history["TSLA"] = with_runup(DAY, 15, step=0.005)
    price = history["TSLA"][-1][1]
    chase = log_trade(client, alice, "TSLA", "buy", 10, price, trade_date=DAY)
    history["KO"] = wiggle(sessions_ending(DAY, 70), step=0.01)
    calm = log_trade(client, alice, "KO", "buy", 10, 100, trade_date=DAY)
    grade(db_session_factory, chase["id"], -10.0, 5.0)
    grade(db_session_factory, calm["id"], 8.0, 5.0, "correct")

    if chase["fomo_band"] != "chasing":  # make the test about the tax, not the exact score
        db = db_session_factory()
        db.get(Trade, chase["id"]).fomo_band = "chasing"
        db.commit()
        db.close()

    summary = client.get("/fomo/summary", headers=alice).json()
    record = summary["record"]
    assert record["fomo_tax"] == pytest.approx(10 * price * -15 / 100, abs=0.01)
    assert record["groups"]["chasing"]["avg_vs_market_pct"] == -15.0
    assert record["groups"]["calm"]["avg_vs_market_pct"] == 3.0
    # One trade of evidence, blended with a "no difference" prior of 0.
    assert record["expected_vs_market_pct"] == pytest.approx(-15 / (1 + fomo.SHRINK_K), abs=0.01)
    assert "averaged -15.0 points" in summary["message"]
    assert summary["recent_chasing"][0]["ticker"] == "TSLA"


def test_admitting_fomo_counts_as_chasing_and_checks_the_detector(client, alice, history):
    history["KO"] = wiggle(sessions_ending(DAY, 70), step=0.01)
    log_trade(client, alice, "KO", "buy", 10, 100, trade_date=DAY, reason_tags=["fomo"])
    record = client.get("/fomo/summary", headers=alice).json()["record"]
    assert record["groups"]["chasing"]["count"] == 1
    assert record["self_tagged"] == 1 and record["self_tagged_caught"] == 0  # the detector missed it


def test_other_users_chasing_record_is_the_prior(client, alice, bob, history, db_session_factory):
    history["KO"] = wiggle(sessions_ending(DAY, 70), step=0.01)
    for _ in range(fomo.MIN_POPULATION):
        t = log_trade(client, bob, "KO", "buy", 1, 100, trade_date=DAY, reason_tags=["fomo"])
        grade(db_session_factory, t["id"], -5.0, 5.0)
    record = client.get("/fomo/summary", headers=alice).json()["record"]
    assert record["expected_vs_market_pct"] == -10.0  # Alice has no record of her own yet
    assert record["expected_based_on"] == {"yours": 0, "others": fomo.MIN_POPULATION}
    assert record["groups"]["chasing"]["count"] == 0  # and none of Bob's trades are hers


def test_trading_patterns_split_chasing_from_calm(client, alice, history, db_session_factory):
    history["KO"] = wiggle(sessions_ending(DAY, 70), step=0.01)
    calm = log_trade(client, alice, "KO", "buy", 10, 100, trade_date=DAY)
    admitted = log_trade(client, alice, "KO", "buy", 10, 100, trade_date=DAY, reason_tags=["fomo"])
    grade(db_session_factory, calm["id"], 8.0, 5.0, "correct")
    grade(db_session_factory, admitted["id"], -8.0, 5.0)
    patterns = client.get("/trading-patterns", headers=alice).json()["fomo_vs_non_fomo"]
    assert patterns["fomo"]["total"] == 1 and patterns["fomo"]["correct_rate"] == 0
    assert patterns["non_fomo"]["total"] == 1 and patterns["non_fomo"]["correct_rate"] == 100


# --- Checking before buying -----------------------------------------------------------

def test_check_before_buying_saves_nothing(client, alice, history):
    today = date.today()
    history["NVDA"] = with_runup(today, 12, step=0.005)
    response = client.post("/fomo/check", headers=alice, json={"ticker": "nvda", "dollars": 1000})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ticker"] == "NVDA" and body["band"] in ("warm", "chasing")
    assert body["record_message"]
    assert client.get("/trades", headers=alice).json() == []


def test_check_rejects_bad_input(client, alice):
    assert client.post("/fomo/check", headers=alice, json={"ticker": "NOPE"}).status_code == 404
    assert client.post("/fomo/check", headers=alice, json={"ticker": "a b"}).status_code == 400
    future = (date.today() + timedelta(days=3)).isoformat()
    assert client.post("/fomo/check", headers=alice, json={"ticker": "AAPL", "trade_date": future}).status_code == 400
    assert client.post("/fomo/check", json={"ticker": "AAPL"}).status_code == 401


def test_inbox_shows_the_check_for_synced_buys(client, alice, history, db_session_factory):
    history["SHOP.TO"] = with_runup(DAY, 12, step=0.005)
    user_id = client.get("/auth/me", headers=alice).json()["id"]
    db = db_session_factory()
    db.add(Trade(user_id=user_id, ticker="SHOP.TO", action="buy", quantity=5,
                 price_per_share=history["SHOP.TO"][-1][1], trade_date=DAY, trade_time=time(9, 35), source="snaptrade"))
    db.commit()
    db.close()
    row = client.get("/trades/needs-thesis", headers=alice).json()["trades"][0]
    assert row["fomo"]["band"] == "chasing"
    assert any(s["key"] == "opening" for s in row["fomo"]["signals"])
