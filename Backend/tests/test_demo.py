# "Try the demo" (demo.py): private throwaway accounts with sample data.
from datetime import date, datetime, timedelta

import pytest

import demo
from models import Trade, User


def start_demo(client, ip="1.2.3.4"):
    response = client.post("/auth/demo", headers={"X-Forwarded-For": ip})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_demo_logs_into_a_ready_made_journal(client):
    headers = start_demo(client)
    me = client.get("/auth/me", headers=headers).json()
    assert me["is_demo"] is True and me["name"] == "Demo investor"

    # Profile done, so no survey; holdings, inbox and watchlist all populated.
    assert client.get("/profile", headers=headers).status_code == 200
    held = {h["ticker"] for h in client.get("/holdings", headers=headers).json()["holdings"]}
    assert {"XEQT.TO", "AAPL", "NVDA", "SHOP.TO", "RY.TO", "MSFT", "ENB.TO", "GOOG.TO", "AMZN"} <= held
    assert "TSLA" not in held  # bought and fully sold: a closed position
    assert client.get("/trades/needs-thesis/count", headers=headers).json() == {"count": 4}
    assert len(client.get("/watchlist", headers=headers).json()["watchlist"]) == len(demo.DEMO_WATCHLIST)
    assert len(client.get("/trades", headers=headers).json()) >= 25


def test_demo_uses_real_prices_and_grades(client, history):
    # Real closes for every ticker on every day: trades are priced at that
    # day's close and "the user" has graded the ones they were meant to.
    today = date.today()
    for ticker in {t["ticker"] for t in demo.DEMO_TRADES} | {"SPY", "VFV.TO"}:
        history[ticker] = [(today - timedelta(days=d), 100 + (400 - d) * 0.1) for d in range(400, -1, -1)]
    headers = start_demo(client)

    body = client.get("/trades", headers=headers).json()
    trades = body["trades"] if isinstance(body, dict) else body
    nvda = next(t for t in trades if t["ticker"] == "NVDA" and t["thesis_text"])
    assert float(nvda["price_per_share"]) == round(100 + (400 - 250) * 0.1, 2)

    recent = client.get("/thesis-reviews/recent", headers=headers).json()["reviews"]
    assert len(recent) >= 3
    assert all(not r["auto_graded"] for r in recent)


def test_each_visitor_gets_a_private_copy(client):
    first, second = start_demo(client, "1.1.1.1"), start_demo(client, "2.2.2.2")
    assert client.get("/auth/me", headers=first).json()["id"] != client.get("/auth/me", headers=second).json()["id"]


def test_demo_starts_are_limited_per_address(client):
    for _ in range(demo.DEMOS_PER_IP_PER_HOUR):
        start_demo(client, "9.9.9.9")
    assert client.post("/auth/demo", headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429
    start_demo(client, "8.8.8.8")  # someone else is unaffected


def test_old_demos_are_deleted_when_a_new_one_starts(client, db_session_factory):
    start_demo(client, "1.1.1.1")
    db = db_session_factory()
    db.query(User).update({User.created_at: datetime.utcnow() - demo.DEMO_LIFETIME - timedelta(minutes=1)})
    db.commit()
    db.close()

    start_demo(client, "2.2.2.2")
    db = db_session_factory()
    assert db.query(User).filter(User.is_demo.is_(True)).count() == 1
    trade_owners = {t.user_id for t in db.query(Trade).all()}
    assert len(trade_owners) == 1  # the old demo's trades went with it
    db.close()


def test_demo_cleanup_never_touches_real_accounts(client, alice, db_session_factory):
    db = db_session_factory()
    db.query(User).update({User.created_at: datetime.utcnow() - timedelta(days=30)})
    db.commit()
    db.close()
    start_demo(client)
    assert client.get("/auth/me", headers=alice).status_code == 200


def test_demo_cannot_use_email_features(client, outbox):
    headers = start_demo(client)
    before = len(outbox)
    assert client.post("/auth/send-verification", headers=headers).status_code == 400
    me = client.get("/auth/me", headers=headers).json()
    assert client.post("/auth/forgot-password", json={"email": me["email"]}).json() == {"sent": True}
    assert len(outbox) == before  # nothing was actually sent


def test_demo_advisor_has_a_smaller_allowance(client, monkeypatch):
    import main
    calls = []
    monkeypatch.setattr(main, "enforce_ai_rate_limit", lambda user_id, bucket: calls.append(bucket))
    monkeypatch.setattr("advisor.run_advisor", lambda *a: ("ok", []))
    headers = start_demo(client)
    client.post("/chat", headers=headers, json={"message": "hi"})
    assert calls == ["demo_chat"]


def test_demo_fomo_buy_lands_on_a_real_hot_day(client, history):
    # A real run-up in the window: the "Fear of missing out" buy is moved
    # onto it (at the opening bell), so the FOMO check has a real chase.
    from test_fomo import sessions_ending, weekdays_after, with_runup
    today = date.today()
    hot = sessions_ending(today - timedelta(days=160), 1)[0]
    top = with_runup(hot, 15, step=0.005, sessions=220)
    history["AMD"] = top + [(d, top[-1][1]) for d in weekdays_after(hot, 200) if d <= today]
    headers = start_demo(client)

    amd = next(t for t in client.get("/trades", headers=headers).json() if t["ticker"] == "AMD")
    assert amd["trade_time"] == "09:40:00"
    assert hot - timedelta(days=7) <= date.fromisoformat(amd["trade_date"]) <= hot
    record = client.get("/fomo/summary", headers=headers).json()["record"]
    assert record["groups"]["chasing"]["count"] == 1 and record["self_tagged_caught"] == 1


def test_demo_shows_an_answered_wait_on_a_real_recent_hot_day(client, history):
    # The hottest recent day across the demo's stocks becomes a "Wait 3
    # days" that's already answered, so the bell has something to show.
    from test_fomo import sessions_ending, weekdays_after, with_runup
    today = date.today()
    hot = sessions_ending(today - timedelta(days=20), 1)[0]
    top = with_runup(hot, 15, step=0.005, sessions=220)
    history["NVDA"] = top + [(d, round(top[-1][1] * 0.97, 4)) for d in weekdays_after(hot, 30) if d <= today]
    headers = start_demo(client)

    notes = client.get("/notifications", headers=headers).json()
    assert notes["unread_count"] >= 1
    wait = notes["waits"][0]
    # The run-up's last day is the one where waiting paid off (-3% three
    # sessions later); earlier run-up days kept climbing, so they lose.
    assert wait["ticker"] == "NVDA" and wait["check_date"] == hot.isoformat()
    assert wait["verdict"] == "paid_off" and wait["change_pct"] == pytest.approx(-3, abs=0.1) and wait["saved"] > 0


def test_demo_wait_prefers_a_real_difference():
    import demo
    check = lambda score: {"score": score}
    tiny = ("COST", date(2026, 11, 5), 100, check(65), 0.1)
    up = ("AAPL", date(2026, 11, 3), 100, check(50), 3.5)
    down = ("AMD", date(2026, 9, 24), 100, check(35), -3.4)
    assert demo._pick_demo_wait([tiny, up, down]) == down    # waiting paid off beats a hotter day
    assert demo._pick_demo_wait([tiny, up]) == up            # a real move beats a non-event
    assert demo._pick_demo_wait([tiny]) == tiny              # last resort: the hottest day
    assert demo._pick_demo_wait([]) is None


def test_demo_covers_every_grading_path_and_reason_stat(client, history):
    today = date.today()
    for ticker in {t["ticker"] for t in demo.DEMO_TRADES} | {"SPY", "VFV.TO"}:
        history[ticker] = [(today - timedelta(days=d), 100 + (400 - d) * 0.1) for d in range(400, -1, -1)]
    headers = start_demo(client)

    # Graded against the numbers, with the note it required.
    trades = client.get("/trades", headers=headers).json()
    tsla_buy = next(t for t in trades if t["ticker"] == "TSLA" and t["action"] == "buy")
    assert tsla_buy["review_notes"] in demo.DISAGREE_NOTES.values()

    # Due for review: auto-grading runs, but sells the prices can't judge
    # (rebalancing, needing cash) are left for the visitor.
    due = client.get("/thesis-reviews", headers=headers).json()
    due = due["reviews"] if isinstance(due, dict) else due
    left = {(r["ticker"], r["action"]) for r in due}
    assert {("ENB.TO", "sell"), ("RY.TO", "sell")} <= left
    recent = client.get("/thesis-reviews/recent", headers=headers).json()["reviews"]
    assert any(r["auto_graded"] for r in recent) and any(not r["auto_graded"] for r in recent)

    summary = client.get("/trades/reasons-summary", headers=headers).json()
    assert summary["dont_remember_count"] == 1
    assert summary["hindsight_count"] == 1          # AMZN's reason, written 25 days later
    assert summary["graded_against_numbers"] == 1   # the TSLA buy


def test_demo_adds_a_holding_that_just_reported_earnings(client, history, market_events, recap_writer):
    from test_earnings_recaps import reported
    today = date.today()
    reported(market_events, "HD", "The Home Depot, Inc.", days_ago=2)
    history["HD"] = [(today - timedelta(days=d), 380.0) for d in range(200, -1, -1)]
    headers = start_demo(client)

    hd = [t for t in client.get("/trades", headers=headers).json() if t["ticker"] == "HD"]
    assert len(hd) == 1 and hd[0]["action"] == "buy" and "Home Depot" in hd[0]["thesis_text"]
    notes = client.get("/notifications", headers=headers).json()["notifications"]
    assert [n["ticker"] for n in notes] == ["HD"]
