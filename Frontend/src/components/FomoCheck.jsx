import { IconBolt } from "@tabler/icons-react"

// NEW: shows one FOMO check (Backend/fomo.py) - "was this buy a chase?"
// Used live in the Log Trade form, on the Search page ("Am I chasing
// this?"), and for stored checks on review cards and the reasons inbox.
// `result` is the backend's shape: {score, band, signals, stats, summary,
// record_message?}. `children` are extra actions (e.g. "add the FOMO tag").

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
