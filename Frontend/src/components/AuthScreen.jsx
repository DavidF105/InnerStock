import { useState } from "react"
import { IconEye, IconEyeOff, IconCheck, IconScale, IconBell, IconMessageCircle, IconArrowLeft } from "@tabler/icons-react"
import { apiFetch } from "../api"

// NEW: the redesigned front door. Left: what InnerStock does that a
// brokerage app can't (first-time visitors used to see a bare form with no
// idea what the app was). Right: one card that switches between
//   login    - email + password, "Forgot password?"
//   signup   - name + email + password with a strength meter
//   forgot   - asks for the email, sends a reset link (Backend/accounts.py)
//   reset    - opened from the emailed link (?reset=...), sets a new password
// plus "Try the demo" (Backend/demo.py), which logs into a private sample
// account without signing up.

const MIN_PASSWORD = 8

const FEATURES = [
  { icon: IconScale, title: "Know if you were right", text: "Write why you made each trade, then get graded against the S&P 500 when the review date comes." },
  { icon: IconBell, title: "Earnings, explained", text: "When a stock you own reports, get a plain-English recap of what happened and what it means." },
  { icon: IconMessageCircle, title: "An advisor that knows you", text: "Ask about any stock or trade. It looks up the numbers and knows your whole journal." },
]

// 0-4: length, mixed case, digits, symbols. Just guidance - the only hard
// rule (checked on the server too) is the minimum length.
function passwordStrength(password) {
  if (password.length < MIN_PASSWORD) return 0
  let score = 1
  if (password.length >= 12) score++
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score++
  if (/\d/.test(password) && /[^A-Za-z0-9]/.test(password)) score++
  return score
}
const STRENGTH_LABELS = ["Too short", "Okay", "Good", "Strong", "Very strong"]

// FastAPI errors come back as {detail: "text"} or, for validation errors,
// {detail: [{msg}, ...]} - turn either into one readable sentence.
function errorText(data, fallback) {
  if (!data?.detail) return fallback
  if (typeof data.detail === "string") return data.detail
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg).join(". ")
  return data.detail.message || fallback
}

function PasswordField({ label, value, onChange, autoComplete, showStrength }) {
  const [visible, setVisible] = useState(false)
  const strength = passwordStrength(value)
  return (
    <label className="auth-field">
      <span>{label}</span>
      <div className="auth-password">
        <input
          type={visible ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoComplete={autoComplete}
          minLength={showStrength ? MIN_PASSWORD : undefined}
          maxLength={72}
          required
        />
        <button type="button" className="auth-eye" onClick={() => setVisible(!visible)} aria-label={visible ? "Hide password" : "Show password"}>
          {visible ? <IconEyeOff size={16} /> : <IconEye size={16} />}
        </button>
      </div>
      {showStrength && value && (
        <div className="auth-strength" data-level={strength}>
          <div className="auth-strength-bar"><span style={{ width: `${(strength / 4) * 100}%` }} /></div>
          <small>{STRENGTH_LABELS[strength]}{strength === 0 ? ` - at least ${MIN_PASSWORD} characters` : ""}</small>
        </div>
      )}
    </label>
  )
}

// notice: {tone, text} from App (e.g. "Your email is confirmed") - shown on
// its own so it can arrive after this screen is already up.
function AuthScreen({ onLogin, initialMode = "login", resetToken = null, onResetDone, notice = null }) {
  const [mode, setMode] = useState(resetToken ? "reset" : initialMode)
  const [name, setName] = useState("")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState(null)
  const [info, setInfo] = useState(null)
  const [loading, setLoading] = useState(false)
  const [demoLoading, setDemoLoading] = useState(false)

  function switchMode(next) {
    setMode(next)
    setError(null)
    setInfo(null)
    setPassword("")
  }

  async function post(path, body) {
    const response = await apiFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
    const data = await response.json().catch(() => null)
    if (!response.ok) throw new Error(errorText(data, "Something went wrong - try again."))
    return data
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setLoading(true)
    setError(null)
    setInfo(null)
    try {
      if (mode === "login") {
        onLogin((await post("/auth/login", { email, password })).access_token)
      } else if (mode === "signup") {
        if (password.length < MIN_PASSWORD) throw new Error(`Use at least ${MIN_PASSWORD} characters for your password.`)
        onLogin((await post("/auth/signup", { name, email, password })).access_token)
      } else if (mode === "forgot") {
        await post("/auth/forgot-password", { email })
        setInfo(`If ${email} has an InnerStock account, a reset link is on its way. It works for 30 minutes - check your spam folder too.`)
      } else if (mode === "reset") {
        if (password.length < MIN_PASSWORD) throw new Error(`Use at least ${MIN_PASSWORD} characters for your password.`)
        const data = await post("/auth/reset-password", { token: resetToken, password })
        onResetDone?.()
        onLogin(data.access_token)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleDemo() {
    setDemoLoading(true)
    setError(null)
    try {
      onLogin((await post("/auth/demo", {})).access_token)
    } catch (err) {
      setError(err.message)
      setDemoLoading(false)
    }
  }

  const titles = {
    login: ["Welcome back", "Log in to your trading journal."],
    signup: ["Create your account", "Free. Takes 30 seconds."],
    forgot: ["Reset your password", "We'll email you a link to choose a new one."],
    reset: ["Choose a new password", "At least 8 characters. You'll be logged in right after."],
  }
  const [title, subtitle] = titles[mode]
  const busy = loading || demoLoading

  return (
    <div className="auth-page">
      <section className="auth-pitch">
        <div className="auth-brand">
          <div className="brand-mark" />
          <span>InnerStock</span>
        </div>
        <h1>Know <em>why</em> you trade.</h1>
        <p className="auth-lede">
          Your brokerage shows what you bought. InnerStock remembers why, then shows you which of your
          reasons actually make money.
        </p>
        <ul className="auth-features">
          {FEATURES.map(({ icon: Icon, title: featureTitle, text }) => (
            <li key={featureTitle}>
              <span className="auth-feature-icon"><Icon size={18} /></span>
              <div>
                <strong>{featureTitle}</strong>
                <p>{text}</p>
              </div>
            </li>
          ))}
        </ul>
        <p className="auth-footnote"><IconCheck size={14} /> Syncs with Wealthsimple · Works with TSX and US stocks</p>
      </section>

      <section className="auth-panel">
        <form className="auth-card" onSubmit={handleSubmit}>
          {(mode === "login" || mode === "signup") && (
            <div className="auth-tabs" role="tablist">
              <button type="button" role="tab" aria-selected={mode === "login"} className={mode === "login" ? "active" : ""} onClick={() => switchMode("login")}>Log in</button>
              <button type="button" role="tab" aria-selected={mode === "signup"} className={mode === "signup" ? "active" : ""} onClick={() => switchMode("signup")}>Sign up</button>
            </div>
          )}
          {(mode === "forgot" || mode === "reset") && (
            <button type="button" className="auth-back" onClick={() => { onResetDone?.(); switchMode("login") }}>
              <IconArrowLeft size={14} /> Back to log in
            </button>
          )}

          <h2>{title}</h2>
          <p className="auth-subtitle">{subtitle}</p>

          {notice && <div className={notice.tone === "error" ? "auth-error" : "auth-info"}>{notice.text}</div>}
          {info && <div className="auth-info">{info}</div>}

          {mode === "signup" && (
            <label className="auth-field">
              <span>Name</span>
              <input type="text" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" required maxLength={100} />
            </label>
          )}

          {mode !== "reset" && (
            <label className="auth-field">
              <span>Email</span>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required autoFocus />
            </label>
          )}

          {mode === "login" && (
            <>
              <PasswordField label="Password" value={password} onChange={setPassword} autoComplete="current-password" />
              <button type="button" className="auth-link auth-forgot" onClick={() => switchMode("forgot")}>Forgot password?</button>
            </>
          )}
          {(mode === "signup" || mode === "reset") && (
            <PasswordField label={mode === "reset" ? "New password" : "Password"} value={password} onChange={setPassword}
              autoComplete="new-password" showStrength />
          )}

          {error && <div className="auth-error" role="alert">{error}</div>}

          {!(mode === "forgot" && info) && (
            <button type="submit" className="btn btn-accent auth-submit" disabled={busy}>
              {loading ? "Please wait…" : { login: "Log in", signup: "Create account", forgot: "Send reset link", reset: "Save new password" }[mode]}
            </button>
          )}

          {(mode === "login" || mode === "signup") && (
            <>
              <div className="auth-divider"><span>or</span></div>
              <button type="button" className="btn auth-demo" onClick={handleDemo} disabled={busy}>
                {demoLoading ? "Setting up a sample portfolio…" : "Try the demo - no sign-up"}
              </button>
              <p className="auth-demo-note">Explore a private sample journal with real prices. It's deleted after 24 hours.</p>
            </>
          )}
        </form>
      </section>
    </div>
  )
}

export default AuthScreen
