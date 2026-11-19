# "Try the demo" (demo.py): private throwaway accounts with sample data.
from datetime import date, datetime, timedelta

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
    assert {"XEQT.TO", "AAPL", "NVDA", "SHOP.TO"} <= held
    assert client.get("/trades/needs-thesis/count", headers=headers).json() == {"count": 3}
    assert len(client.get("/watchlist", headers=headers).json()["watchlist"]) == 3


def test_demo_uses_real_prices_and_grades(client, history):
    # Real closes for every ticker on every day: trades are priced at that
    # day's close and "the user" has graded the ones they were meant to.
    today = date.today()
    for ticker in {t[1] for t in demo.DEMO_TRADES} | {"SPY", "VFV.TO"}:
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
