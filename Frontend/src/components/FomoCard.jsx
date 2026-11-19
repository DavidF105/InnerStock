import { useEffect, useState } from "react"
import { IconBolt } from "@tabler/icons-react"
import { apiFetch } from "../api"
import { FomoBadge } from "./FomoCheck"

// NEW: the dashboard's "How you buy" card (GET /fomo/summary, Backend/fomo.py).
// Every buy is sorted into chasing / warm / calm / dip by the FOMO check;
// this shows how each kind has actually done for this user:
//   - the FOMO tax: what chasing buys cost vs just buying the S&P 500
//   - the patience test: how often a chased stock was cheaper a week later
//   - the detector vs what the user admitted ("Fear of missing out" tag)
// Renders nothing until at least one buy has been checked.

const GROUPS = [
  { key: "chasing", label: "Chasing" },
  { key: "warm", label: "Warm" },
  { key: "calm", label: "Calm" },
  { key: "dip", label: "Dip buys" },
]

function signed(value, digits = 1) {
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`
}

function money(value) {
  return `${value < 0 ? "-" : "+"}$${Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
}

function FomoCard({ refreshKey }) {
  const [summary, setSummary] = useState(null)

  useEffect(() => {
    apiFetch("/fomo/summary")
      .then((response) => (response.ok ? response.json() : null))
      .then(setSummary)
      .catch(() => setSummary(null))
  }, [refreshKey])

  if (!summary) return null
  const { record } = summary
  if (!record.scored_buys && !record.self_tagged) return null

  const taxTone = record.fomo_tax == null ? "" : record.fomo_tax < 0 ? " negative" : " positive"
  return (
    <div className="card fomo-card">
      <div className="card-head">
        <h2><IconBolt size={16} /> How you buy</h2>
        <span className="brokerage-meta">FOMO check on {record.scored_buys} buy{record.scored_buys === 1 ? "" : "s"}</span>
      </div>

      <div className="fomo-card-body">
        <div className="fomo-tax">
          <div className="stat-label">FOMO tax</div>
          <div className={`fomo-tax-value${taxTone}`}>
            {record.fomo_tax == null ? "—" : money(record.fomo_tax)}
          </div>
          <div className="stat-sub">
            {record.fomo_tax == null
              ? "Shows once a chasing buy has been graded."
              : `${record.fomo_tax < 0 ? "lost" : "gained"} vs buying the S&P 500 instead, on ${record.fomo_tax_trades} graded chasing buy${record.fomo_tax_trades === 1 ? "" : "s"}`
                + (record.fomo_tax >= 0 ? " - chasing has paid off so far" : "")}
          </div>
        </div>

        <table className="fomo-table">
          <thead>
            <tr><th /><th>Buys</th><th>Vs S&amp;P 500</th><th>Cheaper a week later</th></tr>
          </thead>
          <tbody>
            {GROUPS.map(({ key, label }) => {
              const g = record.groups[key]
              return (
                <tr key={key} className={g.count ? "" : "empty"}>
                  <td>{label}</td>
                  <td>{g.count}</td>
                  <td className={g.avg_vs_market_pct == null ? "" : g.avg_vs_market_pct < 0 ? "negative" : "positive"}>
                    {g.avg_vs_market_pct == null ? "—" : `${signed(g.avg_vs_market_pct)} pts`}
                  </td>
                  <td>{g.week_checked ? `${g.cheaper_week_later} of ${g.week_checked}` : "—"}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="fomo-card-notes">
        <p>{summary.message}</p>
        {record.self_tagged > 0 && (
          <p>You admitted FOMO on {record.self_tagged} buy{record.self_tagged === 1 ? "" : "s"}; the check flagged {record.self_tagged_caught} of them.</p>
        )}
        {record.expected_vs_market_pct != null && (
          <p>
            Expected cost of your next chasing buy: <strong>{signed(record.expected_vs_market_pct)} points</strong> vs the S&amp;P 500
            {" "}(from {record.expected_based_on.yours} of your graded chasing buys
            {record.expected_based_on.others ? ` and ${record.expected_based_on.others} from other investors` : ""}
            {record.expected_based_on.yours < 5 ? ", leaning on a \"no difference\" starting point until you have more" : ""}).
          </p>
        )}
      </div>

      {summary.recent_chasing.length > 0 && (
        <ul className="fomo-recent">
          {summary.recent_chasing.map((t) => (
            <li key={t.id}>
              <FomoBadge band="chasing" score={t.score} />
              <strong>{t.ticker}</strong>
              <span className="brokerage-meta">{t.trade_date}{t.self_tagged ? " · you tagged it FOMO" : ""}</span>
              <span className="fomo-recent-summary">{t.summary}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default FomoCard
