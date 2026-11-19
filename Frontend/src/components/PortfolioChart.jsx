import { useEffect, useMemo, useState } from "react"
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { apiFetch } from "../api"

// NEW: the dashboard's big portfolio chart (GET /portfolio/history,
// Backend/portfolio_history.py). Three lines, one dollar axis:
//   - your portfolio's value each day (rebuilt from your trades)
//   - the S&P 500 version: the same money put into the index on the same days
//   - money put in (buys minus sells) - the gap above it is your gain
// The headline shows the gain over the chosen range with deposits taken
// out, so adding money doesn't look like a return.
//
// Colours were checked with the dataviz palette validator on the dark
// surface: portfolio #13a898 (a deeper step of the brand teal - #2dd4bf is
// too light for a chart line), S&P 500 #d95926, also dashed so the two
// never depend on colour alone. Money put in is a neutral dotted reference.

const COLORS = { value: "#13a898", sp500: "#d95926", invested: "#8a8a8a" }
const RANGES = [
  { key: "1M", days: 31 },
  { key: "3M", days: 92 },
  { key: "6M", days: 183 },
  { key: "YTD" },
  { key: "1Y", days: 366 },
  { key: "ALL" },
]

function parseDay(iso) {
  const [y, m, d] = iso.split("-").map(Number)
  return new Date(y, m - 1, d)
}

function money(value, digits = 0) {
  return `$${Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

function signedMoney(value) {
  return `${value >= 0 ? "+" : "-"}${money(value)}`
}

function compactMoney(value) {
  if (Math.abs(value) >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`
  if (Math.abs(value) >= 1_000) return `$${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}k`
  return `$${value.toFixed(0)}`
}

function startOf(rangeKey, last) {
  const range = RANGES.find((r) => r.key === rangeKey)
  if (range.key === "ALL") return null
  if (range.key === "YTD") return new Date(last.getFullYear(), 0, 1)
  return new Date(last.getTime() - range.days * 86_400_000)
}

// Gain over a stretch with deposits/withdrawals taken out.
function gainBetween(first, last, key) {
  if (first[key] == null || last[key] == null) return null
  return last[key] - first[key] - (last.invested - first.invested)
}

function ChartTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  const ahead = p.sp500 != null ? p.value - p.sp500 : null
  return (
    <div className="portfolio-tooltip">
      <div className="portfolio-tooltip-date">
        {parseDay(p.date).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" })}
      </div>
      <div><span className="swatch" style={{ background: COLORS.value }} />Your portfolio <strong>{money(p.value)}</strong></div>
      {p.sp500 != null && (
        <div><span className="swatch dashed" style={{ borderColor: COLORS.sp500 }} />S&amp;P 500 version <strong>{money(p.sp500)}</strong></div>
      )}
      <div><span className="swatch dotted" style={{ borderColor: COLORS.invested }} />Money put in <strong>{money(p.invested)}</strong></div>
      {ahead != null && (
        <div className="portfolio-tooltip-note">{ahead >= 0 ? "Ahead of" : "Behind"} the S&amp;P 500 by {money(ahead)}</div>
      )}
    </div>
  )
}

function PortfolioChart({ refreshKey, liveTotal }) {
  const [history, setHistory] = useState(null)
  const [range, setRange] = useState("1Y")

  useEffect(() => {
    apiFetch("/portfolio/history")
      .then((response) => (response.ok ? response.json() : null))
      .then(setHistory)
      .catch(() => setHistory(null))
  }, [refreshKey])

  // The last point is the latest close; swap in the live total so the
  // chart ends where the stats cards say the portfolio is right now.
  const points = useMemo(() => {
    const all = history?.points || []
    if (!all.length || liveTotal == null) return all
    return [...all.slice(0, -1), { ...all[all.length - 1], value: liveTotal }]
  }, [history, liveTotal])

  const shown = useMemo(() => {
    if (points.length < 2) return points
    const from = startOf(range, parseDay(points[points.length - 1].date))
    const inRange = from ? points.filter((p) => parseDay(p.date) >= from) : points
    return inRange.length >= 2 ? inRange : points
  }, [points, range])

  if (points.length < 2) return null
  const first = shown[0]
  const last = shown[shown.length - 1]
  const gain = gainBetween(first, last, "value")
  const base = first.value + Math.max(0, last.invested - first.invested)
  const gainPct = base > 0 ? (gain / base) * 100 : null
  const indexGain = gainBetween(first, last, "sp500")
  const rangeLabel = range === "ALL" ? "since your first trade" : range === "YTD" ? "this year" : `over ${range}`

  return (
    <div className="card portfolio-chart">
      <div className="portfolio-chart-head">
        <div>
          <div className="stat-label">Portfolio value</div>
          <div className="portfolio-chart-value">{money(last.value, 2)}</div>
          <div className={`portfolio-chart-change ${gain >= 0 ? "gain-up" : "gain-down"}`}>
            {signedMoney(gain)}{gainPct != null && ` (${gainPct >= 0 ? "+" : ""}${gainPct.toFixed(1)}%)`}
            <span className="portfolio-chart-range"> {rangeLabel}</span>
          </div>
        </div>
        <div className="toggle-group">
          {RANGES.map((r) => (
            <button key={r.key} className={range === r.key ? "active" : ""} onClick={() => setRange(r.key)}>{r.key}</button>
          ))}
        </div>
      </div>

      <ul className="portfolio-legend">
        <li><span className="swatch" style={{ background: COLORS.value }} />Your portfolio <strong>{money(last.value)}</strong></li>
        {last.sp500 != null && (
          <li><span className="swatch dashed" style={{ borderColor: COLORS.sp500 }} />S&amp;P 500, same deposits <strong>{money(last.sp500)}</strong></li>
        )}
        <li><span className="swatch dotted" style={{ borderColor: COLORS.invested }} />Money put in <strong>{money(last.invested)}</strong></li>
      </ul>

      <div className="portfolio-chart-plot">
        <ResponsiveContainer width="100%" height={320}>
          <ComposedChart data={shown} margin={{ top: 8, right: 4, bottom: 0, left: 4 }}>
            <defs>
              <linearGradient id="portfolioFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={COLORS.value} stopOpacity={0.28} />
                <stop offset="100%" stopColor={COLORS.value} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} stroke="#2a2a2a" />
            <XAxis
              dataKey="date"
              tickFormatter={(d) => parseDay(d).toLocaleDateString(undefined, shown.length > 200 ? { month: "short", year: "2-digit" } : { month: "short", day: "numeric" })}
              tick={{ fill: "#8a8a8a", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              minTickGap={48}
            />
            <YAxis
              orientation="right"
              domain={["auto", "auto"]}
              tickFormatter={compactMoney}
              tick={{ fill: "#8a8a8a", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              width={52}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ stroke: "#555", strokeWidth: 1 }} />
            <Line type="stepAfter" dataKey="invested" stroke={COLORS.invested} strokeWidth={1.5}
              strokeDasharray="2 4" dot={false} activeDot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="sp500" stroke={COLORS.sp500} strokeWidth={2}
              strokeDasharray="6 4" dot={false} activeDot={{ r: 4, strokeWidth: 2, stroke: "#1a1a1a" }}
              connectNulls isAnimationActive={false} />
            <Area type="monotone" dataKey="value" stroke={COLORS.value} strokeWidth={2} fill="url(#portfolioFill)"
              dot={false} activeDot={{ r: 4, strokeWidth: 2, stroke: "#1a1a1a" }} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {indexGain != null && (
        <p className="portfolio-chart-note">
          {rangeLabel[0].toUpperCase() + rangeLabel.slice(1)} you {gain >= 0 ? "made" : "lost"} {money(gain)}; putting
          the same money into the S&amp;P 500 on the same days would have {indexGain >= 0 ? "made" : "lost"} {money(indexGain)}.
          {" "}{gain >= indexGain ? <strong>You beat the index.</strong> : <strong>The index did better.</strong>}
          {history.mixed_currency && " CAD and USD amounts are added as-is, so the total is approximate."}
        </p>
      )}
    </div>
  )
}

export default PortfolioChart
