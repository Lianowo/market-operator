"""Unified domain exception hierarchy for ``market_operator``.

All custom business exceptions inherit from :class:`MarketOperatorError`, which
itself subclasses :class:`RuntimeError` so existing callers catching
``RuntimeError`` remain 100% compatible while enabling precise domain-level
error handling and structured logging.
"""

from __future__ import annotations


class MarketOperatorError(RuntimeError):
    """Base exception for all ``market_operator`` business errors."""


class ConfigurationError(MarketOperatorError):
    """Invalid, incomplete, or inconsistent configuration."""


class DataIntegrityError(MarketOperatorError):
    """Corrupt, missing, unsorted, or schema-invalid market data or manifest."""


class StrategyExecutionError(MarketOperatorError):
    """Invariant violation during strategy, broker, or campaign execution."""


class ExchangeError(MarketOperatorError):
    """Exchange API, protocol, signature, or transport failure."""


class CredentialsError(MarketOperatorError):
    """Missing, invalid, or leaked API/notification credentials."""


class LayoutError(MarketOperatorError):
    """Invalid storage layout configuration or migration precondition."""


class NotificationError(MarketOperatorError):
    """Notification channel transport or API error (e.g. Telegram)."""


class RateLimitError(NotificationError):
    """Upstream rate-limit response carrying a bounded retry delay."""

    def __init__(self, method: str, retry_after: float) -> None:
        self.method = str(method)
        self.retry_after = float(retry_after)
        super().__init__(f"{self.method} 限流(HTTP 429), 建议等待 {self.retry_after:.0f}s")


class LockAcquisitionError(MarketOperatorError):
    """Failed to acquire a single-instance runtime lock."""


__all__ = [
    "MarketOperatorError",
    "ConfigurationError",
    "DataIntegrityError",
    "StrategyExecutionError",
    "ExchangeError",
    "CredentialsError",
    "LayoutError",
    "NotificationError",
    "RateLimitError",
    "LockAcquisitionError",
]
