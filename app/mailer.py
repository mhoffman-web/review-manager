"""Outbound email over SMTP (Microsoft 365 SMTP AUTH by default)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import List, Optional

from .config import settings

log = logging.getLogger(__name__)


def send_email(to: List[str], subject: str, html: str, text: Optional[str] = None) -> None:
    if not to:
        raise ValueError("no recipients")
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text or "This report is best viewed in an HTML-capable mail client.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
        smtp.ehlo()
        if settings.smtp_starttls:
            smtp.starttls()
            smtp.ehlo()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    log.info("sent '%s' to %s", subject, ", ".join(to))
