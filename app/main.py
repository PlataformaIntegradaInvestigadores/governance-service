from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi

from app import __version__
from app.api import supervisors
from app.api.routes import health_router, router
from app.application.record_qa_assessment import RecordQaAssessment
from app.application.supervision import AuthorizeSupervisor
from app.core.config import Settings
from app.infrastructure.identity_token_validator import (
    IdentityTokenValidator,
    create_identity_client,
)
from app.infrastructure.json_policy_repository import JsonPolicyRepository
from app.infrastructure.qa_response import assess_qa_response, retain_qa_content
from app.infrastructure.sqlite_assessment_repository import SqliteAssessmentRepository
from app.infrastructure.sqlite_supervisor_directory import SqliteSupervisorDirectory


def create_qa_assessor(policy_path: Path, database_path: Path) -> RecordQaAssessment:
    return RecordQaAssessment(
        JsonPolicyRepository(policy_path),
        SqliteAssessmentRepository(database_path),
        assess_qa_response,
        retain_qa_content,
    )


def create_supervisor_authorizer(settings: Settings) -> AuthorizeSupervisor | None:
    if settings.identity_base_url is None or settings.supervisors_database is None:
        return None
    client = create_identity_client(
        str(settings.identity_base_url), settings.identity_timeout_seconds
    )
    return AuthorizeSupervisor(
        IdentityTokenValidator(client),
        SqliteSupervisorDirectory(settings.supervisors_database),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    application = FastAPI(
        title="Centinela Governance Service",
        description="Service bootstrap. Governance controls are not enabled.",
        version=__version__,
    )
    application.state.settings = settings
    application.state.authorize_supervisor = create_supervisor_authorizer(settings)
    application.include_router(health_router)
    application.include_router(router, prefix="/api/v1")
    application.include_router(supervisors.router, prefix="/api/v1")
    supervisors.register_error_handlers(application)

    @application.middleware("http")
    async def add_trace_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        trace_id = str(uuid4())
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response

    def custom_openapi() -> dict:
        if application.openapi_schema is None:
            schema = get_openapi(
                title=application.title,
                version=application.version,
                description=application.description,
                routes=application.routes,
            )
            schema["paths"] = {
                path.removeprefix("/api/v1"): item
                for path, item in schema["paths"].items()
            }
            schema["servers"] = [{"url": settings.public_base_path}]
            application.openapi_schema = schema
        return application.openapi_schema

    application.openapi = custom_openapi
    return application


app = create_app()
