"""Shared error types for runtime, tools, and services."""

from __future__ import annotations


class CoursePilotError(Exception):
    """Base error for typed handling across runtime/services."""


class UserInputError(CoursePilotError):
    """User-provided content is invalid or incomplete."""


class IndexNotReadyError(CoursePilotError):
    """Course index is missing or not ready for retrieval."""


class ToolDeniedError(CoursePilotError):
    """Tool call rejected by policy or permission mode."""


class ToolTransientError(CoursePilotError):
    """Tool call failed due to retryable/transient conditions."""


class LLMProviderError(CoursePilotError):
    """LLM provider invocation failed."""
