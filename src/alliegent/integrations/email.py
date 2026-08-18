from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from ..config import Secrets


def _send_sync(secrets: Secrets, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    sender = secrets.smtp_from or secrets.smtp_username
    message["From"] = f"Alliegent AI News <{sender}>"
    message["To"] = secrets.ai_news_email_to
    message.set_content(body)

    context = ssl.create_default_context()

    with smtplib.SMTP(secrets.smtp_host, secrets.smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(secrets.smtp_username, secrets.smtp_password)
        smtp.send_message(message)


async def send_ai_news_email(
    secrets: Secrets,
    subject: str,
    body: str,
) -> None:
    required = {
        "AI_NEWS_EMAIL_TO": secrets.ai_news_email_to,
        "SMTP_USERNAME": secrets.smtp_username,
        "SMTP_PASSWORD": secrets.smtp_password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Email is not configured; missing {', '.join(missing)}"
        )

    await asyncio.to_thread(_send_sync, secrets, subject, body)
