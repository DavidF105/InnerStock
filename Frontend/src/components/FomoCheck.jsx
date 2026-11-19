import { useState } from "react"
import { IconBolt, IconHourglass } from "@tabler/icons-react"
import { apiFetch } from "../api"

// NEW: shows one FOMO check (Backend/fomo.py) - "was this buy a chase?"
// Used live in the Log Trade form, on the Search page ("Am I chasing
// this?"), and for stored checks on review cards and the reasons inbox.
// `result` is the backend's shape: {score, band, signals, stats, summary,
// record_message?}. `children` are extra actions (e.g. "add the FOMO tag",
// "Wait 3 days instead").

const BANDS = {
  chasing: { label: "Chasing", pill: "pill-danger" },
  warm: { label: "A bit warm", pill: "pill-warning" },
  calm: { label: "Calm entry", pill: "pill-success" },
  dip: { label: "Dip buy", pill: "pill-muted" },
  unknown: { label: "Not enough data", pill: "pill-muted" },
}

export function FomoBadge({ band, score }) {
  const meta = BANDS[band] || BANDS.unknown
  return (
    <span className={`pill ${meta.pill} fomo-badge`}>
      <IconBolt size={11} /> {meta.label}{score != null && band !== "dip" ? ` · ${score}` : ""}
    </span>
  )
}

// NEW: "Wait 3 days instead" (POST /fomo/waits). Shown when a buy looks
// hot. The backend saves the price now and, 3 trading days later, tells
// the user (notifications bell) whether waiting saved or cost money.
export function WaitButton({ ticker, price, quantity, dollars }) {
  const [state, setState] = useState({ status: "idle", message: null }) // idle | saving | done | error

  async function startWait() {
    setState({ status: "saving", message: null })
    try {
      const response = await apiFetch("/fomo/waits", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker, price: price || null, quantity: quantity || null, dollars: dollars || null }),
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "Couldn't save that - try again.")
      setState({ status: "done", message: `${data.message} You'll get the answer in your notifications.` })
    } catch (err) {
      setState({ status: "error", message: err.message })
    }
  }

  if (state.status === "done") {
    return <div className="fomo-wait done"><IconHourglass size={14} /> {state.message}</div>
  }
  return (
    <div className="fomo-wait">
      <span>Not sure? Wait 3 trading days instead. We'll save today's price and tell you whether waiting paid off.</span>
      <button type="button" className="btn" onClick={startWait} disabled={state.status === "saving"}>
        <IconHourglass size={14} /> {state.status === "saving" ? "Saving…" : "Wait 3 days instead"}
      </button>
      {state.status === "error" && <span className="fomo-wait-error">{state.message}</span>}
    </div>
  )
}

function pct(value) {
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`
}

function FomoCheck({ result, title = "FOMO check", children }) {
  if (!result || result.band === "unknown") return null
  const { stats } = result
  return (
    <div className={`fomo-check fomo-${result.band}`}>
      <div className="fomo-check-head">
        <span className="fomo-check-title"><IconBolt size={14} /> {title}</span>
        <FomoBadge band={result.band} score={result.score} />
      </div>

      {result.signals.length > 0 ? (
        <ul className="fomo-signals">
          {result.signals.map((s) => (
            <li key={s.key}>
              <strong>{s.label}</strong> {s.detail}
              {s.points > 0 && <span className="fomo-points">+{s.points}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="fomo-quiet">No unusual move, volume or rush before this buy.</p>
      )}

      {stats && (
        <div className="fomo-stats">
          Last 5 trading days: {pct(stats.runup_pct)}
          {stats.market_pct != null && <> · market {pct(stats.market_pct)}</>}
          {" "}· a normal week for this stock is about ±{stats.normal_week_pct.toFixed(1)}%
        </div>
      )}

      {result.record_message && <div className="fomo-record">{result.record_message}</div>}
      {children}
    </div>
  )
}

export default FomoCheck
