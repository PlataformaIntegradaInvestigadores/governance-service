from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.errors import SupervisionNotConfiguredError
from app.domain.supervision import Supervisor

_bearer = HTTPBearer(auto_error=False)

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
