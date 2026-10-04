"""Welcome and password-reset emails. Return True when sent; False when email is not configured or
failed, so the caller can show the link on screen instead."""
from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import settings
from .models import User

log = logging.getLogger(__name__)
_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=select_autoescape(["html"]))


def _send(user: User, kind: str, link: str, by: str = "") -> bool:
    if not settings.mail_enabled:
        log.info("mail not configured; %s link for %s not sent", kind, user.email)
        return False
    html = _env.get_template("account_email.html").render(kind=kind, user=user, link=link, by=by, settings=settings,
                                                          sso=settings.sso_enabled, domains=settings.sso_allowed_domains)
    if kind == "welcome":
        subject = "Your Review Manager account"
        text = (f"Hi {user.first_name},\n\n{by or 'An administrator'} set up your Review Manager account ({user.email}).\n"
                f"Set your password here (link valid 48 hours): {link}\n\n"
                + (f"If you have a {', '.join(settings.sso_allowed_domains)} Microsoft account you can also just use Sign in with Microsoft at {settings.app_base_url}/login.\n" if settings.sso_enabled else ""))
    else:
        subject = "Reset your Review Manager password"
        text = f"Someone asked to reset the password for {user.email}. If that was you, use this link within 2 hours: {link}\n\nIf not, ignore this email; nothing changes."
    try:
        from .mailer import send_email
        send_email([user.email], subject, html, text)
        return True
    except Exception:
        log.exception("could not send %s email to %s", kind, user.email)
        return False


def send_welcome(user: User, link: str, by: str = "") -> bool:
    return _send(user, "welcome", link, by)


def send_reset(user: User, link: str) -> bool:
    return _send(user, "reset", link)
