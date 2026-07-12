# =============================================================================
# routers/auth.py — Authentication Routes
#
# POST /api/auth/login   — validate credentials, return JWT
# GET  /api/auth/me      — return current user profile from JWT
# =============================================================================

from fastapi import APIRouter, HTTPException, status, Depends
from database import fetch_all
from models import LoginRequest, LoginResponse
from security import verify_password, create_access_token 
from dependencies import get_current_user

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


# -----------------------------------------------------------------------------
# POST /api/auth/login
# Accepts username (or employee_id) + password.
# Returns a long-lived JWT on success.
# -----------------------------------------------------------------------------
@router.post("/login", response_model=LoginResponse, summary="User login")
def login(body: LoginRequest):
    print(body)
    """
    Authenticate a user by username (or employee_id) and password.

    On success returns a JWT access token that the frontend stores in
    localStorage and sends as Authorization: Bearer <token> on every
    subsequent request.

    Raises 401 if credentials are wrong or account is disabled.
    """
    # Look up user by username OR employee_id
    rows = fetch_all(
        """
        SELECT *
        FROM   ojas_aviation.users
        WHERE  (username = ? OR employee_id = ?)
          AND  account_status = 1
        LIMIT 1
        """,
        [body.username, body.username],
    )

    if not rows:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Invalid credentials.",
        )

    user = rows[0]

    # Verify password using the stored hash + salt
    if not verify_password(body.password, user["password_hash"], user["password_salt"]):
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Invalid credentials.",
        )

    # Build JWT payload — all fields the frontend and other endpoints need
    token_payload = {
        "user_id":          user["user_id"],
        "username":         user["username"],
        "full_name":        user["full_name"],
        "role":             user["role"],
        "department":       user["department"],
        "dashboard_access": user["dashboard_access"],
        "can_edit_data":    bool(user["can_edit_data"]),
        "can_flag":         bool(user["can_flag"]),
        "can_resolve_flag": bool(user["can_resolve_flag"]),
        "account_status":   bool(user["account_status"]),
    }

    access_token = create_access_token(token_payload)

    return LoginResponse(
        access_token     = access_token,
        user_id          = user["user_id"],
        full_name        = user["full_name"],
        username         = user["username"],
        role             = user["role"],
        department       = user["department"],
        dashboard_access = user["dashboard_access"],
        can_edit_data    = bool(user["can_edit_data"]),
        can_flag         = bool(user["can_flag"]),
        can_resolve_flag = bool(user["can_resolve_flag"]),
    )


# -----------------------------------------------------------------------------
# GET /api/auth/me
# Returns the current user's profile decoded from their JWT.
# Frontend calls this on app load to restore session.
# -----------------------------------------------------------------------------
@router.get("/me", summary="Get current user profile")
def get_me(user: dict = Depends(get_current_user)):
    """Return the authenticated user's profile from their JWT."""
    return {
        "user_id":          user.get("user_id"),
        "username":         user.get("username"),
        "full_name":        user.get("full_name"),
        "role":             user.get("role"),
        "department":       user.get("department"),
        "dashboard_access": user.get("dashboard_access"),
        "can_edit_data":    user.get("can_edit_data"),
        "can_flag":         user.get("can_flag"),
        "can_resolve_flag": user.get("can_resolve_flag"),
    }