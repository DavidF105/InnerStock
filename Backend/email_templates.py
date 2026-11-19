# The emails InnerStock sends. Each function returns (subject, html, text).
#
# Email HTML is not web HTML: many email apps ignore <style> blocks and
# modern CSS, so these use simple tables and inline styles, a light
# background (most inboxes are light), and always come with a plain-text
# version. Everything taken from data is escaped, since recap text and
# news headlines come from outside the app.
from html import escape

ACCENT = "#0f9d8a"
TEXT = "#1f2328"
MUTED = "#6b7280"


def _layout(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f4f5f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:12px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{TEXT};">
<tr><td style="padding:24px 28px 8px;font-size:15px;font-weight:600;color:{ACCENT};">InnerStock</td></tr>
<tr><td style="padding:0 28px;"><h1 style="margin:8px 0 16px;font-size:20px;line-height:1.35;">{escape(title)}</h1></td></tr>
<tr><td style="padding:0 28px 28px;font-size:15px;line-height:1.6;">{body_html}</td></tr>
</table>
</td></tr></table>
</body></html>"""


def _button(url: str, label: str) -> str:
    return (f'<p style="margin:24px 0 8px;"><a href="{escape(url)}" style="display:inline-block;background:{ACCENT};'
            f'color:#ffffff;text-decoration:none;font-weight:600;padding:11px 20px;border-radius:8px;">{escape(label)}</a></p>')


def _small(text: str) -> str:
    return f'<p style="margin:16px 0 0;font-size:12px;color:{MUTED};">{text}</p>'


# --- Earnings recap ("Email me this") ---------------------------------------------------

def _pct(value):
    return f"{value:+.1f}%" if value is not None else "n/a"


def recap_email(ticker: str, recap, app_link: str):
    company = recap.company_name or ticker
    subject = f"{ticker} earnings: {recap.headline}"
    eps = (f"${recap.eps_actual:.2f} vs ${recap.eps_estimate:.2f} expected ({_pct(recap.surprise_pct)})"
           if recap.eps_actual is not None and recap.eps_estimate is not None else "n/a")
    sources = recap.sources_list

    stat_rows = "".join(
        f'<tr><td style="padding:4px 0;color:{MUTED};width:150px;">{escape(label)}</td><td style="padding:4px 0;font-weight:600;">{escape(value)}</td></tr>'
        for label, value in [
            ("Reported", recap.report_date.strftime("%a, %b %d, %Y")),
            ("EPS", eps),
            ("Stock reaction", f"{_pct(recap.reaction_pct)} (first close after)"),
            ("Overall read", recap.sentiment.capitalize()),
        ]
    )
    source_items = "".join(
        f'<li style="margin:0 0 6px;"><a href="{escape(s["url"])}" style="color:{ACCENT};">{escape(s["title"])}</a>'
        + (f' <span style="color:{MUTED};">· {escape(s["publisher"])}</span>' if s.get("publisher") else "") + "</li>"
        for s in sources
    )
    html = _layout(f"{ticker} · {recap.headline}", f"""
<p style="margin:0 0 4px;color:{MUTED};">{escape(company)}</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 16px;font-size:14px;">{stat_rows}</table>
<h2 style="font-size:15px;margin:20px 0 6px;">What happened</h2>
<p style="margin:0;">{escape(recap.what_happened)}</p>
<h2 style="font-size:15px;margin:20px 0 6px;">What it means for {escape(company)}</h2>
<p style="margin:0;">{escape(recap.what_it_means)}</p>
{f'<h2 style="font-size:15px;margin:20px 0 6px;">Sources</h2><ul style="margin:0;padding-left:18px;font-size:14px;">{source_items}</ul>' if sources else ''}
{_button(app_link, "Open in InnerStock")}
{_small("Written by Claude from Yahoo Finance numbers and headlines. A summary, not investment advice. You got this because you pressed “Email me this” in InnerStock.")}
""")
    text_sources = "\n".join(f"- {s['title']}: {s['url']}" for s in sources)
    text = f"""{ticker} earnings: {recap.headline}
{company}

Reported: {recap.report_date.isoformat()}
EPS: {eps}
Stock reaction: {_pct(recap.reaction_pct)} (first close after)
Overall read: {recap.sentiment}

WHAT HAPPENED
{recap.what_happened}

WHAT IT MEANS FOR {company.upper()}
{recap.what_it_means}
{f"{chr(10)}SOURCES{chr(10)}{text_sources}{chr(10)}" if sources else ""}
Open in InnerStock: {app_link}

Written by Claude from Yahoo Finance numbers and headlines. A summary, not investment advice.
"""
    return subject, html, text


# --- Account emails ------------------------------------------------------------------------

def verify_email(name: str, link: str, hours: int):
    subject = "Confirm your email for InnerStock"
    html = _layout("Confirm your email", f"""
<p style="margin:0;">Hi {escape(name)}, click below to confirm this is your email address. Once it's confirmed you can send earnings recaps to your inbox.</p>
{_button(link, "Confirm my email")}
{_small(f"This link works for {hours} hours. If you didn't create an InnerStock account, you can ignore this email.")}
""")
    text = (f"Hi {name},\n\nConfirm this is your email address for InnerStock:\n{link}\n\n"
            f"This link works for {hours} hours. If you didn't create an InnerStock account, ignore this email.\n")
    return subject, html, text


def reset_email(name: str, link: str, minutes: int):
    subject = "Reset your InnerStock password"
    html = _layout("Reset your password", f"""
<p style="margin:0;">Hi {escape(name)}, someone (hopefully you) asked to reset your InnerStock password. Click below to choose a new one.</p>
{_button(link, "Choose a new password")}
{_small(f"This link works once, for {minutes} minutes. If you didn't ask for this, ignore this email - your password won't change.")}
""")
    text = (f"Hi {name},\n\nReset your InnerStock password here:\n{link}\n\n"
            f"This link works once, for {minutes} minutes. If you didn't ask for this, ignore this email.\n")
    return subject, html, text
