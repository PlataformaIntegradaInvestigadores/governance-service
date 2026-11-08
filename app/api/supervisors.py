from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.domain.supervision import (
    AuthenticationError,
    IdentityUnavailableError,
    Supervisor,
    SupervisorNotEnabledError,
)


class SupervisionNotConfiguredError(Exception):
    """Identity or the supervisor list is not configured for this deployment."""


_bearer = HTTPBearer(auto_error=False)
_ERRORS: dict[type[Exception], tuple[int, str]] = {
    AuthenticationError: (401, "AUTHENTICATION_REQUIRED"),
    SupervisorNotEnabledError: (403, "SUPERVISOR_NOT_ENABLED"),
    IdentityUnavailableError: (503, "IDENTITY_UNAVAILABLE"),
    SupervisionNotConfiguredError: (503, "SUPERVISION_NOT_CONFIGURED"),
}

router = APIRouter(prefix="/supervisors", tags=["Supervisors"])


def current_supervisor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Supervisor:
    authorizer = request.app.state.authorize_supervisor
    if authorizer is None:
        raise SupervisionNotConfiguredError
    return authorizer.execute(credentials.credentials if credentials else None)


@router.get("/me")
def me(supervisor: Annotated[Supervisor, Depends(current_supervisor)]) -> dict:
    return {
        "identity_id": supervisor.identity_id,
        "display_name": supervisor.display_name,
    }


def _error_response(request: Request, error: Exception) -> JSONResponse:
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
