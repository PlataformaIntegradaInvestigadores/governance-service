from fastapi import APIRouter, Request

from app import __version__

health_router = APIRouter()
router = APIRouter()


@health_router.get("/health", include_in_schema=False)
def health() -> dict:
    return {
        "server_name": "governance-service",
        "ip_address": "unknown",
        "global_status": "Online",
        "groups": [
            {
                "group_name": "Service runtime",
                "group_status": "Operativo",
                "services": [{"name": "api", "status": "ok"}],
            }
        ],
    }


@router.get("/status", tags=["Service"])
def status(request: Request) -> dict:
    enabled = request.app.state.qa_upstream is not None
    return {
        "service": "governance-service",
        "version": __version__,
        "stage": "governed-qa" if enabled else "bootstrap",
        "governance_enabled": enabled,
    }
