"""Transactional email transport.

A technical concern only: this module knows how to put a message on the wire,
never why it is being sent. Subjects and bodies belong to the domain that sends
them - for password recovery, `services/auth/emails.py`.

Which backend runs is decided by configuration, not by a flag at the call site:

- `RESEND_API_KEY` set  -> Resend, over its HTTPS API.
- key absent            -> the console backend, which logs the message in full.

The console backend exists so the whole recovery flow can be walked locally
without an email account. It prints the reset link, which is exactly what the
recipient would have clicked.
"""

import logging

import resend
from resend.exceptions import ResendError

from services.core.config import get_settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    """The provider refused the message or could not be reached."""


def _send_via_resend(sender: str, recipient: str, subject: str, html: str, text: str) -> str:
    settings = get_settings()
    # The SDK reads its credential from module state; set it per call so a
    # rotated key is picked up without a restart.
    resend.api_key = settings.resend_api_key

    params: resend.Emails.SendParams = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": html,
        "text": text,
    }
    try:
        response = resend.Emails.send(params)
    except ResendError as error:
        raise EmailDeliveryError(f"Resend rejected the message: {error}") from error
    except Exception as error:  # network failure, DNS, TLS - never a 200
        raise EmailDeliveryError(f"Could not reach Resend: {error}") from error

    return response["id"]


def _send_via_console(sender: str, recipient: str, subject: str, text: str) -> str:
    logger.warning(
        "RESEND_API_KEY is not set, so no mail was sent. The message follows.\n"
        "From:    %s\nTo:      %s\nSubject: %s\n\n%s",
        sender,
        recipient,
        subject,
        text,
    )
    return "console"


def send_email(recipient: str, subject: str, html: str, text: str) -> str:
    """Send one message and return the provider's id for it.

    Raises `EmailDeliveryError` when the provider is configured but the send
    failed. The console backend cannot fail.
    """
    settings = get_settings()
    if not settings.email_is_live:
        return _send_via_console(settings.email_from, recipient, subject, text)
    return _send_via_resend(settings.email_from, recipient, subject, html, text)
