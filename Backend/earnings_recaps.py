# Earnings recaps: when a company you hold reports earnings, Claude writes
# a short recap - what happened (EPS vs the estimate, how the stock
# reacted, the key headlines) and what it means for the company - and you
# get an in-app notification (the sidebar bell).
#
# No background jobs: recaps are written lazily, the first time any holder
# loads GET /notifications after the results are out. A recap is about the
# company, not the user, so it's stored once per ticker + report date and
# every holder gets a notification pointing at the same row - a popular
# stock costs one Claude call per quarter, not one per user.
#
# When a recap gets written:
#   - the report is in the last REPORT_LOOKBACK_DAYS days
#   - Yahoo's earnings calendar already shows the REPORTED EPS. Waiting
#     for the actual number (rather than "24h after the expected date")
#     means an estimated date that passes without a report never produces
#     a made-up recap.
#   - the user held the stock when it reported (bought on or before the
#     report date and still holds it)
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import fomo
import grading
import holdings_news as hn
from deps import get_current_user, get_db
from models import EarningsNotification, EarningsRecap, Trade, User

router = APIRouter(tags=["notifications"])

# Per-million-token pricing for RECAP_MODEL, used for the cost line printed
# after each call (same habit as the sentiment route). Update both if the
# model changes. If the refusal fallback (below) hands a request to another
# model, the printed figure is approximate.
RECAP_MODEL = "claude-opus-5-5"
CLAUDE_OPUS_5_5_INPUT_COST_PER_MTOK = 4.00
CLAUDE_OPUS_5_5_OUTPUT_COST_PER_MTOK = 20.00

REPORT_LOOKBACK_DAYS = 10
MAX_RECAPS_PER_REQUEST = 3     # bounds how long one page load can spend writing
RECAP_HEADLINES = 8            # headlines given to Claude
RECAP_SOURCES = 5              # of those, shown as links on the recap card
NOTIFICATIONS_LIMIT = 30


# --- Which positions were held when a report came out ---------------------------------

def held_positions(trades):
    """{ticker: date the current position was opened} for every ticker still held.

    Walks trades in date order: a position "opens" when the share count
    goes from zero to positive, and closing it fully resets that, so a
    stock sold and re-bought counts from the re-buy.
    """
    shares, opened = {}, {}
    for trade in sorted(trades, key=lambda t: (t.trade_date, t.id)):
        ticker = trade.ticker.upper()
        before = shares.get(ticker, 0)
        qty = float(trade.quantity)
        after = before + (qty if trade.action == "buy" else -qty)
        shares[ticker] = after
        if before <= hn.SHARE_QTY_EPSILON < after:
            opened[ticker] = trade.trade_date
        elif after <= hn.SHARE_QTY_EPSILON:
            opened.pop(ticker, None)
    return {ticker: opened[ticker] for ticker, qty in shares.items() if qty > hn.SHARE_QTY_EPSILON and ticker in opened}


def latest_reported(reports, now):
    """The most recent calendar entry in the lookback window, if its results are out."""
    window_start = now.timestamp() - REPORT_LOOKBACK_DAYS * 24 * 3600
    past = [r for r in reports or [] if window_start <= r["ts"] <= now.timestamp()]
    if not past:
        return None
    latest = max(past, key=lambda r: r["ts"])
    return latest if latest.get("eps_actual") is not None else None


def find_recent_report(display_ticker, held_since, now):
    """The report this holding needs a recap for, or None. Runs in a thread (yfinance only)."""
    profile = hn.get_profile(display_ticker)
    if not profile or profile.get("quote_type") == "ETF":
        return None
    source_ticker, source_profile = hn.resolve_source(display_ticker, profile)
    report = latest_reported(hn.get_calendar_earnings(source_ticker), now)
    if not report:
        return None
    moment = datetime.fromtimestamp(report["ts"], tz=timezone.utc).astimezone(hn.MARKET_TZ)
    if held_since > moment.date():
        return None  # bought after the report - nothing new to tell them
    return SimpleNamespace(
        display_ticker=display_ticker,
        source_ticker=source_ticker,
        profile=source_profile,
        report=report,
        moment=moment,
        report_date=moment.date(),
        recap=None,
    )


# --- Facts handed to Claude ----------------------------------------------------------------

def price_reaction(closes, report_date, after_close):
    """(reaction %, move since %): last close before the report -> first close after it,
    then first close after it -> latest close. None where the data isn't there yet."""
    if after_close:
        before = [c for c in closes if c[0] <= report_date]
        after = [c for c in closes if c[0] > report_date]
    else:
        # Before the open (or an unknown time): the report day's own close
        # is already the reaction.
        before = [c for c in closes if c[0] < report_date]
        after = [c for c in closes if c[0] >= report_date]
    if not before or not after:
        return None, None
    base, first_after, latest = before[-1][1], after[0][1], closes[-1]
    reaction = (first_after / base - 1) * 100
    move_since = (latest[1] / first_after - 1) * 100 if latest[0] > after[0][0] else None
    return reaction, move_since


def gather_facts(candidate, now):
    report, moment = candidate.report, candidate.moment
    after_close = moment.hour >= 16
    today = now.astimezone(hn.MARKET_TZ).date()
    closes = grading.fetch_daily_closes(candidate.source_ticker, candidate.report_date - timedelta(days=10), today)
    reaction, move_since = price_reaction(closes, candidate.report_date, after_close)

    articles = hn.rank_news(hn.get_news(candidate.source_ticker) or [], candidate.source_ticker,
                            candidate.profile, now, limit=RECAP_HEADLINES * 2)
    # Prefer coverage of THIS report: stories from 12h before it onwards.
    since = moment - timedelta(hours=12)
    fresh = [a for a in articles if a["published_at"] and datetime.fromisoformat(a["published_at"]) >= since]
    articles = (fresh or articles)[:RECAP_HEADLINES]

    return {
        "ticker": candidate.source_ticker,
        "company_name": (candidate.profile or {}).get("name") or candidate.source_ticker,
        "report_date": candidate.report_date.isoformat(),
        "report_timing": "after market close" if after_close else ("before market open" if moment.hour < 12 else "during the day"),
        "eps_estimate": report.get("eps_estimate"),
        "eps_actual": report.get("eps_actual"),
        "surprise_pct": report.get("surprise_pct"),
        "reaction_pct": reaction,
        "move_since_pct": move_since,
        "headlines": articles,
    }


def facts_prompt(facts):
    def pct(value):
        return f"{value:+.1f}%" if value is not None else "not available"

    def money(value):
        return f"${value:.2f}" if value is not None else "not available"

    lines = [
        f"Company: {facts['company_name']} ({facts['ticker']})",
        f"Report date: {facts['report_date']}, {facts['report_timing']}",
        f"EPS (earnings per share): actual {money(facts['eps_actual'])} vs analyst estimate {money(facts['eps_estimate'])} (surprise {pct(facts['surprise_pct'])})",
        f"Stock move from the last close before the report to the first close after it: {pct(facts['reaction_pct'])}",
        f"Further move since then, to the latest close: {pct(facts['move_since_pct'])}",
        "",
        "Recent headlines about the company (newest coverage of this report first where available):",
    ]
    if facts["headlines"]:
        lines += [f"- {a['title']}" + (f" ({a['publisher']})" if a.get("publisher") else "") for a in facts["headlines"]]
    else:
        lines.append("- (no headlines available)")
    return "\n".join(lines)


# --- Claude --------------------------------------------------------------------------

RECAP_SYSTEM = """You write short earnings recaps for InnerStock, a trading journal used by individual investors who hold the stock.

You are given the facts about one company's quarterly earnings report: reported EPS against the analyst estimate, how the share price reacted, and recent news headlines. Write a recap with four fields:

- headline: one plain sentence under 12 words that captures the result. No hype or clickbait.
- what_happened: 2-3 sentences. The result against expectations, how the stock reacted, and the most important points the headlines report (for example revenue, guidance, a segment that stood out).
- what_it_means: 2-3 sentences on what the report suggests for the company's business and outlook - growth, profitability, guidance, competitive position. Ground every point in the facts, and say plainly when the facts are too thin to tell.
- sentiment: the overall read of the report for the company - positive, neutral or negative.

Rules:
- Use only the facts provided. Never invent numbers, quotes or events that aren't in them; if something important (such as revenue) isn't covered, leave it out rather than guess.
- Headlines can be noisy: ignore any that aren't about this report or this company.
- Plain language for a non-professional reader; explain jargon briefly the first time.
- Describe the company, don't advise: no buy, sell or hold recommendations and no price targets of your own."""

RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "what_happened": {"type": "string"},
        "what_it_means": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
    },
    "required": ["headline", "what_happened", "what_it_means", "sentiment"],
    "additionalProperties": False,
}


def _claude_client():
    # The app's one Anthropic client lives in main.py (see CLAUDE.md).
    # Imported here at call time because main.py imports this module.
    import main
    return main.claude_client


def parse_recap(message):
    """The recap dict from a Claude response, or None if it was refused/cut off/malformed."""
    if message.stop_reason == "refusal":
        print(f"[earnings-recap] refused: {getattr(message, 'stop_details', None)}")
        return None
    if message.stop_reason == "max_tokens":
        print("[earnings-recap] hit max_tokens before finishing")
        return None
    # With the refusal fallback, a declined model's partial text can come
    # before a "fallback" block; only text after the last one is the answer.
    text = []
    for block in message.content:
        if block.type == "fallback":
            text = []
        elif block.type == "text":
            text.append(block.text)
    try:
        recap = json.loads("".join(text))
    except (json.JSONDecodeError, TypeError):
        print("[earnings-recap] response was not valid JSON")
        return None
    if not all(isinstance(recap.get(k), str) and recap[k].strip() for k in RECAP_SCHEMA["required"]):
        return None
    if recap["sentiment"] not in ("positive", "neutral", "negative"):
        recap["sentiment"] = "neutral"
    return recap


def write_recap(facts):
    """Ask Claude for the recap. Returns the recap dict, or None on any failure."""
    try:
        message = _claude_client().beta.messages.create(
            model=RECAP_MODEL,
            max_tokens=16000,
            system=RECAP_SYSTEM,
            messages=[{"role": "user", "content": facts_prompt(facts)}],
            # Structured output: the reply is guaranteed to be JSON matching
            # RECAP_SCHEMA, so parsing can't trip on stray prose. Medium
            # effort is plenty for summarizing a page of facts.
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": RECAP_SCHEMA}},
            # If Claude's safety checks decline a request (rare, but news
            # text can trip them), retry it on Anthropic's recommended
            # fallback model inside the same call instead of failing.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as e:
        print(f"[earnings-recap] Claude API error for {facts['ticker']}: {e.status_code} {e.message}")
        return None
    except anthropic.APIConnectionError as e:
        print(f"[earnings-recap] could not reach Claude for {facts['ticker']}: {e}")
        return None

    cost_usd = (
        message.usage.input_tokens / 1_000_000 * CLAUDE_OPUS_5_5_INPUT_COST_PER_MTOK
        + message.usage.output_tokens / 1_000_000 * CLAUDE_OPUS_5_5_OUTPUT_COST_PER_MTOK
    )
    print(f"[earnings-recap] {facts['ticker']} {facts['report_date']}: ~${cost_usd:.4f} "
          f"({message.usage.input_tokens} in / {message.usage.output_tokens} out tokens)")
    return parse_recap(message)


def build_recap(candidate, now):
    """Facts + Claude for one report. Runs in a thread; returns (facts, recap or None)."""
    facts = gather_facts(candidate, now)
    return facts, write_recap(facts)


# --- Creating notifications -------------------------------------------------------------

def _enforce_rate_limit(user_id):
    # Same per-user "analysis" budget as sentiment / bull-bear (main.py).
    import main
    main.enforce_ai_rate_limit(user_id, "analysis")


def _save_recap(db, candidate, facts, recap):
    row = EarningsRecap(
        ticker=candidate.source_ticker,
        company_name=facts["company_name"],
        report_date=candidate.report_date,
        eps_estimate=facts["eps_estimate"],
        eps_actual=facts["eps_actual"],
        surprise_pct=facts["surprise_pct"],
        reaction_pct=facts["reaction_pct"],
        move_since_pct=facts["move_since_pct"],
        headline=recap["headline"].strip(),
        what_happened=recap["what_happened"].strip(),
        what_it_means=recap["what_it_means"].strip(),
        sentiment=recap["sentiment"],
        sources=json.dumps([
            {"title": a["title"], "url": a["url"], "publisher": a.get("publisher")}
            for a in facts["headlines"][:RECAP_SOURCES]
        ]),
    )
    db.add(row)
    try:
        db.commit()
        return row
    except IntegrityError:
        # Another holder's request wrote the same recap a moment earlier.
        db.rollback()
        return _existing_recap(db, candidate)


def _existing_recap(db, candidate):
    return db.query(EarningsRecap).filter(
        EarningsRecap.ticker == candidate.source_ticker,
        EarningsRecap.report_date == candidate.report_date,
    ).first()


def create_due_notifications(db, user):
    """Writes any missing recaps for this user's holdings and adds their notifications."""
    positions = held_positions(db.query(Trade).filter(Trade.user_id == user.id).all())
    if not positions:
        return
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=min(hn.MAX_WORKERS, len(positions))) as pool:
        candidates = [c for c in pool.map(lambda item: find_recent_report(item[0], item[1], now), positions.items()) if c]
    if not candidates:
        return

    # Holding both GOOG and GOOG.TO means two candidates, one recap.
    by_report = {}
    for c in candidates:
        by_report.setdefault((c.source_ticker, c.report_date), []).append(c)

    missing = []
    for group in by_report.values():
        recap = _existing_recap(db, group[0])
        for c in group:
            c.recap = recap
        if recap is None:
            missing.append(group)

    to_write = []
    for group in missing[:MAX_RECAPS_PER_REQUEST]:
        try:
            _enforce_rate_limit(user.id)
        except HTTPException:
            break  # out of AI budget for now - the rest get written on a later load
        to_write.append(group)

    if to_write:
        with ThreadPoolExecutor(max_workers=len(to_write)) as pool:
            results = list(pool.map(lambda group: build_recap(group[0], now), to_write))
        for group, (facts, recap) in zip(to_write, results):
            if recap is None:
                continue  # Claude failed - tried again on the next load
            saved = _save_recap(db, group[0], facts, recap)
            for c in group:
                c.recap = saved

    for c in candidates:
        if c.recap is None:
            continue
        exists = db.query(EarningsNotification.id).filter(
            EarningsNotification.user_id == user.id, EarningsNotification.recap_id == c.recap.id
        ).first()
        if exists:
            continue
        db.add(EarningsNotification(user_id=user.id, recap_id=c.recap.id, display_ticker=c.display_ticker))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # a parallel request from the same user added it first


# --- Routes ------------------------------------------------------------------------------

def serialize(notification, recap):
    return {
        "id": notification.id,
        "ticker": notification.display_ticker,
        "source_ticker": recap.ticker,
        "company_name": recap.company_name,
        "report_date": recap.report_date.isoformat(),
        "eps_estimate": recap.eps_estimate,
        "eps_actual": recap.eps_actual,
        "surprise_pct": recap.surprise_pct,
        "reaction_pct": recap.reaction_pct,
        "move_since_pct": recap.move_since_pct,
        "headline": recap.headline,
        "what_happened": recap.what_happened,
        "what_it_means": recap.what_it_means,
        "sentiment": recap.sentiment,
        "sources": json.loads(recap.sources) if recap.sources else [],
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
        "unread": notification.read_at is None,
        "emailed_at": notification.emailed_at.isoformat() if notification.emailed_at else None,
    }


# PROTECTED + SCOPED: only the logged-in user's own notifications.
@router.get("/notifications")
def list_notifications(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    create_due_notifications(db, current_user)
    rows = (
        db.query(EarningsNotification, EarningsRecap)
        .join(EarningsRecap, EarningsNotification.recap_id == EarningsRecap.id)
        .filter(EarningsNotification.user_id == current_user.id)
        .order_by(EarningsRecap.report_date.desc(), EarningsNotification.id.desc())
        .limit(NOTIFICATIONS_LIMIT)
        .all()
    )
    unread = db.query(EarningsNotification).filter(
        EarningsNotification.user_id == current_user.id, EarningsNotification.read_at.is_(None)
    ).count()
    # NEW: answered "Wait 3 days" checks (fomo.py) share the bell.
    waits, unseen_waits = fomo.answered_waits_for_notifications(db, current_user)
    return {"unread_count": unread + unseen_waits, "notifications": [serialize(n, r) for n, r in rows],
            "waits": waits}


@router.post("/notifications/read-all")
def mark_all_read(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db.query(EarningsNotification).filter(
        EarningsNotification.user_id == current_user.id, EarningsNotification.read_at.is_(None)
    ).update({EarningsNotification.read_at: datetime.utcnow()}, synchronize_session=False)
    fomo.mark_waits_seen(db, current_user)
    db.commit()
    return {"success": True}


@router.post("/notifications/{notification_id}/read")
def mark_read(notification_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    notification = db.query(EarningsNotification).filter(
        EarningsNotification.id == notification_id, EarningsNotification.user_id == current_user.id
    ).first()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.read_at is None:
        notification.read_at = datetime.utcnow()
        db.commit()
    return {"success": True}


# --- "Email me this" -----------------------------------------------------------------------
# NEW: sends one recap to the user's own (confirmed) email address. Only
# to their account address and only once it's confirmed, so this can't be
# used to send mail to someone else. Limits: the same recap at most once
# every RESEND_AFTER, and EMAILS_PER_DAY recap emails per user per day.

RESEND_AFTER = timedelta(minutes=10)
EMAILS_PER_DAY = 20


@router.post("/notifications/{notification_id}/email")
def email_recap(notification_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import email_templates
    import emailer

    row = (
        db.query(EarningsNotification, EarningsRecap)
        .join(EarningsRecap, EarningsNotification.recap_id == EarningsRecap.id)
        .filter(EarningsNotification.id == notification_id, EarningsNotification.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification, recap = row

    if current_user.is_demo:
        raise HTTPException(status_code=403, detail={"code": "demo", "message": "Emailing recaps isn't available in the demo - create a free account to use it."})
    if not current_user.email_verified:
        raise HTTPException(status_code=403, detail={"code": "email_not_verified",
                                                     "message": f"Confirm your email ({current_user.email}) first, then you can send recaps to it."})
    now = datetime.utcnow()
    if notification.emailed_at and now - notification.emailed_at < RESEND_AFTER:
        raise HTTPException(status_code=429, detail="Already sent - check your inbox (and spam folder).")
    sent_today = db.query(EarningsNotification).filter(
        EarningsNotification.user_id == current_user.id, EarningsNotification.emailed_at >= now - timedelta(days=1)
    ).count()
    if sent_today >= EMAILS_PER_DAY:
        raise HTTPException(status_code=429, detail="That's the most recap emails for today - try again tomorrow.")

    subject, html, text = email_templates.recap_email(notification.display_ticker, recap, f"{emailer.app_url()}/")
    try:
        emailer.send_email(current_user.email, subject, html, text)
    except emailer.EmailNotConfigured:
        raise HTTPException(status_code=503, detail="Email isn't set up on this server yet.")
    except emailer.EmailSendFailed:
        raise HTTPException(status_code=502, detail="Couldn't send the email right now. Try again in a few minutes.")

    notification.emailed_at = now
    db.commit()
    return {"sent": True, "to": current_user.email}
