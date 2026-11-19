import { useEffect, useState } from "react"
import { IconBolt, IconHourglass } from "@tabler/icons-react"
import { apiFetch } from "../api"
import { FomoBadge } from "./FomoCheck"

// NEW: the dashboard's "How you buy" card (GET /fomo/summary, Backend/fomo.py).
// Every buy is sorted into chasing / warm / calm / dip by the FOMO check;
// this shows how each kind has actually done for this user:
//   - the FOMO tax: what chasing buys cost vs just buying the S&P 500
//   - the patience test: how often a chased stock was cheaper a week later
//   - the detector vs what the user admitted ("Fear of missing out" tag)
//   - NEW "If you'd waited": for each hot buy, buying when the stock next
//     scored calm instead (the cool-off) - in dollars saved or cost
//   - NEW "Your waits": the "Wait 3 days" checks, waiting or answered
// Renders nothing until at least one buy has been checked (or a wait exists).

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

function shortDate(iso) {
  const [y, m, d] = iso.split("-").map(Number)
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" })
}

function dollars(value) {
  return `$${Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
}

// "saved $120" / "cost $85": positive = waiting would have been cheaper.
function savedText(value) {
  return value >= 0 ? `saved ${dollars(value)}` : `cost ${dollars(value)}`
}

function WaitingLedger({ waiting, ledger }) {
  if (!waiting.checked) return null
  return (
    <div className="fomo-section">
      <div className="fomo-section-head">
        <h3>If you'd waited until it cooled off</h3>
        <span className={`fomo-section-total ${waiting.saved >= 0 ? "positive" : "negative"}`}>
          Waiting would have {savedText(waiting.saved)}
        </span>
      </div>
      <p className="fomo-section-sub">
        For each hot buy, the check was re-run on the following days. The first day the stock scored
        calm is when it "cooled off" (about {waiting.avg_sessions} trading days later on average).
        Buying then would have been cheaper {waiting.cheaper} of {waiting.checked} times.
      </p>
      <ul className="fomo-ledger">
        {ledger.map((t) => {
          const c = t.cool_off
          return (
            <li key={t.id}>
              <FomoBadge band={t.self_tagged && t.band !== "chasing" ? "chasing" : t.band} score={t.score} />
              <strong>{t.ticker}</strong>
              <span className="brokerage-meta">{shortDate(t.trade_date)}</span>
              <span className={`fomo-ledger-saved ${c.saved >= 0 ? "positive" : "negative"}`}>{savedText(c.saved)}</span>
              <span className="fomo-recent-summary">
                Paid ${t.price.toFixed(2)}.{" "}
                {c.cooled
                  ? `Cooled off ${c.sessions === 1 ? "the next day" : `${c.sessions} trading days later`} (${shortDate(c.date)}) at $${c.price.toFixed(2)}`
                  : `Still hot two weeks later; on ${shortDate(c.date)} it was $${c.price.toFixed(2)}`}
                {" "}({signed(c.change_pct)}%). Lowest along the way: ${c.low_price.toFixed(2)} on {shortDate(c.low_date)}.
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function YourWaits({ waits, onCancel }) {
  const { summary } = waits
  return (
    <div className="fomo-section">
      <div className="fomo-section-head">
        <h3><IconHourglass size={15} /> Your "Wait 3 days"</h3>
        {summary.resolved > 0 && (
          <span className="fomo-section-total">
            Cheaper after waiting {summary.cheaper} of {summary.resolved} time{summary.resolved === 1 ? "" : "s"}
            {summary.saved != null ? ` · ${savedText(summary.saved)}` : ""}
          </span>
        )}
      </div>
      {waits.waits.length === 0 ? (
        <p className="fomo-section-sub">
          When the check says a stock looks hot (on Search or when logging a buy), press
          "Wait 3 days instead". Three trading days later you'll see whether waiting paid off.
        </p>
      ) : (
        <ul className="fomo-ledger">
          {waits.waits.map((w) => (
            <li key={w.id}>
              <span className={`pill ${w.status === "waiting" ? "pill-muted" : w.change_pct < 0 ? "pill-success" : "pill-danger"}`}>
                {w.status === "waiting" ? "Waiting" : w.change_pct < 0 ? "Waiting paid off" : "Cost you"}
              </span>
              <strong>{w.ticker}</strong>
              <span className="brokerage-meta">{shortDate(w.check_date)}</span>
              {w.status === "waiting" && (
                <button type="button" className="fomo-link" onClick={() => onCancel(w.id)}>Cancel</button>
              )}
              <span className="fomo-recent-summary">{w.message}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function FomoCard({ refreshKey }) {
  const [summary, setSummary] = useState(null)

  const [reloads, setReloads] = useState(0)

  useEffect(() => {
    apiFetch("/fomo/summary")
      .then((response) => (response.ok ? response.json() : null))
      .then(setSummary)
      .catch(() => setSummary(null))
  }, [refreshKey, reloads])

  function cancelWait(id) {
    apiFetch(`/fomo/waits/${id}`, { method: "DELETE" })
      .then(() => setReloads((n) => n + 1))
      .catch(() => {})
  }

  if (!summary) return null
  const { record } = summary
  if (!record.scored_buys && !record.self_tagged && !summary.waits.waits.length) return null

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

      <WaitingLedger waiting={record.waiting} ledger={summary.waiting_ledger} />
      <YourWaits waits={summary.waits} onCancel={cancelWait} />

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
