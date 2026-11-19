import { useState, useEffect } from "react"
import { IconHourglass } from "@tabler/icons-react"
import { apiFetch } from "../api"

// NEW: earnings recaps for stocks you hold. When a company reports, the
// backend has Claude write what happened and what it means for the company
// (Backend/earnings_recaps.py) and adds a notification here. Opening this
// page marks them all as read, which clears the sidebar bell.
// NEW: answered "Wait 3 days" checks (Backend/fomo.py) show here too.

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

// NEW: "Email me this" - sends the recap to the user's own confirmed email
// (POST /notifications/{id}/email). Unconfirmed emails get a "send me the
// confirmation link" button instead; demo accounts can't email.
function EmailButton({ recap, me }) {
  const [state, setState] = useState(recap.emailed_at ? "sent-before" : "idle") // idle | sending | sent | sent-before
  const [message, setMessage] = useState(null)
  const [needsVerify, setNeedsVerify] = useState(false)

  async function send() {
    setState("sending")
    setMessage(null)
    try {
      const response = await apiFetch(`/notifications/${recap.id}/email`, { method: "POST" })
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        const detail = data?.detail
        if (detail?.code === "email_not_verified") setNeedsVerify(true)
        throw new Error(typeof detail === "string" ? detail : detail?.message || "Couldn't send it - try again.")
      }
      setState("sent")
      setMessage(`Sent to ${data.to}.`)
    } catch (err) {
      setState("idle")
      setMessage(err.message)
    }
  }

  async function resendConfirmation() {
    setMessage(null)
    const response = await apiFetch("/auth/send-verification", { method: "POST" }).catch(() => null)
    const data = await response?.json().catch(() => null)
    setMessage(response?.ok ? `Confirmation link sent to ${data?.to || "your email"} - click it, then come back and press Email me this.`
      : data?.detail || "Couldn't send the link - try again later.")
  }

  if (me?.is_demo) {
    return <div className="recap-email"><span className="recap-email-note">Emailing recaps is available once you create a free account.</span></div>
  }
  return (
    <div className="recap-email">
      <button className="btn" onClick={send} disabled={state === "sending"}>
        {state === "sending" ? "Sending…" : state === "sent" ? "Sent ✓" : state === "sent-before" ? "Email me this again" : "Email me this"}
      </button>
      {needsVerify && <button className="btn" onClick={resendConfirmation}>Send confirmation link</button>}
      {message && <span className="recap-email-note">{message}</span>}
    </div>
  )
}

function RecapCard({ recap, focused, me }) {
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

        <EmailButton recap={recap} me={me} />

        <div className="recap-footnote">
          Written by Claude from Yahoo Finance numbers and headlines. A summary, not investment advice.
        </div>
      </div>
    </div>
  )
}

// NEW: the answer to a "Wait 3 days": the price when the user chose to
// wait vs the close 3 trading days later.
function WaitCard({ wait }) {
  const cheaper = wait.change_pct < 0
  const verdict = wait.change_pct === 0 ? "No difference"
    : wait.saved != null ? `Waiting ${cheaper ? "saved" : "cost"} $${Math.abs(wait.saved).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
      : cheaper ? "Waiting paid off" : "Waiting cost you"
  return (
    <div className="card recap-card wait-card">
      <div className="card-head">
        <div className="recap-title">
          {wait.unread && <span className="recap-new-dot" aria-label="New" />}
          <IconHourglass size={16} />
          <span className="recap-ticker">{wait.ticker}</span>
          <span className="recap-company">Wait 3 days</span>
        </div>
        <div className="recap-meta">
          <span className={`pill ${cheaper ? "pill-success" : wait.change_pct > 0 ? "pill-danger" : "pill-muted"}`}>{verdict}</span>
        </div>
      </div>
      <div className="recap-body">
        <p className="recap-text">{wait.message}</p>
        <div className="recap-stats">
          <Stat label="When you waited" value={`$${wait.price.toFixed(2)}`} detail={formatDay(wait.check_date)} />
          <Stat label="3 trading days later" value={`$${wait.result_price.toFixed(2)}`} detail={formatDay(wait.result_date)} />
          <Stat label="Change" value={formatPct(wait.change_pct)} toneValue={-wait.change_pct} />
        </div>
        {wait.summary && <div className="recap-footnote">The FOMO check at the time{wait.score != null ? ` (${wait.score}/100)` : ""}: {wait.summary}</div>}
      </div>
    </div>
  )
}

function NotificationsPage({ focusId, me }) {
  const [recaps, setRecaps] = useState(null) // null = still loading
  const [waits, setWaits] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    apiFetch("/notifications")
      .then((response) => {
        if (!response.ok) throw new Error("Couldn't load notifications")
        return response.json()
      })
      .then((data) => {
        setRecaps(data.notifications)
        setWaits(data.waits || [])
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
          <div className="sub">Earnings recaps for the stocks you hold, and your "Wait 3 days" answers</div>
        </div>
      </div>
      <div className="content recaps-content">
        {error && <p style={{ color: "var(--color-danger)" }}>{error}</p>}
        {!error && recaps === null && (
          <p style={{ color: "var(--color-text-secondary)" }}>Checking for new earnings reports…</p>
        )}
        {recaps && recaps.length === 0 && waits.length === 0 && (
          <p style={{ color: "var(--color-text-secondary)" }}>
            Nothing yet. When a company you hold reports earnings, a recap of what happened and what it
            means for the company shows up here. So does the answer to any "Wait 3 days" you start.
          </p>
        )}
        {waits.map((wait) => <WaitCard key={`wait-${wait.id}`} wait={wait} />)}
        {recaps && recaps.map((recap) => <RecapCard key={recap.id} recap={recap} focused={recap.id === focusId} me={me} />)}
      </div>
    </>
  )
}

export default NotificationsPage
