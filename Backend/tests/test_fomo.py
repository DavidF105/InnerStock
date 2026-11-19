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


# --- NEW: cool-off ("what would waiting have done?") ----------------------------------

def test_cool_off_buys_at_the_first_session_that_scores_calm():
    closes = with_runup(DAY, 12, step=0.005)
    entry = closes[-1][1]
    # Afterwards it drifts sideways a little below the buy.
    closes += wiggle(weekdays_after(DAY, 12), start=entry * 0.97, step=0.005)
    cool = fomo.cool_off(closes, {}, [], DAY, entry)
    assert cool["cooled"] and 1 <= cool["sessions"] <= fomo.COOL_OFF_SESSIONS
    cooled_day = date.fromisoformat(cool["date"])
    assert cool["price"] == pytest.approx(dict(closes)[cooled_day], abs=1e-3)
    assert cool["change_pct"] < 0
    # It really is the FIRST calm session: every session before it was still hot.
    for d in weekdays_after(DAY, cool["sessions"] - 1):
        assert fomo.score_buy(trade_date=d, trade_time=None, price=None, quantity=None,
                              closes=closes, volumes={}, bench_closes=[])["band"] in fomo.HOT_BANDS
    assert cool["low_price"] <= cool["price"]


def test_cool_off_uses_the_tenth_session_if_it_never_calms_down():
    closes = with_runup(DAY, 12, step=0.005)
    entry, price = closes[-1][1], closes[-1][1]
    for d in weekdays_after(DAY, 12):  # +3% a day, every day
        price *= 1.03
        closes.append((d, round(price, 4)))
    cool = fomo.cool_off(closes, {}, [], DAY, entry)
    assert not cool["cooled"] and cool["sessions"] == fomo.COOL_OFF_SESSIONS
    assert cool["date"] == weekdays_after(DAY, 10)[-1].isoformat()
    assert cool["change_pct"] > 0  # waiting would have cost money


def test_cool_off_waits_for_ten_sessions_of_data():
    closes = with_runup(DAY, 12, step=0.005) + wiggle(weekdays_after(DAY, 9))
    assert fomo.cool_off(closes, {}, [], DAY, closes[69][1]) is None


OLD_DAY = sessions_ending(date.today() - timedelta(days=30), 1)[0]


def hot_then_cheaper(day):
    closes = with_runup(day, 12, step=0.005)
    entry = closes[-1][1]
    return closes + wiggle(weekdays_after(day, 14), start=entry * 0.95, step=0.005), entry


def test_hot_buys_get_the_cool_off_and_it_counts_in_the_record(client, alice, history):
    history["NVDA"], entry = hot_then_cheaper(OLD_DAY)
    history["KO"] = wiggle(sessions_ending(OLD_DAY, 70)) + wiggle(weekdays_after(OLD_DAY, 14))
    log_trade(client, alice, "NVDA", "buy", 10, entry, trade_date=OLD_DAY)
    log_trade(client, alice, "KO", "buy", 10, dict(history["KO"])[OLD_DAY], trade_date=OLD_DAY)

    body = client.get("/fomo/summary", headers=alice).json()
    waiting = body["record"]["waiting"]
    assert waiting["checked"] == 1  # the calm KO buy isn't counted
    ledger = body["waiting_ledger"]
    assert [row["ticker"] for row in ledger] == ["NVDA"]
    cool = ledger[0]["cool_off"]
    assert cool["change_pct"] < 0 and cool["saved"] > 0
    assert waiting["saved"] == pytest.approx(-10 * entry * cool["change_pct"] / 100, abs=0.01)
    assert "cooled off would have saved" in body["message"]


def test_older_checks_get_the_cool_off_filled_in(client, alice, history, db_session_factory):
    import json
    from datetime import datetime
    history["NVDA"], entry = hot_then_cheaper(OLD_DAY)
    log_trade(client, alice, "NVDA", "buy", 10, entry, trade_date=OLD_DAY)

    # As if it was checked before the cool-off existed, two days ago.
    db = db_session_factory()
    trade = db.query(Trade).one()
    blob = json.loads(trade.fomo_signals)
    blob.pop("cool_off")
    trade.fomo_signals = json.dumps(blob)
    trade.fomo_checked_at = datetime.utcnow() - timedelta(days=2)
    db.commit()
    db.close()

    assert client.get("/fomo/summary", headers=alice).json()["record"]["waiting"]["checked"] == 1


# --- NEW: "Wait 3 days" ------------------------------------------------------------------

def test_wait_instead_of_buying_then_get_the_answer(client, alice, bob, history, db_session_factory):
    from models import FomoWait
    today = date.today()
    history["NVDA"] = with_runup(today, 12, step=0.005)

    created = client.post("/fomo/waits", headers=alice, json={"ticker": "nvda", "dollars": 1000})
    assert created.status_code == 201, created.text
    wait = created.json()
    assert wait["ticker"] == "NVDA" and wait["status"] == "waiting" and wait["band"] in fomo.HOT_BANDS
    assert wait["quantity"] == pytest.approx(1000 / wait["price"], rel=1e-3)
    assert wait["remind_on"] == fomo.add_sessions(date.fromisoformat(wait["check_date"]), 3).isoformat()
    assert "Waiting on NVDA" in wait["message"]
    # One open wait per stock.
    assert client.post("/fomo/waits", headers=alice, json={"ticker": "NVDA"}).status_code == 409
    # Nothing to announce yet.
    notes = client.get("/notifications", headers=alice).json()
    assert notes["waits"] == [] and notes["unread_count"] == 0

    # Time passes: pretend the check was made 8 sessions ago.
    checked = sessions_ending(today, 9)[0]
    answer_day, answer_close = [c for c in history["NVDA"] if c[0] > checked][2]
    db = db_session_factory()
    row = db.query(FomoWait).one()
    row.check_date, row.remind_on = checked, fomo.add_sessions(checked, 3)
    db.commit()
    db.close()

    notes = client.get("/notifications", headers=alice).json()
    assert notes["unread_count"] == 1
    answered = notes["waits"][0]
    assert answered["status"] == "answered" and answered["unread"]
    assert answered["result_date"] == answer_day.isoformat()
    assert answered["result_price"] == pytest.approx(answer_close, abs=1e-3)
    assert answered["change_pct"] == pytest.approx((answer_close - wait["price"]) / wait["price"] * 100, abs=0.01)
    assert answered["message"].startswith("You almost bought NVDA")

    client.post("/notifications/read-all", headers=alice)
    assert client.get("/notifications", headers=alice).json()["unread_count"] == 0
    assert client.get("/fomo/summary", headers=alice).json()["record"]["waits"]["resolved"] == 1

    # Bob can't see or remove Alice's wait.
    assert client.get("/fomo/waits", headers=bob).json()["waits"] == []
    assert client.delete(f"/fomo/waits/{wait['id']}", headers=bob).status_code == 404
    assert client.delete(f"/fomo/waits/{wait['id']}", headers=alice).status_code == 200
    assert client.get("/fomo/waits", headers=alice).json()["waits"] == []


def test_wait_needs_a_stock_with_history(client, alice):
    assert client.post("/fomo/waits", headers=alice, json={"ticker": "NOPE"}).status_code == 404
    assert client.post("/fomo/waits", json={"ticker": "NVDA"}).status_code == 401


def test_add_sessions_skips_weekends():
    friday = date(2026, 11, 13)
    assert fomo.add_sessions(friday, 3) == date(2026, 11, 18)


def test_a_failed_price_download_is_retried_not_stored_as_unknown(client, alice, history):
    # No prices at all (Yahoo refusing the download) -> left unchecked.
    trade = log_trade(client, alice, "NVDA", "buy", 10, 100.0, trade_date=DAY)
    assert trade["fomo_band"] is None
    summary = client.get("/fomo/summary", headers=alice).json()
    assert summary["record"]["buys"] == 1 and summary["record"]["unchecked"] == 1

    # Prices come back: the very next request scores it.
    history["NVDA"] = with_runup(DAY, 12, step=0.005)
    record = client.get("/fomo/summary", headers=alice).json()["record"]
    assert record["scored_buys"] == 1 and record["unchecked"] == 0


def test_too_little_history_is_stored_as_unknown(client, alice, history):
    history["NEWCO"] = wiggle(sessions_ending(DAY, 10))
    trade = log_trade(client, alice, "NEWCO", "buy", 10, history["NEWCO"][-1][1], trade_date=DAY)
    assert trade["fomo_band"] == "unknown"


def test_a_tiny_move_after_waiting_is_no_real_difference():
    from models import FomoWait
    w = FomoWait(ticker="COST", price=1065.59, quantity=1.4, check_date=date(2026, 11, 5),
                 remind_on=date(2026, 11, 10), result_date=date(2026, 11, 10), result_price=1066.14)
    assert fomo.serialize_wait(w)["verdict"] == "same"
    assert "no real difference" in fomo.wait_message(w)
    w.result_price = 1000.0
    assert fomo.serialize_wait(w)["verdict"] == "paid_off" and "waiting saved you" in fomo.wait_message(w)
