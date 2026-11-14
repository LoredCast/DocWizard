"""Outgoing mail for password resets and invitations (§13.2), via smtplib.

Without ``[smtp] host`` no mail is sent; the admin area then shows the links so
an admin can pass them on. Tests replace :attr:`Mailer.transport`.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .logs import fields

log = logging.getLogger("docwizard.mail")


class MailError(Exception):
    pass


class Mailer:
    def __init__(self, app):
        self.app = app
        self.transport = None  # a callable(EmailMessage) replaces SMTP, for tests

    @property
    def enabled(self) -> bool:
        return self.transport is not None or bool(self.app.cfg.smtp.host)

    def sender(self) -> str:
        smtp = self.app.cfg.smtp
        return smtp.sender or formataddr(("DocWizard", smtp.username or "docwizard@localhost"))

    def build(self, to: str, subject: str, body: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.sender()
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain="docwizard")
        msg.set_content(body)
        return msg

    def _send_smtp(self, msg: EmailMessage) -> None:
        smtp = self.app.cfg.smtp
        context = ssl.create_default_context()
        cls = smtplib.SMTP_SSL if smtp.tls else smtplib.SMTP
        kwargs = {"context": context} if smtp.tls else {}
        with cls(smtp.host, smtp.port, timeout=20, **kwargs) as conn:
            if smtp.starttls and not smtp.tls:
                conn.starttls(context=context)
            if smtp.username:
                conn.login(smtp.username, smtp.password)
            conn.send_message(msg)

    async def send(self, to: str, subject: str, body: str) -> None:
        if not self.enabled:
            raise MailError("Mail isn't set up on this server.")
        msg = self.build(to, subject, body)
        try:
            if self.transport is not None:
                self.transport(msg)
            else:
                await asyncio.to_thread(self._send_smtp, msg)
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("sending mail failed", extra=fields(error=str(exc)))
            raise MailError(f"The mail server refused the message: {exc}") from None
        log.info("mail sent", extra=fields(subject=subject))
