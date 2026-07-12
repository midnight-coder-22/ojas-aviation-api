# =============================================================================
# main.py — Ojas Aviation Operations API
# Entry point for the FastAPI application.
# Run locally with: uvicorn main:app --reload
# =============================================================================
from __future__ import annotations
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.dashboard import router as dashboard_router
from config import settings

# -----------------------------------------------------------------------------
# Application instance
# -----------------------------------------------------------------------------
app = FastAPI(
    title       = "Ojas Aviation Operations API",
    description = "KPI and operational analytics API powering the Ojas Aviation dashboard.",
    version     = "1.0.0",
    docs_url    = "/docs",      # Swagger UI  → http://localhost:8000/docs
    redoc_url   = "/redoc",     # ReDoc UI    → http://localhost:8000/redoc
)

# -----------------------------------------------------------------------------
# CORS Middleware
# Allows the GitHub Pages frontend to call this API from a different origin.
# In production, replace cors_origin with your exact GitHub Pages URL.
# Example: https://yourorg.github.io
# -----------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins     = [settings.cors_origin],
    allow_credentials = True,
    allow_methods     = ["GET"],   # This API is read-only — only GET is needed
    allow_headers     = ["*"],
)

# -----------------------------------------------------------------------------
# Routers
# -----------------------------------------------------------------------------
app.include_router(dashboard_router)

# -----------------------------------------------------------------------------
# Health check — used by deployment platforms and load balancers
# -----------------------------------------------------------------------------
@app.get("/health", tags=["Health"])
def health_check():
    """Confirms the API is running. Does not test the Databricks connection."""
    return {
        "status":  "ok",
        "service": "Ojas Aviation Operations API",
        "version": "1.0.0",
    }


# -----------------------------------------------------------------------------
# Root redirect info
# -----------------------------------------------------------------------------
@app.get("/", tags=["Health"])
def root():
    return {
        "message": "Ojas Aviation Operations API is running.",
        "docs":    "/docs",
        "health":  "/health",
    }
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

# =============================================================================
# models.py — Pydantic Request and Response Models
# =============================================================================


from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel


# =============================================================================
# AUTH
# =============================================================================

class LoginRequest(BaseModel):
    username:  str       # accepts username OR employee_id
    password:  str


class LoginResponse(BaseModel):
    access_token:    str
    token_type:      str = "bearer"
    user_id:         str
    full_name:       str
    username:        str
    role:            str
    department:      str
    dashboard_access: str
    can_edit_data:   bool
    can_flag:        bool
    can_resolve_flag: bool


# =============================================================================
# DASHBOARD — Work Order KPI
# =============================================================================

class WorkOrderKPI(BaseModel):
    wo_id:            str
    wo_name:          str
    dept_in_date:     Optional[date]
    wo_ageing_days:   Optional[int]
    dept_ageing_days: Optional[int]
    planned_qty:      int
    next_dept:        Optional[str]
    priority:         str
    status:           str
    expected_steps:   int
    done_steps:       int
    qc_alert:         bool
    mi_alert:         bool
    has_active_flag:  bool            # True if WO has an unresolved flag
    last_refreshed:   Optional[datetime]

    class Config:
        from_attributes = True


class DepartmentResponse(BaseModel):
    department:   str
    record_count: int
    data:         list[WorkOrderKPI]


class DepartmentSummary(BaseModel):
    department:         str
    total_wos:          int
    qc_alert_count:     int
    mi_alert_count:     int
    flagged_count:      int           # WOs with has_active_flag = True
    status_breakdown:   dict[str, int]
    priority_breakdown: dict[str, int]
    last_refreshed:     Optional[datetime]


# =============================================================================
# FLAGS
# =============================================================================

class FlagCreateRequest(BaseModel):
    wo_ids:     list[str]    # one or more WO IDs to flag in one action
    item_no:    Optional[str] = None
    department: str


class FlagResolveRequest(BaseModel):
    wo_ids: list[str]        # one or more WO IDs to resolve in one action


class FlagRecord(BaseModel):
    sr_no:         Optional[int]
    wo_id:         str
    item_no:       Optional[str]
    department:    str
    flag_status:   int
    raised_date:   Optional[datetime]
    resolved_date: Optional[datetime]
    raised_by:     Optional[str]
    resolved_by:   Optional[str]

    class Config:
        from_attributes = True


# =============================================================================
# EDIT DATA — Google Sheets Read
# =============================================================================

class SheetDataResponse(BaseModel):
    sheet_name:   str
    headers:      list[str]
    rows:         list[list]          # Raw rows as returned from Google Sheets
    total_rows:   int


class SheetWriteRequest(BaseModel):
    sheet_name:   str                 # "wos" or "ows"
    headers:      list[str]
    rows:         list[list]          # Full updated dataset to write back


class SheetWriteResponse(BaseModel):
    success:      bool
    message:      str
    rows_written: int
    job_triggered: bool               # Whether Databricks pipeline job was triggered


# =============================================================================
# main.py — Ojas Aviation Operations API Entry Point
# =============================================================================

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from routers.auth       import router as auth_router
from routers.dashboard  import router as dashboard_router
from routers.flags      import router as flags_router
from routers.edit_data  import router as edit_data_router
from config             import settings

# =============================================================================
# RATE LIMITER
# 300 requests per day per client IP.
# For authenticated endpoints, IP is a proxy for the user since the app is
# used from office/home networks. True per-user limiting would require Redis
# (out of scope to stay in Always Free tier).
# =============================================================================
limiter = Limiter(key_func=get_remote_address, default_limits=["300/day"])

# =============================================================================
# APP INSTANCE
# =============================================================================
app = FastAPI(
    title       = "Ojas Aviation Operations API",
    description = "KPI and operational analytics API for the Ojas Aviation dashboard.",
    version     = "2.0.0",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

# Attach rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# =============================================================================
# CORS
# =============================================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins     = [settings.cors_origin],
    allow_credentials = True,
    allow_methods     = ["GET", "POST"],
    allow_headers     = ["*"],
)

# =============================================================================
# ROUTERS
# =============================================================================
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(flags_router)
app.include_router(edit_data_router)

# =============================================================================
# HEALTH + ROOT
# =============================================================================
@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "service": "Ojas Aviation Operations API", "version": "2.0.0"}


@app.get("/", tags=["Health"])
def root():
    return {"message": "Ojas Aviation Operations API is running.", "docs": "/docs"}


# =============================================================================
# LOCAL RUN
# =============================================================================
if __name__ == "__main__":
    import uvicorn, os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)