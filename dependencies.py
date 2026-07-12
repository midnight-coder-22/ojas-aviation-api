# =============================================================================
# dependencies.py — FastAPI Dependency Injection
#
# Provides reusable dependencies that routes inject via Depends():
#
#   get_current_user  — decode JWT from Authorization header, return user dict
#   require_role      — factory that returns a dependency checking role access
#
# Rate limiting is configured in main.py via slowapi and applied per-route.
# =============================================================================

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from security import decode_access_token

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """
    Extract and validate the JWT from the Authorization: Bearer <token> header.

    Returns the full token payload dict which contains:
        user_id, username, full_name, role, department,
        dashboard_access, can_edit_data, can_flag, can_resolve_flag

    Raises HTTP 401 if the token is missing, expired, or invalid.
    Raises HTTP 403 if the user's account_status is 0 (disabled).
    """
    token   = credentials.credentials
    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Invalid or expired token. Please log in again.",
            headers     = {"WWW-Authenticate": "Bearer"},
        )

    if not payload.get("account_status", 1):
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Your account has been disabled. Contact your administrator.",
        )

    return payload


def require_role(*allowed_roles: str):
    """
    Dependency factory. Returns a dependency that raises HTTP 403 if the
    current user's role is not in allowed_roles.

    Usage in a route:
        @router.post("/something")
        def do_something(user = Depends(require_role("Admin", "Executive"))):
            ...
    """
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code = status.HTTP_403_FORBIDDEN,
                detail      = f"Access denied. Required roles: {', '.join(allowed_roles)}.",
            )
        return user
    return _check


def require_permission(permission_field: str):
    """
    Dependency factory that checks a specific boolean permission field
    from the JWT payload (can_edit_data, can_flag, can_resolve_flag).

    Usage:
        @router.post("/flags")
        def add_flag(user = Depends(require_permission("can_flag"))):
            ...
    """
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if not user.get(permission_field):
            raise HTTPException(
                status_code = status.HTTP_403_FORBIDDEN,
                detail      = f"You do not have permission to perform this action.",
            )
        return user
    return _check