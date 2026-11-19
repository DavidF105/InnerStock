import { useState } from "react"
import ThesisInbox from "./ThesisInbox"
import ThesisReview from "./ThesisReview"

// NEW: the two halves of the journal loop on one page (this replaced the
// separate "Thesis review" sidebar item):
//   - Needs a reason: say WHY you made a trade, and when to check back
//   - Due for review: when that date arrives, grade whether you were right,
//     with the scorecard (return vs the S&P 500, suggested grade)
// Both tabs are the existing components in `embedded` mode (no topbar of
// their own). Counts come from App.jsx, which also drives the sidebar badge.
function TradeReasonsPage({ needsReasonCount, dueCount, initialTab, onAnswered, onGraded }) {
  // Open whichever tab has something waiting, reasons first.
  const [tab, setTab] = useState(initialTab || (needsReasonCount === 0 && dueCount > 0 ? "review" : "reasons"))

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Trade reasons</h1>
          <div className="sub">Write down why you traded, then grade whether you were right</div>
        </div>
        <div className="toggle-group" role="tablist">
          <button role="tab" aria-selected={tab === "reasons"} className={tab === "reasons" ? "active" : ""} onClick={() => setTab("reasons")}>
            Needs a reason{needsReasonCount > 0 ? ` (${needsReasonCount})` : ""}
          </button>
          <button role="tab" aria-selected={tab === "review"} className={tab === "review" ? "active" : ""} onClick={() => setTab("review")}>
            Due for review{dueCount > 0 ? ` (${dueCount})` : ""}
          </button>
        </div>
      </div>
      <div className="content">
        {tab === "reasons" ? <ThesisInbox embedded onAnswered={onAnswered} /> : <ThesisReview embedded onGraded={onGraded} />}
      </div>
    </>
  )
}

export default TradeReasonsPage
