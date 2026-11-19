import { useState, useEffect, useRef } from "react"
import { IconBell } from "@tabler/icons-react"
import { apiFetch } from "../api"

// NEW: bell next to the brand name, with a count of unread earnings
// recaps and answered "Wait 3 days" checks. Loading GET /notifications is also what makes the backend write
// recaps for any holdings that just reported (Backend/earnings_recaps.py),
// so this poll is how new recaps appear without opening a page.
// Earnings come out a few times a day at most - 30 minutes is plenty.
const NOTIFICATIONS_POLL_INTERVAL_MS = 30 * 60_000

function NotificationsBell({ active, onClick }) {
  const [unread, setUnread] = useState(0)
  const lastCount = useRef(0)

  useEffect(() => {
    function fetchCount() {
      apiFetch("/notifications")
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          if (!data) return
          // New recaps arrived: tell the holdings sidebar so its rows can
          // show "Reported ..." too.
          if (data.unread_count > lastCount.current) window.dispatchEvent(new Event("recaps-updated"))
          lastCount.current = data.unread_count
          setUnread(data.unread_count)
        })
        .catch(() => {})
    }

    fetchCount()
    // A new trade can mean a new holding to check; the notifications page
    // marking everything read means the count should drop to zero.
    window.addEventListener("holdings-updated", fetchCount)
    window.addEventListener("notifications-read", fetchCount)
    const intervalId = setInterval(fetchCount, NOTIFICATIONS_POLL_INTERVAL_MS)
    return () => {
      window.removeEventListener("holdings-updated", fetchCount)
      window.removeEventListener("notifications-read", fetchCount)
      clearInterval(intervalId)
    }
  }, [])

  return (
    <button
      className={`notifications-bell${active ? " active" : ""}`}
      onClick={onClick}
      aria-label={unread > 0 ? `${unread} new notification${unread === 1 ? "" : "s"}` : "Notifications"}
      title="Notifications"
    >
      <IconBell size={17} />
      {unread > 0 && <span className="notifications-bell-count">{unread > 9 ? "9+" : unread}</span>}
    </button>
  )
}

export default NotificationsBell
