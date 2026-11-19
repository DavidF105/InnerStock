# The advisor chat (advisor.py): history, the tool loop, each tool, and
# suggested questions. Claude is replaced by a scripted fake client.
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import advisor
import main
from conftest import log_trade, signup
from models import ChatMessage
from test_earnings_recaps import et_day, get_notifications, reported
from test_holdings_news import article, stock


def text_reply(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                 cache_creation_input_tokens=0, cache_read_input_tokens=0))


def tool_reply(*calls):
    blocks = [SimpleNamespace(type="tool_use", id=f"tu{i}", name=name, input=args) for i, (name, args) in enumerate(calls)]
    return SimpleNamespace(stop_reason="tool_use", content=blocks,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                 cache_creation_input_tokens=0, cache_read_input_tokens=0))


class FakeClaude:
    """Returns the scripted replies in order and records what each request sent."""

    def __init__(self):
        self.script = []
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Copy the message list - the loop keeps appending to the same one.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.script.pop(0) if self.script else text_reply("(no more scripted replies)")


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(main, "claude_client", fake)
    main._ai_call_log.clear()  # rate-limit counts are global, keyed by user id
    return fake


def chat(client, headers, message):
    response = client.post("/chat", headers=headers, json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


def tool_results(request):
    """The tool_result blocks sent back in a request's last message."""
    return request["messages"][-1]["content"]


# --- Conversation + history --------------------------------------------------------

def test_reply_is_stored_and_returned(client, alice, claude):
    claude.script = [text_reply("Hello! Your portfolio looks fine.")]
    data = chat(client, alice, "hi")
    assert data == {"reply": "Hello! Your portfolio looks fine.", "tools_used": []}
    history = client.get("/chat/history", headers=alice).json()
    assert [m["content"] for m in history["messages"]][-2:] == ["hi", "Hello! Your portfolio looks fine."]


def test_history_sends_the_newest_messages_not_the_oldest(client, alice, claude, db_session_factory):
    user_id = client.get("/auth/me", headers=alice).json()["id"]
    db = db_session_factory()
    for i in range(50):
        db.add(ChatMessage(user_id=user_id, role="user" if i % 2 == 0 else "assistant", content=f"old {i}"))
    db.commit()
    db.close()

    chat(client, alice, "the newest question")
    sent = claude.requests[0]["messages"]
    assert len(sent) <= advisor.HISTORY_MESSAGES
    assert sent[-1] == {"role": "user", "content": "the newest question"}
    assert sent[0]["role"] == "user"
    assert all(m["content"] != "old 0" for m in sent)


def test_instructions_are_cached_and_facts_are_fresh(client, alice, claude, prices):
    prices["AAPL"] = (110, 100)
    log_trade(client, alice, "AAPL", "buy", 5, 100)
    chat(client, alice, "how am I doing?")
    system = claude.requests[0]["system"]
    assert system[0]["text"] == advisor.ADVISOR_INSTRUCTIONS
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "AAPL" in system[1]["text"] and "Today's date" in system[1]["text"]
    assert claude.requests[0]["model"] == advisor.ADVISOR_MODEL


def test_message_validation(client, alice, claude):
    assert client.post("/chat", headers=alice, json={"message": ""}).status_code == 422
    assert client.post("/chat", headers=alice, json={"message": "x" * (advisor.MAX_MESSAGE_CHARS + 1)}).status_code == 422
    assert client.post("/chat", headers=alice, json={"message": "   "}).status_code == 400


def test_claude_failure_returns_500_but_keeps_the_question(client, alice, claude, monkeypatch):
    def boom(**kwargs):
        raise advisor.AdvisorUnavailable()
    monkeypatch.setattr(advisor, "run_advisor", lambda *a: boom())
    assert client.post("/chat", headers=alice, json={"message": "hello?"}).status_code == 500
    assert client.get("/chat/history", headers=alice).json()["messages"][-1]["content"] == "hello?"


# --- Tool loop ------------------------------------------------------------------------

def test_tool_loop_looks_things_up_then_answers(client, alice, claude):
    log_trade(client, alice, "AAPL", "buy", 5, 100)
    claude.script = [
        tool_reply(("get_my_trades", {"ticker": "AAPL"})),
        text_reply("You bought AAPL because: Test thesis for AAPL."),
    ]
    data = chat(client, alice, "why did I buy AAPL?")
    assert data["reply"].startswith("You bought AAPL")
    assert data["tools_used"] == ["your AAPL trades"]

    results = tool_results(claude.requests[1])
    assert results[0]["tool_use_id"] == "tu0" and results[0]["is_error"] is False
    assert "Test thesis for AAPL" in results[0]["content"]
    # Claude's own turn was echoed back unchanged before the results.
    assert claude.requests[1]["messages"][-2]["role"] == "assistant"


def test_parallel_tool_calls_answered_in_one_message(client, alice, claude, prices):
    prices["AAPL"] = (110, 100)
    claude.script = [
        tool_reply(("get_stock_snapshot", {"ticker": "AAPL"}), ("get_portfolio_breakdown", {})),
        text_reply("Done."),
    ]
    chat(client, alice, "look at AAPL")
    assert [r["tool_use_id"] for r in tool_results(claude.requests[1])] == ["tu0", "tu1"]


def test_tool_errors_go_back_to_claude_instead_of_failing(client, alice, claude):
    claude.script = [tool_reply(("get_stock_snapshot", {"ticker": "NOPE"})), text_reply("I couldn't find that ticker.")]
    data = chat(client, alice, "what about NOPE?")
    result = tool_results(claude.requests[1])[0]
    assert result["is_error"] is True and "No price data" in result["content"]
    assert data["tools_used"] == []
    assert data["reply"] == "I couldn't find that ticker."


def test_lookup_rounds_are_capped(client, alice, claude):
    claude.script = [tool_reply(("get_my_trades", {}))] * advisor.MAX_TOOL_ROUNDS + [text_reply("Here's what I found.")]
    assert chat(client, alice, "dig deep")["reply"] == "Here's what I found."
    assert len(claude.requests) == advisor.MAX_TOOL_ROUNDS + 1
    assert claude.requests[-1]["tool_choice"] == {"type": "none"}
    assert all(r["tool_choice"] == {"type": "auto"} for r in claude.requests[:-1])


def test_refusal_gets_a_friendly_reply(client, alice, claude):
    claude.script = [SimpleNamespace(stop_reason="refusal", content=[],
                                     usage=SimpleNamespace(input_tokens=1, output_tokens=0))]
    assert "different way" in chat(client, alice, "something odd")["reply"]


def test_text_after_a_fallback_block_is_the_answer(client, alice, claude):
    reply = text_reply("final answer")
    reply.content = [SimpleNamespace(type="text", text="partial"), SimpleNamespace(type="fallback"),
                     SimpleNamespace(type="text", text="final answer")]
    claude.script = [reply]
    assert chat(client, alice, "q")["reply"] == "final answer"


# --- Individual tools -------------------------------------------------------------------

def ctx_for(client, headers, db_session_factory):
    """An AdvisorContext for the user behind `headers`."""
    me = client.get("/auth/me", headers=headers).json()
    db = db_session_factory()
    user = db.get(main.User, me["id"])
    return advisor.AdvisorContext(db, user)


def test_snapshot_includes_position_watchlist_and_earnings(client, alice, prices, market_events, db_session_factory):
    prices["AAPL"] = (110, 100)
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=12)
    log_trade(client, alice, "AAPL", "buy", 5, 100)
    client.post("/watchlist", headers=alice, json={"ticker": "AAPL"})

    snap = advisor.tool_stock_snapshot(ctx_for(client, alice, db_session_factory), "aapl")
    assert snap["name"] == "Apple Inc." and snap["currency"] == "USD"
    assert snap["day_change_pct"] == 10.0
    assert snap["your_position"]["shares"] == 5
    assert snap["on_your_watchlist"] is True
    assert snap["next_earnings"]["days_until"] == 12


def test_performance_compares_to_the_matching_benchmark(client, alice, history, db_session_factory):
    today = date.today()
    history["SHOP.TO"] = [(today - timedelta(days=d), 100 + (100 - d) * 0.5) for d in range(100, -1, -1)]
    history["VFV.TO"] = [(today - timedelta(days=d), 100.0) for d in range(100, -1, -1)]
    perf = advisor.tool_price_performance(ctx_for(client, alice, db_session_factory), "SHOP.TO")
    assert perf["benchmark"]["ticker"] == "VFV.TO"
    assert perf["returns_pct"]["1m"] > 0 and perf["benchmark"]["returns_pct"]["1m"] == 0
    assert perf["pct_below_52w_high"] == 0


def test_news_tool_uses_ranked_company_news(client, alice, market_events, db_session_factory):
    market_events.profiles["AAPL"] = stock("Apple Inc.", start_days=12)
    market_events.news["AAPL"] = [article("Apple beats earnings"), article("Dow slips")]
    news = advisor.tool_company_news(ctx_for(client, alice, db_session_factory), "AAPL")
    assert [s["title"] for s in news["stories"]] == ["Apple beats earnings"]


def test_earnings_tool_includes_our_recap(client, alice, market_events, recap_writer, db_session_factory):
    reported(market_events, "AAPL", "Apple Inc.")
    log_trade(client, alice, "AAPL", "buy", 5, 90)
    get_notifications(client, alice)  # writes the recap
    result = advisor.tool_earnings(ctx_for(client, alice, db_session_factory), "AAPL")
    assert result["recent_reports"][0]["eps_actual"] == 2.10
    assert result["latest_recap"]["headline"] == "Results beat estimates"
    assert result["next_report"]["days_until"] == 75


def test_trade_tools_only_see_the_users_own_trades(client, alice, bob, db_session_factory):
    log_trade(client, alice, "AAPL", "buy", 5, 100)
    bobs = log_trade(client, bob, "MSFT", "buy", 1, 300)
    ctx = ctx_for(client, alice, db_session_factory)
    assert [t["ticker"] for t in advisor.tool_my_trades(ctx)["trades"]] == ["AAPL"]
    with pytest.raises(advisor.ToolError):
        advisor.tool_trade_review(ctx, bobs["id"])


def test_trade_review_returns_the_scorecard(client, alice, history, db_session_factory):
    start = date.today() - timedelta(days=40)
    trade = log_trade(client, alice, "AAPL", "buy", 5, 100, trade_date=start)
    history["AAPL"] = [(start + timedelta(days=d), 100 + d) for d in range(41)]
    card = advisor.tool_trade_review(ctx_for(client, alice, db_session_factory), trade["id"])
    assert card["available"] is True and card["return_now_pct"] == 40.0
    assert card["trade"]["thesis"] == "Test thesis for AAPL"
    assert "series" not in card and len(card["price_path"]) <= 10


def test_simulate_trade_shows_concentration_change(client, alice, prices, db_session_factory):
    prices["AAPL"] = (100, 100)
    prices["MSFT"] = (100, 100)
    log_trade(client, alice, "AAPL", "buy", 10, 100)
    log_trade(client, alice, "MSFT", "buy", 10, 100)
    ctx = ctx_for(client, alice, db_session_factory)

    sim = advisor.tool_simulate_trade(ctx, "AAPL", "buy", dollars=2000)
    assert sim["trade"]["shares"] == 20
    assert sim["before"]["position_weight_pct"] == 50.0
    assert sim["after"]["position_weight_pct"] == 75.0
    assert sim["after"]["largest_position"]["ticker"] == "AAPL"

    with pytest.raises(advisor.ToolError):
        advisor.tool_simulate_trade(ctx, "AAPL", "sell", shares=11)
    assert advisor.tool_simulate_trade(ctx, "AAPL", "sell", shares=10)["after"]["position_weight_pct"] == 0


def test_portfolio_breakdown(client, alice, prices, db_session_factory):
    prices["AAPL"] = (150, 150)
    prices["SHOP.TO"] = (50, 50)
    log_trade(client, alice, "AAPL", "buy", 1, 100)
    log_trade(client, alice, "SHOP.TO", "buy", 1, 100)
    breakdown = advisor.tool_portfolio_breakdown(ctx_for(client, alice, db_session_factory))
    assert breakdown["largest_position_pct"] == 75.0
    assert breakdown["currency_split_pct"] == {"CAD": 25.0, "USD": 75.0}
    assert breakdown["biggest_winners"] == ["AAPL"] and breakdown["biggest_losers"] == ["SHOP.TO"]


def test_bad_ticker_input_is_rejected():
    with pytest.raises(advisor.ToolError):
        advisor._clean_ticker("DROP TABLE trades;")


# --- Suggestions ------------------------------------------------------------------------

def suggestions(client, headers):
    response = client.get("/chat/suggestions", headers=headers)
    assert response.status_code == 200
    return [s["label"] for s in response.json()["suggestions"]]


def test_suggestions_put_urgent_things_first(client, alice, prices, market_events, recap_writer):
    reported(market_events, "AAPL", "Apple Inc.")             # reported 2 days ago -> recap
    market_events.profiles["NVDA"] = stock("NVIDIA Corporation", start_days=2)  # reports in 2 days
    prices["AAPL"] = (100, 100)
    prices["NVDA"] = (100, 100)
    log_trade(client, alice, "AAPL", "buy", 1, 90)
    log_trade(client, alice, "NVDA", "buy", 1, 90)
    log_trade(client, alice, "MSFT", "buy", 1, 90, review_date=date.today() - timedelta(days=1))
    get_notifications(client, alice)

    labels = suggestions(client, alice)
    assert labels[0] == "What AAPL's earnings mean for me"
    assert labels[1] == "NVDA reports in 2 days"
    assert "Review my 1 due trade" in labels
    assert len(labels) == advisor.MAX_SUGGESTIONS


def test_suggestions_flag_big_moves_concentration_and_losses(client, alice, prices):
    prices["TSLA"] = (90, 100)     # -10% today
    prices["AAPL"] = (50, 50)      # down 50% from cost
    log_trade(client, alice, "TSLA", "buy", 100, 100)
    log_trade(client, alice, "AAPL", "buy", 1, 100)
    labels = suggestions(client, alice)
    assert "Why is TSLA down 10% today?" in labels
    assert "Does my AAPL thesis still hold?" in labels
    assert "Is TSLA too big a position?" not in labels  # one suggestion per stock


def test_new_user_gets_generic_suggestions(client, claude):
    newbie = signup(client, "new@example.com")
    labels = suggestions(client, newbie)
    assert labels == ["How should I think about my next buy?"]


def test_suggestions_mention_fresh_auto_grades(client, alice, history):
    from test_auto_grading import winning_trade
    winning_trade(client, alice, history)
    client.get("/thesis-reviews", headers=alice)  # auto-grades it
    assert "What my new grade says" in suggestions(client, alice)
