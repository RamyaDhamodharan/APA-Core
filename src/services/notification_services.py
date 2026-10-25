import os
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage


def send_notification(notification) -> bool:
    allowed = {
        item.strip().casefold()
        for item in os.getenv("TEST_MAILBOX_ALLOWLIST", "").split(",")
        if item.strip()
    }
    if notification.recipient_email.casefold() not in allowed:
        notification.status = "failed"
        notification.attempts += 1
        notification.last_error = "Recipient is not in TEST_MAILBOX_ALLOWLIST"
        return False
    if notification.attempts >= 3:
        notification.status = "failed"
        notification.last_error = "Retry limit reached"
        return False
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM", "")
    message["To"] = notification.recipient_email
    message["Subject"] = notification.subject or "AP Assistant notification"
    message.set_content(notification.body or "")
    notification.attempts += 1
    try:
        with smtplib.SMTP(
            os.getenv("SMTP_HOST", "localhost"), int(os.getenv("SMTP_PORT", "587"))
        ) as smtp:
            user, password = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
            if user:
                smtp.starttls()
                smtp.login(user, password or "")
            smtp.send_message(message)
        notification.status = "sent"
        notification.sent_at = datetime.now(UTC)
        notification.last_error = None
        return True
    except Exception as exc:
        notification.status = "failed"
        notification.last_error = str(exc)[:500]
        return False
