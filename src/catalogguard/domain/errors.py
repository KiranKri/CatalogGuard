"""Domain errors. The API layer maps these to HTTP responses."""


class NonRetryableError(Exception):
    """The event bus dead-letters this instead of trying again."""


class DomainError(Exception):
    code = "domain_error"
    http_status = 400


class NotFoundError(DomainError, NonRetryableError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class OptimisticConflictError(ConflictError):
    """Inventory version did not match. The caller must re-read and retry."""

    code = "optimistic_conflict"

    def __init__(self, message: str, *, current_version: int) -> None:
        super().__init__(message)
        self.current_version = current_version


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"


class InvalidRequestError(DomainError):
    code = "invalid_request"
    http_status = 400


class CostLimitError(DomainError, NonRetryableError):
    """The SKU has used its LLM calls for the UTC day. Do not retry."""

    code = "llm_cost_limit"
    http_status = 429
