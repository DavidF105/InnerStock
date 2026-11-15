import { useState, useEffect } from "react"
import { apiFetch } from "../api"

// NEW: earnings recaps for stocks you hold. When a company reports, the
// backend has Claude write what happened and what it means for the company
// (Backend/earnings_recaps.py) and adds a notification here. Opening this
// page marks them all as read, which clears the sidebar bell.

const SENTIMENT_PILL = { positive: "pill-success", neutral: "pill-muted", negative: "pill-danger" }

function formatDay(isoDate) {
  const [y, m, d] = isoDate.split("-").map(Number)
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
}

function formatPct(value) {
  if (value === null || value === undefined) return "—"
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`
}

function tone(value) {
  if (value === null || value === undefined || value === 0) return ""
  return value > 0 ? " positive" : " negative"
}

function Stat({ label, value, detail, toneValue }) {
  return (
    <div className="recap-stat">
      <div className="recap-stat-label">{label}</div>
      <div className={`recap-stat-value${tone(toneValue)}`}>{value}</div>
      {detail && <div className="recap-stat-detail">{detail}</div>}
    </div>
  )
}

function RecapCard({ recap, focused }) {
  const eps = recap.eps_actual != null ? `$${recap.eps_actual.toFixed(2)}` : "—"
  const epsDetail = recap.eps_estimate != null
    ? `vs $${recap.eps_estimate.toFixed(2)} expected (${formatPct(recap.surprise_pct)})`
    : null
  return (
    <div id={`recap-${recap.id}`} className={`card recap-card${focused ? " focused" : ""}`}>
      <div className="card-head">
        <div className="recap-title">
          {recap.unread && <span className="recap-new-dot" aria-label="New" />}
          <span className="recap-ticker">{recap.ticker}</span>
          <span className="recap-company">{recap.company_name}</span>
        </div>
        <div className="recap-meta">
          <span>Reported {formatDay(recap.report_date)}</span>
          <span className={`pill ${SENTIMENT_PILL[recap.sentiment] || "pill-muted"}`}>{recap.sentiment}</span>
        </div>
      </div>

      <div className="recap-body">
        <h3 className="recap-headline">{recap.headline}</h3>

        <div className="recap-stats">
          <Stat label="EPS" value={eps} detail={epsDetail} toneValue={recap.surprise_pct} />
          <Stat label="Stock reaction" value={formatPct(recap.reaction_pct)} detail="first close after the report" toneValue={recap.reaction_pct} />
          {recap.move_since_pct != null && (
            <Stat label="Since then" value={formatPct(recap.move_since_pct)} toneValue={recap.move_since_pct} />
          )}
        </div>

        <div className="recap-section-label">What happened</div>
        <p className="recap-text">{recap.what_happened}</p>

        <div className="recap-section-label">What it means for {recap.company_name || recap.ticker}</div>
        <p className="recap-text">{recap.what_it_means}</p>

        {recap.sources.length > 0 && (
          <>
            <div className="recap-section-label">Sources</div>
            <ul className="recap-sources">
              {recap.sources.map((source) => (
                <li key={source.url}>
                  <a href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a>
                  {source.publisher && <span className="recap-source-publisher"> · {source.publisher}</span>}
                </li>
              ))}
            </ul>
          </>
        )}

        <div className="recap-footnote">
          Written by Claude from Yahoo Finance numbers and headlines. A summary, not investment advice.
        </div>
      </div>
    </div>
  )
}

function NotificationsPage({ focusId }) {
  const [recaps, setRecaps] = useState(null) // null = still loading
  const [error, setError] = useState(null)

  useEffect(() => {
    apiFetch("/notifications")
      .then((response) => {
        if (!response.ok) throw new Error("Couldn't load notifications")
        return response.json()
      })
      .then((data) => {
        setRecaps(data.notifications)
        // Seen = read. The cards keep their "new" dot for this visit (it's
        // in the data we just loaded); the bell clears right away.
        if (data.unread_count > 0) {
          apiFetch("/notifications/read-all", { method: "POST" })
            .then(() => window.dispatchEvent(new Event("notifications-read")))
            .catch(() => {})
        }
      })
      .catch((err) => setError(err.message))
  }, [])

  // Arriving from a sidebar "Read recap" link: scroll that card into view.
  useEffect(() => {
    if (recaps && focusId) {
      document.getElementById(`recap-${focusId}`)?.scrollIntoView({ behavior: "smooth", block: "start" })
    }
  }, [recaps, focusId])

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Notifications</h1>
          <div className="sub">Earnings recaps for the stocks you hold</div>
        </div>
      </div>
      <div className="content recaps-content">
        {error && <p style={{ color: "var(--color-danger)" }}>{error}</p>}
        {!error && recaps === null && (
          <p style={{ color: "var(--color-text-secondary)" }}>Checking for new earnings reports…</p>
        )}
        {recaps && recaps.length === 0 && (
          <p style={{ color: "var(--color-text-secondary)" }}>
            Nothing yet. When a company you hold reports earnings, a recap of what happened and what it
            means for the company shows up here.
          </p>
        )}
        {recaps && recaps.map((recap) => <RecapCard key={recap.id} recap={recap} focused={recap.id === focusId} />)}
      </div>
    </>
  )
}

export default NotificationsPage
