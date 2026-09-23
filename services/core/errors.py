class DomainError(Exception):
    """Base class for business rule violations raised by the service layer.

    Routers translate these into HTTP responses; services stay HTTP-agnostic.
    """


class EmailAlreadyRegistered(DomainError):
    def __init__(self, email: str) -> None:
        super().__init__(f"Email {email} is already registered")
        self.email = email


class InvalidResetToken(DomainError):
    """A password reset token that cannot be honoured.

    `reason` says which way it failed - forged, expired, unknown or already
    spent. It is for the log only: the router answers every case with the same
    message, so a caller cannot use the response to tell a real but spent token
    from one they invented.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"Password reset token {reason}")
        self.reason = reason


class IncorrectPassword(DomainError):
    """The current password supplied to a change-password request did not match."""

    def __init__(self) -> None:
        super().__init__("Current password is incorrect")
