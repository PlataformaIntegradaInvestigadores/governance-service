from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi

from app import __version__
from app.api.routes import health_router, router
from app.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    application = FastAPI(
        title="Centinela Governance Service",
        description="Service bootstrap. Governance controls are not enabled.",
        version=__version__,
    )
    application.state.settings = settings
    application.include_router(health_router)
    application.include_router(router, prefix="/api/v1")

    @application.middleware("http")
    async def add_trace_id(request: Request, call_next):
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
