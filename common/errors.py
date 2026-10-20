class OutboundGateError(Exception):
    """Base exception whose message can be shown to the user."""


class ValidationError(OutboundGateError):
    """Input or file validation failed."""


class ConflictError(OutboundGateError):
    """A conflict over history, file name or content."""


class StorageError(OutboundGateError):
    """A file or the ledger could not be written reliably."""
