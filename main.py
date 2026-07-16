from __future__ import annotations

# FastAPI core imports
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Imports required for API rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

# Application configuration and API routers
from config import settings
from routers.auth import router as auth_router
from routers.dashboard import router as dashboard_router
from routers.flags import router as flags_router
from routers.edit_data import router as edit_data_router


# ------------------------------------------------------------------
# Configure API rate limiting
# ------------------------------------------------------------------
# Limits each client (identified by IP address) to 300 requests per day.
# This helps protect the API against abuse and accidental overuse.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["300/day"]
)


# ------------------------------------------------------------------
# Create the FastAPI application
# ------------------------------------------------------------------
# This metadata is displayed in the automatically generated Swagger
# documentation available at /docs.
app = FastAPI(
    title="Ojas Aviation Operations API",
    description="KPI and operational analytics API for the Ojas Aviation dashboard.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


# ------------------------------------------------------------------
# Register rate limiter with the application
# ------------------------------------------------------------------
# Stores the limiter inside the application state, registers the
# exception handler for rate limit violations, and enables the
# middleware that checks every incoming request.
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler
)
app.add_middleware(SlowAPIMiddleware)


# ------------------------------------------------------------------
# Configure Cross-Origin Resource Sharing (CORS)
# ------------------------------------------------------------------
# Reads the allowed frontend URLs from the application configuration.
# Multiple origins can be supplied as a comma-separated string.
cors_origins = [
    origin.strip()
    for origin in settings.cors_origin.split(",")
    if origin.strip()
]

# Enable cross-origin requests from approved frontend applications.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Register API route modules
# ------------------------------------------------------------------
# Each router groups endpoints belonging to a specific feature of the
# application, keeping the project modular and maintainable.
app.include_router(auth_router)        # Authentication endpoints
app.include_router(dashboard_router)   # Dashboard KPI endpoints
app.include_router(flags_router)       # Status flag endpoints
app.include_router(edit_data_router)   # Data editing endpoints


# ------------------------------------------------------------------
# Health Check Endpoint
# ------------------------------------------------------------------
# Used by deployment platforms, monitoring tools, or developers to
# verify that the API is running successfully.
@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status": "ok",
        "service": "Ojas Aviation Operations API",
        "version": "2.0.0",
    }


# ------------------------------------------------------------------
# Root Endpoint
# ------------------------------------------------------------------
# A simple landing endpoint that confirms the API is running and
# provides links to useful endpoints such as the documentation and
# health check.
@app.get("/", tags=["Health"])
def root():
    return {
        "message": "Ojas Aviation Operations API is running.",
        "docs": "/docs",
        "health": "/health",
    }