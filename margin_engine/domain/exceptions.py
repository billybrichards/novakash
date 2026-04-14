"""
Domain exceptions — structured error types for the margin engine domain.
"""

from __future__ import annotations
from typing import List


class DomainException(Exception):
    """Base domain exception."""

    pass


class DomainValidationError(DomainException):
    """Validation error in domain entities/value objects."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class EntityNotFoundError(DomainException):
    """Entity not found in repository."""

    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} with ID {entity_id} not found")


class BusinessRuleViolationError(DomainException):
    """Business rule violated."""

    def __init__(self, rule: str, context: str = ""):
        self.rule = rule
        super().__init__(
            f"Business rule violated: {rule}" + (f" ({context})" if context else "")
        )
