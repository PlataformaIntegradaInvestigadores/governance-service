from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi

from app import __version__
from app.api import governed_qa, qa_samples, review_cases, supervisors
from app.api.errors import register_error_handlers
from app.api.routes import health_router, router
from app.application.record_qa_assessment import RecordQaAssessment
from app.application.resolve_review import ReviewCases
from app.application.supervision import AuthorizeSupervisor
from app.core.config import Settings
from app.infrastructure.admin_token_validator import (
    SearchAdminTokenValidator,
    create_admin_client,
)
from app.infrastructure.json_policy_repository import JsonPolicyRepository
from app.infrastructure.qa_response import assess_qa_response, retain_qa_content
from app.infrastructure.qa_upstream import QaUpstream
from app.infrastructure.sqlite_assessment_repository import (
    SqliteAssessmentRepository,
    SqliteQaDeliveryRepository,
    SqliteReviewCaseRepository,
    SqliteReviewResolutionRepository,
)


def create_qa_assessor(policy_path: Path, database_path: Path) -> RecordQaAssessment:
    return RecordQaAssessment(
        JsonPolicyRepository(policy_path),
        SqliteAssessmentRepository(database_path),
        assess_qa_response,
        retain_qa_content,
    )


def create_supervisor_authorizer(settings: Settings) -> AuthorizeSupervisor | None:
    if settings.admin_verifier_base_url is None:
        return None
    client = create_admin_client(
        str(settings.admin_verifier_base_url), settings.admin_verifier_timeout_seconds
    )
    return AuthorizeSupervisor(SearchAdminTokenValidator(client))


def create_review_cases(settings: Settings) -> ReviewCases | None:
    if settings.database is None:
        return None
    return ReviewCases(
        SqliteAssessmentRepository(settings.database),
        SqliteReviewCaseRepository(settings.database),
        SqliteReviewResolutionRepository(settings.database),
        timedelta(minutes=settings.review_window_minutes),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    application = FastAPI(
        title="Centinela Governance Service",
        description="Local Q&A governance and supervisor review service.",
        version=__version__,
    )
    application.state.settings = settings
    application.state.authorize_supervisor = create_supervisor_authorizer(settings)
    application.state.review_cases = create_review_cases(settings)
    application.state.qa_upstream = None
    application.state.qa_release_key = None
    application.state.qa_deliveries = None
    if settings.qa_upstream_base_url is not None:
        application.state.qa_upstream = QaUpstream(
            str(settings.qa_upstream_base_url), settings.qa_timeout_seconds
        )
        application.state.qa_assessor = create_qa_assessor(
            settings.qa_policy_path, settings.database
        )
        application.state.qa_deliveries = SqliteQaDeliveryRepository(
            settings.database, settings.qa_retain_synthetic_deliveries
        )
        application.state.qa_release_key = (
            settings.qa_release_key.get_secret_value().encode()
        )
    application.include_router(health_router)
    application.include_router(router, prefix="/api/v1")
    application.include_router(supervisors.router, prefix="/api/v1")
    application.include_router(review_cases.router, prefix="/api/v1")
    application.include_router(governed_qa.router, prefix="/api/v1")
    application.include_router(qa_samples.router, prefix="/api/v1")
    register_error_handlers(application)

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
