from fastapi import APIRouter

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
def status() -> dict:
    return {
        "service": "governance-service",
        "version": __version__,
        "stage": "bootstrap",
        "governance_enabled": False,
    }
