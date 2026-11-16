# Sending email - the one place the app talks to an email service.
#
# Uses a Gmail account through Gmail's SMTP server (smtp.gmail.com, port
# 465, encrypted). Gmail won't accept the account's normal password from
# an app, so it needs an "app password" (Google Account -> Security ->
# 2-Step Verification -> App passwords). Settings, in .env locally and in
# the Vercel project for production:
#   GMAIL_ADDRESS       the Gmail account emails are sent from
#   GMAIL_APP_PASSWORD  its 16-character app password (spaces are fine)
#   APP_URL             where links in emails point, e.g. https://innerstock.vercel.app
#
# Gmail allows roughly 500 emails a day from a regular account - plenty for
# "Email me this", password resets and confirmations at this size. Moving to
# a dedicated email service later only means changing send_email().
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SENDER_NAME = "InnerStock"


class EmailNotConfigured(Exception):
    """GMAIL_ADDRESS / GMAIL_APP_PASSWORD aren't set."""


class EmailSendFailed(Exception):
    """Gmail refused or couldn't be reached."""


def is_configured() -> bool:
    return bool(os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"))


def app_url() -> str:
    return (os.getenv("APP_URL") or "http://localhost:5173").rstrip("/")


def send_email(to: str, subject: str, html: str, text: str) -> None:
    """Sends one email with an HTML body and a plain-text fallback. Raises on failure."""
    if not is_configured():
        raise EmailNotConfigured()
    address = os.getenv("GMAIL_ADDRESS").strip()
    # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
    password = os.getenv("GMAIL_APP_PASSWORD").replace(" ", "")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((SENDER_NAME, address))
    message["To"] = to
    message.set_content(text)                    # for email apps that don't show HTML
    message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=20) as server:
            server.login(address, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as e:
        print(f"[email] sending to {to} failed: {e}")
        raise EmailSendFailed() from e
