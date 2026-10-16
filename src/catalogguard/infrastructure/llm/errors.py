"""LLM transport errors. Permanent failures are not retried."""

from catalogguard.domain.errors import NonRetryableError


class TransientLLMError(Exception):
    """Timeouts, connection failures, and retryable HTTP statuses."""


class PermanentLLMError(NonRetryableError):
    """The request itself is rejected. Another attempt will not help."""
