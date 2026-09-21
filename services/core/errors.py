class DomainError(Exception):
    """Base class for business rule violations raised by the service layer.

    Routers translate these into HTTP responses; services stay HTTP-agnostic.
    """


class EmailAlreadyRegistered(DomainError):
    def __init__(self, email: str) -> None:
        super().__init__(f"Email {email} is already registered")
        self.email = email
