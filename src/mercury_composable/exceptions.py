"""
Exceptions for Mercury Composable polyglot functions.

AppException mirrors the Java/Rust engines' AppException: an intentional
application error carrying an HTTP-style status code and a message. Raised
from a function handler, it becomes the portable error contract on the wire:
envelope status (>= 400) + body (the error message).
"""


class AppException(Exception):
    """Intentional application error with an HTTP-style status code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)

    def __repr__(self) -> str:
        return f"AppException(status={self.status}, message={self.message!r})"


class CompactFormatError(ValueError):
    """The payload used the classic compact wire format (single-character map keys).

    This implementation speaks the language-neutral standard format only.
    Engines default to standard for Event over HTTP; a caller configured with
    ``event.over.http.format=compact`` must switch back to ``standard``.
    """
