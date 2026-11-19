import { useState } from "react"

// NEW: a company logo next to a ticker, instead of its first two letters.
// Logos come from Financial Modeling Prep's free logo images (no key, the
// browser loads them straight from their site). It tries:
//   1. the exact ticker - TSX listings have their own (RY.TO -> RBC, XEQT.TO -> iShares)
//   2. for a listing with a suffix it doesn't know, the US ticker - this is
//      what makes CDRs work (GOOG.TO -> GOOG's logo)
//   3. the old two-letter badge
// A URL that failed once is remembered for the session, so other rows (and
// later renders) skip straight to the next option.
const logoUrl = (symbol) => `https://financialmodelingprep.com/image-stock/${encodeURIComponent(symbol)}.png`
const broken = new Set()

function candidates(ticker) {
  const symbol = ticker.toUpperCase()
  const base = symbol.split(".")[0]
  return (base !== symbol ? [symbol, base] : [symbol]).filter((s) => !broken.has(s))
}

function StockLogo({ ticker }) {
  // Failures live in `broken`; this counter just re-renders after one.
  const [, setFailures] = useState(0)
  const symbol = candidates(ticker)[0]

  if (!symbol) return <div className="ticker-badge">{ticker.slice(0, 2)}</div>
  return (
    <img
      key={symbol}
      className="ticker-logo"
      src={logoUrl(symbol)}
      alt=""
      loading="lazy"
      onError={() => {
        broken.add(symbol)
        setFailures((n) => n + 1)
      }}
    />
  )
}

export default StockLogo
