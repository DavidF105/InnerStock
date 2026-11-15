import { useState, useEffect } from "react"
import { apiFetch } from "../api"

// NEW: sidebar section under the watchlist - one row per stock you hold,
// with its next earnings date at a glance and (click to expand) links to
// the few news stories that matter for that company. Data comes from
// GET /holdings/news (Backend/holdings_news.py).
//
// Earnings dates and news don't change minute to minute (the backend
// caches them for 12h / 30min), so this polls far less often than the
// 60s price poll in SidebarWatchlist.
const NEWS_POLL_INTERVAL_MS = 30 * 60_000

// Earnings within this many days get the highlighted chip.
const EARNINGS_SOON_DAYS = 7

// NEW: for this many days after a report, the chip says "Reported" (with
// the recap one click away) instead of the far-off next date.
const JUST_REPORTED_DAYS = 7

// "2026-12-03" -> a local Date at midnight. new Date("2026-12-03") would
// parse as UTC midnight, which shows as the previous day in the Americas.
function parseDay(isoDate) {
  const [y, m, d] = isoDate.split("-").map(Number)
  return new Date(y, m - 1, d)
}

// Compact chip text: "Today", "Tomorrow", "in 5d", or "Jan 28" further out.
// "~" marks a date Yahoo is only estimating.
function earningsChipText(earnings) {
  const prefix = earnings.is_estimate ? "~" : ""
  if (earnings.days_until === 0) return "Today"
  if (earnings.days_until === 1) return "Tomorrow"
  if (earnings.days_until <= 14) return `${prefix}in ${earnings.days_until}d`
  return prefix + parseDay(earnings.date).toLocaleDateString(undefined, { month: "short", day: "numeric" })
}

// Longer line for the expanded view, e.g. "Earnings Thu, Dec 3 · before open".
function earningsDetail(earnings) {
  const day = parseDay(earnings.date).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
  const timing = { before_open: "before open", after_close: "after close" }[earnings.timing]
  return `Earnings ${day}${timing ? ` · ${timing}` : ""}${earnings.is_estimate ? " (estimated)" : ""}`
}

function daysSince(isoDate) {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return Math.round((today - parseDay(isoDate)) / (24 * 60 * 60 * 1000))
}

// "3h", "2d" - how old a news story is.
function timeAgo(isoTimestamp) {
  if (!isoTimestamp) return null
  const minutes = Math.max(0, Math.round((Date.now() - new Date(isoTimestamp).getTime()) / 60_000))
  if (minutes < 60) return `${minutes}m`
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h`
  return `${Math.round(minutes / (60 * 24))}d`
}

// onOpenRecap(notificationId) opens the Notifications page at that recap.
function SidebarHoldingsNews({ onOpenRecap }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(null) // ticker whose news is open

  function fetchHoldingsNews() {
    apiFetch("/holdings/news")
      .then((response) => (response.ok ? response.json() : { holdings: [] }))
      .then((result) => setRows(result.holdings || []))
      .catch(() => {}) // keep whatever we showed last rather than blanking the section
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchHoldingsNews()
    // App.jsx fires "holdings-updated" after a trade is logged or a
    // brokerage sync brings in trades, so a new position shows up here
    // without waiting for the next poll.
    // NEW: NotificationsBell fires "recaps-updated" when new earnings
    // recaps arrive, and NotificationsPage fires "notifications-read" -
    // both change what the rows below show.
    const handleUpdated = () => fetchHoldingsNews()
    const events = ["holdings-updated", "recaps-updated", "notifications-read"]
    events.forEach((name) => window.addEventListener(name, handleUpdated))
    const intervalId = setInterval(fetchHoldingsNews, NEWS_POLL_INTERVAL_MS)
    return () => {
      events.forEach((name) => window.removeEventListener(name, handleUpdated))
      clearInterval(intervalId)
    }
  }, [])

  // Nothing held yet: hide the whole section instead of an empty heading.
  if (!loading && rows.length === 0) return null

  return (
    <div className="sidebar-watchlist sidebar-holdings-news">
      <div className="sidebar-watchlist-title">Holdings · earnings &amp; news</div>

      {loading && <div className="sidebar-watchlist-empty">Loading…</div>}

      {rows.map((row) => {
        const isOpen = expanded === row.ticker
        const soon = row.earnings && row.earnings.days_until <= EARNINGS_SOON_DAYS
        const justReported = row.recap && daysSince(row.recap.report_date) <= JUST_REPORTED_DAYS
        return (
          <div key={row.ticker} className="holdings-news-item">
            <button
              className={`holdings-news-row${isOpen ? " open" : ""}`}
              onClick={() => setExpanded(isOpen ? null : row.ticker)}
              aria-expanded={isOpen}
              title={row.name || row.ticker}
            >
              <span className="sidebar-watchlist-ticker">{row.ticker}</span>
              {row.recap?.unread && <span className="recap-new-dot" aria-label="New earnings recap" />}
              {justReported ? (
                <span className="earnings-chip reported" title={row.recap.headline}>Reported</span>
              ) : row.earnings ? (
                <span className={`earnings-chip${soon ? " soon" : ""}`} title={earningsDetail(row.earnings)}>
                  {earningsChipText(row.earnings)}
                </span>
              ) : (
                <span className="earnings-chip none" title={row.is_etf ? "ETFs don't report earnings" : "Next earnings date not announced yet"}>
                  {row.is_etf ? "ETF" : "—"}
                </span>
              )}
              {row.news.length > 0 && <span className="holdings-news-count">{row.news.length}</span>}
            </button>

            {isOpen && (
              <div className="holdings-news-panel">
                {row.name && <div className="holdings-news-name">{row.name}</div>}
                <div className="holdings-news-earnings">
                  {row.earnings
                    ? earningsDetail(row.earnings)
                    : row.is_etf
                      ? "ETFs don't report earnings."
                      : "Next earnings date not announced yet."}
                </div>

                {/* NEW: the latest earnings recap, one click from the full write-up */}
                {row.recap && (
                  <button className="holdings-news-recap" onClick={() => onOpenRecap?.(row.recap.notification_id)}>
                    <span className="holdings-news-recap-label">
                      Reported {parseDay(row.recap.report_date).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                    </span>
                    <span className="holdings-news-recap-headline">{row.recap.headline}</span>
                    <span className="holdings-news-recap-link">Read recap →</span>
                  </button>
                )}

                {!row.is_etf && row.news.length > 0 && <div className="holdings-news-section">Latest news</div>}

                {!row.is_etf && row.news.map((story) => (
                  <a key={story.url} className="holdings-news-link" href={story.url} target="_blank" rel="noopener noreferrer">
                    <span className="holdings-news-title">{story.title}</span>
                    <span className="holdings-news-meta">
                      {[story.publisher, timeAgo(story.published_at)].filter(Boolean).join(" · ")}
                    </span>
                  </a>
                ))}

                {!row.is_etf && row.news.length === 0 && (
                  <div className="sidebar-watchlist-empty">
                    {row.news_available ? "No company-specific news this week." : "News is unavailable right now."}
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default SidebarHoldingsNews
