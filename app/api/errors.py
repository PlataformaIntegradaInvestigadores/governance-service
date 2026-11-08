from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.resolution import (
    JustificationRequiredError,
    ReviewCaseAlreadyResolvedError,
    ReviewCaseExpiredError,
    ReviewCaseNotFoundError,
)
from app.domain.supervision import (
    AuthenticationError,
    IdentityUnavailableError,
    SupervisorNotEnabledError,
)


class SupervisionNotConfiguredError(Exception):
    """Identity or the local database is not configured for this deployment."""


_ERRORS: dict[type[Exception], tuple[int, str]] = {
    AuthenticationError: (401, "AUTHENTICATION_REQUIRED"),
    SupervisorNotEnabledError: (403, "SUPERVISOR_NOT_ENABLED"),
    ReviewCaseNotFoundError: (404, "CASE_NOT_FOUND"),
    ReviewCaseAlreadyResolvedError: (409, "CASE_ALREADY_RESOLVED"),
    ReviewCaseExpiredError: (409, "CASE_EXPIRED"),
    JustificationRequiredError: (422, "JUSTIFICATION_REQUIRED"),
    RequestValidationError: (422, "INVALID_REQUEST"),
    IdentityUnavailableError: (503, "IDENTITY_UNAVAILABLE"),
    SupervisionNotConfiguredError: (503, "SUPERVISION_NOT_CONFIGURED"),
}


def _error_response(request: Request, error: Exception) -> JSONResponse:
    """Code and trace ID only: no input echo, internal detail or content."""
    status_code, code = _ERRORS[type(error)]
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return JSONResponse(
        {"code": code, "trace_id": request.state.trace_id},
        status_code=status_code,
        headers=headers,
    )


def register_error_handlers(application: FastAPI) -> None:
    for error_type in _ERRORS:
        application.add_exception_handler(error_type, _error_response)
