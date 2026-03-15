"""Health and readiness endpoints for Kubernetes probes."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/healthz")
async def healthz():
    """Liveness probe — returns 200 if the process is alive."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    """Readiness probe — returns 200 only if DB and Redis are reachable."""
    try:
        await request.app.state.db.fetchval("SELECT 1")
        await request.app.state.redis.ping()
    except Exception as e:
        return JSONResponse(
            {"status": "not ready", "error": str(e)},
            status_code=503,
        )
    return {"status": "ready"}
