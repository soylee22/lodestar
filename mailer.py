"""Gmail SMTP helper. Same pattern as tools/min-jobs.

Credentials come from the environment: GMAIL_APP_PASSWORD is a GitHub Actions
secret, never committed. Without it the message is printed instead of sent, so
the scripts stay safe to run locally.
"""
from __future__ import annotations
import os, smtplib
from email.message import EmailMessage

GMAIL_USER = os.environ.get("GMAIL_USER", "leeslater1992@gmail.com")
MAIL_TO = os.environ.get("MAIL_TO", "leeslater1992@gmail.com")


def email(subject: str, plain: str, html: str | None = None) -> bool:
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not pw:
        print(f"[mailer] no GMAIL_APP_PASSWORD set; would send '{subject}':\n"
              + "-" * 50 + "\n" + plain[:1500] + "\n" + "-" * 50)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Lodestar <{GMAIL_USER}>"
    msg["To"] = MAIL_TO
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(GMAIL_USER, pw.replace(" ", ""))
            smtp.send_message(msg)
        print(f"[mailer] sent '{subject}' to {MAIL_TO}")
        return True
    except Exception as e:
        print("[mailer] send failed:", e)
        return False


if __name__ == "__main__":
    email("Lodestar test", "If you can read this, SMTP delivery works.")
