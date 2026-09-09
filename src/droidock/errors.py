class DroidockError(Exception):
    """A recoverable operation failure; callers can branch on ``code``."""

    def __init__(self, message: str, *, code: str = "connection_error") -> None:
        super().__init__(message)
        self.code = code


class IdentityError(DroidockError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="identity_mismatch")
