"""HTTP entry point; domain logic will live outside API routes."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from traceforge.api.execution import router as execution_router
from traceforge.api.routes import router

app = FastAPI(title="TraceForge", version="0.1.0")
app.include_router(router)
app.include_router(execution_router)


class HealthResponse(BaseModel):
    status: Literal["ok"]


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    """Report process liveness, independently of database availability."""
    return HealthResponse(status="ok")
