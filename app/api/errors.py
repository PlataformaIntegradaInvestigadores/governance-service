from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.qa_sampling import InsufficientDeliveriesError, QaSampleNotFoundError
from app.domain.resolution import (
    InvalidRecommendationResolutionError,
    JustificationRequiredError,
    ReviewCaseAlreadyResolvedError,
    ReviewCaseExpiredError,
    ReviewCaseNotFoundError,
)
from app.domain.supervision import (
    AdminTokenRejectedError,
    AdminVerifierUnavailableError,
    AuthenticationError,
)


class SupervisionNotConfiguredError(Exception):
    """The admin verifier or local review database is not configured."""


_ERRORS: dict[type[Exception], tuple[int, str]] = {
    AuthenticationError: (401, "AUTHENTICATION_REQUIRED"),
    AdminTokenRejectedError: (403, "ADMIN_TOKEN_REJECTED"),
    ReviewCaseNotFoundError: (404, "CASE_NOT_FOUND"),
    QaSampleNotFoundError: (404, "QA_SAMPLE_NOT_FOUND"),
    InsufficientDeliveriesError: (409, "INSUFFICIENT_QA_DELIVERIES"),
    ReviewCaseAlreadyResolvedError: (409, "CASE_ALREADY_RESOLVED"),
    ReviewCaseExpiredError: (409, "CASE_EXPIRED"),
    JustificationRequiredError: (422, "JUSTIFICATION_REQUIRED"),
    InvalidRecommendationResolutionError: (422, "INVALID_RECOMMENDATION_RESOLUTION"),
    RequestValidationError: (422, "INVALID_REQUEST"),
    AdminVerifierUnavailableError: (503, "ADMIN_VERIFIER_UNAVAILABLE"),
    SupervisionNotConfiguredError: (503, "SUPERVISION_NOT_CONFIGURED"),
}


def _error_response(request: Request, error: Exception) -> JSONResponse:
    """Code and trace ID only: no input echo, internal detail or content."""
    status_code, code = _ERRORS[type(error)]
    headers = {}
    if request.url.path.startswith(
        (
            "/api/v1/supervisors/",
            "/api/v1/review-cases/",
            "/api/v1/qa-samples/",
            "/api/v1/governed/recommender/",
        )
    ):
        headers["Cache-Control"] = "no-store"
        if status_code == 401:
            headers["WWW-Authenticate"] = "X-Admin-Token"
    return JSONResponse(
        {"code": code, "trace_id": request.state.trace_id},
        status_code=status_code,
        headers=headers,
    )


def register_error_handlers(application: FastAPI) -> None:
    for error_type in _ERRORS:
        application.add_exception_handler(error_type, _error_response)
