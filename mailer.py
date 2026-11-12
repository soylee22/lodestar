"""Gmail SMTP helper. Same pattern as tools/min-jobs.

Credentials come from the environment: GMAIL_APP_PASSWORD is a GitHub Actions
secret, never committed. Without it, delivery fails without printing private message content.
"""
from __future__ import annotations
import os, smtplib, time
from email.message import EmailMessage

GMAIL_USER = os.environ.get("GMAIL_USER", "leeslater1992@gmail.com")
MAIL_TO = os.environ.get("MAIL_TO", "leeslater1992@gmail.com")


def email(subject: str, plain: str, html: str | None = None) -> bool:
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not pw:
        print("[mailer] Gmail credentials are missing")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Lodestar <{GMAIL_USER}>"
    msg["To"] = MAIL_TO
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
                smtp.login(GMAIL_USER, pw.replace(" ", ""))
                rejected = smtp.send_message(msg)
            if not rejected:
                print("[mailer] Gmail accepted the email")
                return True
        except Exception as exc:
            print("[mailer] Email attempt failed:", type(exc).__name__)
        if attempt < 2:
            time.sleep(2)
    return False


if __name__ == "__main__":
    email("Lodestar test", "If you can read this, SMTP delivery works.")
