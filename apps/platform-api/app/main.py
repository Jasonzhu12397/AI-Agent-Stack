from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import database_is_ready

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="A modular, policy-governed AI Agent platform.",
)


@app.get("/health/live", tags=["health"])
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def readiness() -> JSONResponse:
    if not database_is_ready():
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ready", "database": "ok"})


@app.get("/", tags=["system"])
def root() -> dict[str, str]:
    return {"name": settings.app_name, "version": "0.1.0", "environment": settings.app_env}
