from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import APIKeyHeader

from app.api.errors import SupervisionNotConfiguredError
from app.domain.supervision import AuthenticationError, Supervisor

_admin_token = APIKeyHeader(name="X-Admin-Token", auto_error=False)

router = APIRouter(prefix="/supervisors", tags=["Supervisors"])


def current_supervisor(
    request: Request,
    token: Annotated[str | None, Depends(_admin_token)],
) -> Supervisor:
    if not token or len(request.headers.getlist("X-Admin-Token")) != 1:
        raise AuthenticationError("One administrator token is required")
    authorizer = request.app.state.authorize_supervisor
    if authorizer is None:
        raise SupervisionNotConfiguredError
    return authorizer.execute(token)


@router.get("/me")
def me(
    supervisor: Annotated[Supervisor, Depends(current_supervisor)], response: Response
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"role": supervisor.identity_id}
