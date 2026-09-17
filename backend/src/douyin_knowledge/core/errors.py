"""Domain error taxonomy (ARCHITECTURE.md section 21).

Every error carries a stable ``code`` so that ``jobs.last_error_json`` and
``processing_runs.error_json`` are machine-readable, and a ``retryable`` flag so
the worker does not have to pattern-match on messages.
"""

from __future__ import annotations

from typing import Any


class DKError(Exception):
    code = "internal_error"
    retryable = False
    http_status = 500

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "context": self.context,
        }


class ConfigurationError(DKError):
    code = "configuration_error"
    http_status = 500


class NotFoundError(DKError):
    code = "not_found"
    http_status = 404


class ValidationError(DKError):
    code = "validation_error"
    http_status = 422


class ConflictError(DKError):
    code = "conflict"
    http_status = 409


# ---- capture ------------------------------------------------------------
class CaptureUnavailable(DKError):
    code = "capture_unavailable"
    retryable = True
    http_status = 503


class AuthenticationRequired(DKError):
    code = "authentication_required"
    retryable = False
    http_status = 401


class CaptureRateLimited(DKError):
    """Distinct from CaptureUnavailable: the sidecar is healthy and told us to slow
    down. Same retry behaviour, but the operator's fix is different, so it gets its
    own code rather than being flattened into a generic transport failure."""

    code = "capture_rate_limited"
    retryable = True
    http_status = 429


class SourceUnavailable(DKError):
    code = "source_unavailable"
    retryable = False


class MediaDownloadFailed(DKError):
    code = "media_download_failed"
    retryable = True


# ---- processing ---------------------------------------------------------
class ASRFailed(DKError):
    code = "asr_failed"
    retryable = True


class OCRFailed(DKError):
    code = "ocr_failed"
    retryable = True


class ModelRateLimited(DKError):
    code = "model_rate_limited"
    retryable = True


class ModelUnavailable(DKError):
    code = "model_unavailable"
    retryable = True


class StructuredOutputInvalid(DKError):
    code = "structured_output_invalid"
    retryable = True


class EntityResolutionAmbiguous(DKError):
    code = "entity_resolution_ambiguous"
    retryable = False


class VectorIndexUnavailable(DKError):
    code = "vector_index_unavailable"
    retryable = True


class ProviderNotConfigured(ConfigurationError):
    code = "provider_not_configured"
