import { useState, useEffect } from 'react'
import Layout from './components/Layout'
import AuthScreen from './components/AuthScreen'
import SurveyScreen from './components/SurveyScreen'
import TradeForm from './components/TradeForm'
import Dashboard from './components/Dashboard'
import ChatScreen from './components/ChatScreen'
import { apiFetch } from './api'
import SearchPage from './components/SearchPage'
import TradeReasonsPage from './components/TradeReasonsPage'
import NotificationsPage from './components/NotificationsPage'

// NEW: links in our emails open the app as ?verify=<token> (confirm your
// email) or ?reset=<token> (choose a new password). Read once at load, then
// removed from the address bar so a refresh or a shared screenshot can't
// reuse them.
function readEmailLink() {
  const params = new URLSearchParams(window.location.search)
  const link = { verify: params.get("verify"), reset: params.get("reset") }
  if (link.verify || link.reset) window.history.replaceState(null, "", window.location.pathname)
  return link
}
const EMAIL_LINK = readEmailLink()
let verifyStarted = false // React's dev mode runs effects twice; a link only works once

function App() {
  const [token, setToken] = useState(localStorage.getItem("token"))
  // NEW: account details (/auth/me) - is this a demo, is the email confirmed
  const [me, setMe] = useState(null)
  const [resetToken, setResetToken] = useState(EMAIL_LINK.reset)
  const [notice, setNotice] = useState(null) // e.g. "Your email is confirmed"
  const [authMode, setAuthMode] = useState("login")
  const [refreshKey, setRefreshKey] = useState(0)
  const [activeView, setActiveView] = useState('dashboard')
  // NEW: which earnings recap to scroll to when the Notifications page is
  // opened from a sidebar "Read recap" link (null = just open the page).
  const [focusRecapId, setFocusRecapId] = useState(null)

  // Tracks whether the logged-in user has completed the survey yet.
  // null = still checking, true = has a profile, false = needs the survey.
  const [hasProfile, setHasProfile] = useState(null)

  // How many trades are still waiting for a reason (mostly ones synced
  // from the brokerage). Shown as a sidebar badge and a dashboard nudge;
  // refreshed after a sync, after answering, and whenever the view changes.
  const [inboxCount, setInboxCount] = useState(0)
  // NEW: trades whose review date has arrived and still need a grade. The
  // Trade reasons page now holds both halves (reasons + reviews), so its
  // sidebar badge counts both. /thesis-reviews does no price lookups, so
  // this is cheap to refresh alongside the reason count.
  const [dueCount, setDueCount] = useState(0)

  function refreshInboxCount() {
    apiFetch("/trades/needs-thesis/count")
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (data) setInboxCount(data.count)
      })
      .catch(() => {})
    apiFetch("/thesis-reviews")
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (data) setDueCount(data.reviews.length)
      })
      .catch(() => {})
  }

  useEffect(() => {
    if (token && hasProfile) refreshInboxCount()
  }, [token, hasProfile, activeView])

  // Whenever we have a token, check if a profile already exists for this
  // user - this runs once on login/app load, and again after the survey
  // is submitted (via handleSurveyComplete below).
  useEffect(() => {
    if (!token) {
      setHasProfile(null)
      return
    }

    apiFetch("/profile")
      .then((response) => {
        if (response.status === 401) {
          // Token is invalid or points to a user that no longer exists
          // (e.g. after a database reset during testing) - treat this as
          // "not actually logged in" rather than getting stuck showing
          // the survey with a broken identity underneath it.
          localStorage.removeItem("token")
          setToken(null)
          setHasProfile(null)
          return
        }
        setHasProfile(response.ok)
      })
      .catch(() => {
        setHasProfile(false)
      })
  }, [token])

  function refreshMe() {
    apiFetch("/auth/me")
      .then((response) => (response.ok ? response.json() : null))
      .then(setMe)
      .catch(() => {})
  }

  useEffect(() => {
    if (token && hasProfile) refreshMe()
  }, [token, hasProfile])

  // NEW: a "confirm your email" link was opened. Works logged in or out -
  // holding the link is the proof.
  useEffect(() => {
    if (!EMAIL_LINK.verify || verifyStarted) return
    verifyStarted = true
    apiFetch("/auth/verify-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: EMAIL_LINK.verify }),
    })
      .then(async (response) => {
        const data = await response.json().catch(() => null)
        if (!response.ok) throw new Error(data?.detail || "That confirmation link didn't work.")
        setNotice({ tone: "success", text: "Your email is confirmed. You can now send earnings recaps to your inbox." })
        refreshMe()
      })
      .catch((err) => setNotice({ tone: "error", text: err.message }))
  }, [])

  // NEW: tells sidebar sections that depend on WHAT you hold (not just
  // prices) - currently the holdings earnings/news list - to reload.
  function notifyHoldingsChanged() {
    window.dispatchEvent(new Event("holdings-updated"))
  }

  function handleTradeLogged() {
    setRefreshKey((prev) => prev + 1)
    notifyHoldingsChanged()
  }

  function handleLogin(newToken) {
    localStorage.setItem("token", newToken)
    setToken(newToken)
  }

  function handleLogout() {
    localStorage.removeItem("token")
    setToken(null)
    setMe(null)
    setAuthMode("login")
  }

  // NEW: demo banner's "Create your free account" - leave the demo and
  // land on the sign-up form.
  function handleCreateAccount() {
    handleLogout()
    setAuthMode("signup")
  }

  // Called by SurveyScreen once the profile is successfully submitted -
  // flips hasProfile to true so the app moves on to the real dashboard.
  function handleSurveyComplete() {
    setHasProfile(true)
  }

  // Not logged in — show the auth screen instead of the app entirely.
  // NEW: also shown for a password-reset link, even when logged in.
  if (!token || resetToken) {
    return (
      <AuthScreen
        key={authMode + (resetToken ? "-reset" : "")}
        initialMode={authMode}
        onLogin={handleLogin}
        resetToken={resetToken}
        onResetDone={() => setResetToken(null)}
        notice={notice}
      />
    )
  }

  // Logged in, but we haven't checked (or finished checking) whether a
  // profile exists yet - avoid flashing the survey or dashboard incorrectly
  // while that request is still in flight.
  if (hasProfile === null) {
    return <p style={{ color: "var(--color-text-secondary)", textAlign: "center", marginTop: "40px" }}>Loading...</p>
  }

  // Logged in, but no profile yet - block access to the rest of the app
  // until the required survey is completed.
  if (!hasProfile) {
    return <SurveyScreen onComplete={handleSurveyComplete} />
  }

  return (
    <Layout
      activeView={activeView}
      inboxCount={inboxCount + dueCount}
      onNavigate={(view) => { setFocusRecapId(null); setActiveView(view) }}
      onOpenRecap={(id) => { setFocusRecapId(id); setActiveView('notifications') }}
      onLogout={handleLogout}
      onChangeContext={() => setActiveView('editprofile')}
      isDemo={me?.is_demo}
      onCreateAccount={handleCreateAccount}
      notice={notice}
      onDismissNotice={() => setNotice(null)}
    >
      {activeView === 'dashboard' && (
  <Dashboard refreshKey={refreshKey} onNavigate={setActiveView} inboxCount={inboxCount} onBrokerageSynced={() => { refreshInboxCount(); notifyHoldingsChanged() }} />
)}
      {activeView === 'editprofile' && (
        // Same component as the required first-time survey, just in
        // editMode (pre-fills from the existing profile) - "done" here
        // means back to the dashboard, not flipping hasProfile, since
        // that flag is only meaningful for the initial onboarding gate.
        <SurveyScreen editMode onComplete={() => setActiveView('dashboard')} />
      )}
      {activeView === 'logtrade' && (
  <>
    <div className="topbar">
      <div>
        <h1>Log a trade</h1>
        <div className="sub">Every trade needs a thesis — no exceptions</div>
      </div>
    </div>
    <div className="content">
      <TradeForm onTradeLogged={handleTradeLogged} />
    </div>
  </>
)}
      {activeView === 'search' && (
  <SearchPage />
)}
      {(activeView === 'reasons' || activeView === 'thesisreview') && (
        // 'thesisreview' (the old separate page) opens the review tab
        <TradeReasonsPage
          key={activeView}
          needsReasonCount={inboxCount}
          dueCount={dueCount}
          initialTab={activeView === 'thesisreview' ? 'review' : undefined}
          onAnswered={(remaining) => setInboxCount(remaining)}
          onGraded={() => setDueCount((n) => Math.max(0, n - 1))}
        />
      )}
      {activeView === 'advisor' && (
        <ChatScreen />
      )}
      {activeView === 'notifications' && (
        // key: opening a different recap from the sidebar while already on
        // this page remounts it, so it re-fetches and scrolls to the new one
        <NotificationsPage key={focusRecapId ?? 'all'} focusId={focusRecapId} me={me} />
      )}
    </Layout>
  )
}

export default App