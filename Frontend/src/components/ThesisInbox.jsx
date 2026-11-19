import { useEffect, useMemo, useState } from "react"
import { apiFetch } from "../api"
import ReasonChips from "./ReasonChips"
import { FomoBadge } from "./FomoCheck"
import { DONT_REMEMBER, REVIEW_OPTIONS, dateMonthsAfter } from "../reasons"

// How many unanswered trades to render at once - a first brokerage sync can
// bring in hundreds, and the newest ones matter most.
const PAGE_SIZE = 25

function formatTradeDate(isoDate) {
  return new Date(`${isoDate}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
}

function daysAgo(isoDate) {
  const days = Math.round((Date.now() - new Date(`${isoDate}T00:00:00`).getTime()) / 86_400_000)
  if (days <= 0) return "today"
  if (days === 1) return "yesterday"
  return `${days} days ago`
}

function emptyAnswer(action) {
  // Buys come back for grading in 3 months by default; sells usually have
  // nothing left to grade, so they start without a review date.
  return { tags: [], thesis: "", conviction: null, review: action === "buy" ? "3m" : null, applyToSimilar: false }
}

// "Your reasons so far" - the user's own answers, aggregated. This is the
// same summary (GET /trades/reasons-summary) the advisor reasons over.
function ReasonsSummary({ summary }) {
  if (!summary || summary.answered === 0) return null
  const top = summary.by_reason.slice(0, 6)
  const max = Math.max(...top.map((r) => r.count), 1)

  return (
    <div className="card reasons-summary">
      <div className="card-head">
        <h2>Why you trade, so far</h2>
        <span className="brokerage-meta">
          {summary.answered} of {summary.total_trades} trades explained
        </span>
      </div>
      <div className="reasons-summary-body">
        {top.map((r) => (
          <div key={`${r.action}-${r.tag}`} className="reason-bar">
            <span className="reason-bar-label">
              <span className={`pill ${r.action === "buy" ? "pill-success" : "pill-danger"}`}>{r.action}</span>
              {r.label}
            </span>
            <span className="reason-bar-track">
              <span className="reason-bar-fill" style={{ width: `${(r.count / max) * 100}%` }} />
            </span>
            <span className="reason-bar-count">
              {r.count}
              {r.graded > 0 && <span className="brokerage-meta"> · {r.correct_rate}% right</span>}
            </span>
          </div>
        ))}
        <div className="reasons-summary-flags">
          <span>Couldn't remember why: <strong>{summary.dont_remember_rate}%</strong></span>
          <span>Explained in hindsight (7+ days later): <strong>{summary.hindsight_rate}%</strong></span>
        </div>
      </div>
    </div>
  )
}

// NEW: says when the chosen review lands - or that it's already passed,
// in which case the trade is graded as soon as it's answered.
function ReviewDateHint({ trade, reviewKey }) {
  const months = REVIEW_OPTIONS.find((o) => o.key === reviewKey)?.months
  if (!months) return null
  const reviewDate = dateMonthsAfter(trade.trade_date, months)
  const [y, m, d] = reviewDate.split("-").map(Number)
  const day = new Date(y, m - 1, d)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const label = day.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
  return (
    <div className="review-date-hint">
      {day <= today
        ? `That's ${label}, which has already passed, so it'll be graded right away from the numbers.`
        : `It'll be graded on ${label}.`}
    </div>
  )
}

// The "Why did you make this trade?" inbox. Trades synced from a brokerage
// arrive without a thesis; this is where the user gives one. Answers are
// final on purpose (the backend enforces it) - a journal is only useful if
// you can't rewrite why you traded after seeing how it turned out.
// embedded: rendered inside TradeReasonsPage, which owns the topbar and
// the .content wrapper (NEW - this page and Thesis review were merged).
function ThesisInbox({ onAnswered, embedded = false }) {
  const [catalog, setCatalog] = useState(null)
  const [trades, setTrades] = useState(null)
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const [openId, setOpenId] = useState(null)
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null)
  const [shown, setShown] = useState(PAGE_SIZE)

  function loadSummary() {
    apiFetch("/trades/reasons-summary")
      .then((r) => (r.ok ? r.json() : null))
      .then(setSummary)
      .catch(() => {})
  }

  useEffect(() => {
    Promise.all([apiFetch("/trades/reasons"), apiFetch("/trades/needs-thesis")])
      .then(async ([catalogRes, tradesRes]) => {
        if (!catalogRes.ok || !tradesRes.ok) throw new Error("Couldn't load your trades")
        const [catalogData, tradesData] = [await catalogRes.json(), await tradesRes.json()]
        setCatalog(catalogData)
        setTrades(tradesData.trades)
        if (tradesData.trades.length) {
          setOpenId(tradesData.trades[0].id)
          setDraft(emptyAnswer(tradesData.trades[0].action))
        }
      })
      .catch((err) => setError(err.message))
    loadSummary()
  }, [])

  const openTrade = trades?.find((t) => t.id === openId)
  // Other unanswered trades of the same ticker and side - e.g. a run of
  // automatic weekly buys - which can share one answer.
  const similar = useMemo(
    () => (openTrade ? trades.filter((t) => t.id !== openTrade.id && t.ticker === openTrade.ticker && t.action === openTrade.action) : []),
    [trades, openTrade]
  )

  function open(trade) {
    if (trade.id === openId) return
    setOpenId(trade.id)
    setDraft(emptyAnswer(trade.action))
    setFormError(null)
  }

  function update(fields) {
    setDraft((prev) => ({ ...prev, ...fields }))
  }

  async function submit(tags) {
    const dontRemember = tags.length === 1 && tags[0] === DONT_REMEMBER
    if (!tags.length) return setFormError("Pick at least one reason.")
    if (!dontRemember) {
      if (draft.thesis.trim().length < 10) return setFormError("Write a sentence or two about why (at least 10 characters).")
      if (!draft.conviction) return setFormError("How sure were you? Pick 1–5.")
    }

    const ids = [openTrade.id, ...(draft.applyToSimilar ? similar.map((t) => t.id) : [])]
    const reviewMonths = REVIEW_OPTIONS.find((o) => o.key === draft.review)?.months
    setSaving(true)
    setFormError(null)
    try {
      const response = await apiFetch("/trades/thesis", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          trade_ids: ids,
          reason_tags: tags,
          thesis_text: dontRemember ? null : draft.thesis.trim(),
          conviction_score: dontRemember ? null : draft.conviction,
          // NEW: months after each trade's OWN date (the backend works out
          // the date per trade), so an old trade whose date has passed is
          // graded right away instead of months from now.
          review_months: !dontRemember && reviewMonths ? reviewMonths : null,
        }),
      })
      const result = await response.json().catch(() => null)
      if (!response.ok) throw new Error(result?.detail || "Couldn't save your answer")

      // Drop the answered trades and move straight on to the next one.
      const answered = new Set(result.updated)
      const remaining = trades.filter((t) => !answered.has(t.id))
      const currentIndex = trades.findIndex((t) => t.id === openTrade.id)
      const next = remaining[Math.min(currentIndex, remaining.length - 1)]
      setTrades(remaining)
      setOpenId(next?.id ?? null)
      setDraft(next ? emptyAnswer(next.action) : null)
      loadSummary()
      onAnswered?.(result.remaining)
    } catch (err) {
      setFormError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const topbar = (
    <div className="topbar">
      <div>
        <h1>Trade reasons</h1>
        <div className="sub">
          {trades ? `${trades.length} trade${trades.length === 1 ? "" : "s"} waiting for a reason` : "Every trade needs a reason"}
        </div>
      </div>
    </div>
  )

  const page = (children) => (embedded ? children : <>{topbar}<div className="content">{children}</div></>)

  if (error || !trades || !catalog) {
    return page(
      <p style={{ color: error ? "var(--color-danger)" : "var(--color-text-secondary)" }}>{error || "Loading…"}</p>
    )
  }

  return page(
    <>
        <p className="inbox-intro">
          These trades came in from your brokerage without a thesis. Say why you made each one — be honest,
          including <em>“Don't remember”</em>. <strong>Answers are final</strong>: like a real trading journal, you
          can't rewrite your reasons after seeing how a trade turned out.
        </p>

        <ReasonsSummary summary={summary} />

        {trades.length === 0 ? (
          <div className="review-card">
            <div className="thesis-quote">All caught up — every trade has a reason.</div>
          </div>
        ) : (
          <>
            <div className="section-title">Waiting for a reason</div>
            {trades.slice(0, shown).map((trade) => {
              const isOpen = trade.id === openId
              const options = catalog[trade.action]
              return (
                <div key={trade.id} className={`review-card inbox-row${isOpen ? " open" : ""}`}>
                  <button type="button" className="inbox-row-head" onClick={() => open(trade)} aria-expanded={isOpen}>
                    <span className="review-ticker">
                      <span className={`pill ${trade.action === "buy" ? "pill-success" : "pill-danger"}`}>{trade.action}</span>
                      <strong>{trade.ticker}</strong>
                      {trade.fomo && ["chasing", "warm"].includes(trade.fomo.band) && (
                        <FomoBadge band={trade.fomo.band} score={trade.fomo.score} />
                      )}
                      <span className="brokerage-meta">
                        {trade.quantity.toLocaleString(undefined, { maximumFractionDigits: 4 })} @ ${trade.price_per_share.toFixed(2)}
                      </span>
                    </span>
                    <span className="brokerage-meta">
                      {formatTradeDate(trade.trade_date)} · {daysAgo(trade.trade_date)}
                      {trade.account ? ` · ${trade.account.replace("Wealthsimple Trade ", "")}` : ""}
                    </span>
                  </button>

                  {isOpen && draft && (
                    <div className="inbox-form">
                      <div className="reasoning-prompt">
                        {trade.action === "buy" ? `Why did you buy ${trade.ticker}?` : `Why did you sell ${trade.ticker}?`}
                      </div>

                      <div className="field">
                        <label>Reasons (pick all that apply)</label>
                        <ReasonChips options={options} selected={draft.tags} onChange={(tags) => update({ tags })} />
                      </div>

                      {/* NEW: this buy looked like a chase (fomo.py). Asking now, while the
                          user answers, is what makes the FOMO record honest. */}
                      {trade.fomo && ["chasing", "warm"].includes(trade.fomo.band) && !draft.tags.includes("fomo") && (
                        <div className="fomo-nudge">
                          <span><strong>This looked like a chase:</strong> {trade.fomo.summary.replace(/^(Chasing|A bit warm): /, "")} Was it FOMO?</span>
                          <button type="button" className="btn"
                            onClick={() => update({ tags: [...draft.tags.filter((t) => t !== DONT_REMEMBER), "fomo"] })}>
                            Yes, it was
                          </button>
                        </div>
                      )}

                      {!(draft.tags.length === 1 && draft.tags[0] === DONT_REMEMBER) && (
                        <>
                          <div className="field">
                            <label>{trade.action === "buy" ? "What did you expect to happen?" : "What changed?"}</label>
                            <textarea value={draft.thesis} onChange={(e) => update({ thesis: e.target.value })} />
                          </div>

                          <div className="row-2">
                            <div className="field">
                              <label>How sure were you?</label>
                              <div className="conviction-row">
                                {[1, 2, 3, 4, 5].map((score) => (
                                  <button key={score} type="button"
                                    className={`conviction-btn${draft.conviction === score ? " active" : ""}`}
                                    onClick={() => update({ conviction: score })}>
                                    {score}
                                  </button>
                                ))}
                              </div>
                            </div>
                            <div className="field">
                              <label>Grade this trade how long after you made it?</label>
                              <div className="reason-chips">
                                {REVIEW_OPTIONS.map((o) => (
                                  <button key={o.key} type="button"
                                    className={`reason-chip${draft.review === o.key ? " selected" : ""}`}
                                    onClick={() => update({ review: draft.review === o.key ? null : o.key })}>
                                    {o.label}
                                  </button>
                                ))}
                              </div>
                              <ReviewDateHint trade={trade} reviewKey={draft.review} />
                            </div>
                          </div>
                        </>
                      )}

                      {similar.length > 0 && (
                        <label className="inbox-similar">
                          <input type="checkbox" checked={draft.applyToSimilar}
                            onChange={(e) => update({ applyToSimilar: e.target.checked })} />
                          Same answer for the other {similar.length} unanswered {trade.action}
                          {similar.length === 1 ? "" : "s"} of {trade.ticker}
                        </label>
                      )}

                      {formError && <div className="brokerage-message error">{formError}</div>}

                      <div className="inbox-actions">
                        <button type="button" className="btn" disabled={saving} onClick={() => submit([DONT_REMEMBER])}>
                          I don't remember
                        </button>
                        <button type="button" className="btn btn-primary" disabled={saving} onClick={() => submit(draft.tags)}>
                          {saving ? "Saving…" : "Save reason"}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
            {trades.length > shown && (
              <button type="button" className="btn" onClick={() => setShown((n) => n + PAGE_SIZE)}>
                Show {Math.min(PAGE_SIZE, trades.length - shown)} more
              </button>
            )}
          </>
        )}
    </>
  )
}

export default ThesisInbox
