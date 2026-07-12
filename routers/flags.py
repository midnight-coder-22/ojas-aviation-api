# =============================================================================
# routers/flags.py — Flag Management Routes
#
# GET  /api/flags                — list all active flags
# GET  /api/flags/{department}   — list active flags for one department
# POST /api/flags/raise          — raise flags on one or more WO IDs
# POST /api/flags/resolve        — resolve flags on one or more WO IDs
# =============================================================================

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from database import fetch_all
from dependencies import require_permission, get_current_user
from models import FlagCreateRequest, FlagResolveRequest, FlagRecord
from config import settings

router = APIRouter(prefix="/api/flags", tags=["Flags"])

SCHEMA = settings.databricks_schema


# -----------------------------------------------------------------------------
# GET /api/flags
# Returns all currently active flags (flag_status = 1).
# Accessible to all authenticated users (read-only view).
# -----------------------------------------------------------------------------
@router.get(
    "",
    response_model=list[FlagRecord],
    summary="List all active flags"
)
def list_all_flags(user: dict = Depends(get_current_user)):
    """Return all active (unresolved) flags across all departments."""
    rows = fetch_all(
        f"""
        SELECT sr_no, wo_id, item_no, department, flag_status,
               raised_date, resolved_date, raised_by, resolved_by
        FROM   {SCHEMA}.flags
        WHERE  flag_status = 1
        ORDER BY raised_date DESC
        """
    )
    return [FlagRecord(**r) for r in rows]


# -----------------------------------------------------------------------------
# GET /api/flags/{department}
# Returns active flags for a specific department.
# -----------------------------------------------------------------------------
@router.get(
    "/{department}",
    response_model=list[FlagRecord],
    summary="List active flags for a department"
)
def list_flags_by_department(department: str, user: dict = Depends(get_current_user)):
    """Return all active flags for the specified department."""
    dept = department.upper().replace("-", " ").replace("_", " ").strip()
    rows = fetch_all(
        f"""
        SELECT sr_no, wo_id, item_no, department, flag_status,
               raised_date, resolved_date, raised_by, resolved_by
        FROM   {SCHEMA}.flags
        WHERE  flag_status = 1
          AND  UPPER(department) = ?
        ORDER BY raised_date DESC
        """,
        [dept],
    )
    return [FlagRecord(**r) for r in rows]


# -----------------------------------------------------------------------------
# POST /api/flags/raise
# Raises flags on one or more WO IDs in a single action.
# Permission: can_flag must be 1 (Admin and Executive only by default).
# -----------------------------------------------------------------------------
@router.post(
    "/raise",
    summary="Raise flags on one or more work orders"
)
def raise_flags(
    body: FlagCreateRequest,
    user: dict = Depends(require_permission("can_flag")),
):
    """
    Insert flag rows for each WO ID in the request.
    Each WO gets its own row in the flags table with flag_status = 1.
    If a WO already has an active flag it is skipped (idempotent).
    """
    if not body.wo_ids:
        raise HTTPException(status_code=400, detail="wo_ids cannot be empty.")

    now       = datetime.now(timezone.utc).isoformat()
    raised_by = user.get("username", "unknown")
    inserted  = []
    skipped   = []

    for wo_id in body.wo_ids:
        # Check if this WO already has an active flag
        existing = fetch_all(
            f"SELECT sr_no FROM {SCHEMA}.flags WHERE wo_id = ? AND flag_status = 1 LIMIT 1",
            [wo_id],
        )
        if existing:
            skipped.append(wo_id)
            continue

        # Insert new flag row
        fetch_all(
            f"""
            INSERT INTO {SCHEMA}.flags
                (wo_id, item_no, department, flag_status, raised_date, raised_by)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            [wo_id, body.item_no or "", body.department, now, raised_by],
        )
        inserted.append(wo_id)

    return {
        "success":  True,
        "inserted": inserted,
        "skipped":  skipped,
        "message":  f"{len(inserted)} flag(s) raised. {len(skipped)} already flagged (skipped).",
    }


# -----------------------------------------------------------------------------
# POST /api/flags/resolve
# Resolves active flags on one or more WO IDs.
# Permission: can_resolve_flag must be 1 (Executive only by default).
# -----------------------------------------------------------------------------
@router.post(
    "/resolve",
    summary="Resolve flags on one or more work orders"
)
def resolve_flags(
    body: FlagResolveRequest,
    user: dict = Depends(require_permission("can_resolve_flag")),
):
    """
    Sets flag_status = 0 and records resolved_date + resolved_by
    for all active flag rows matching the given WO IDs.
    """
    if not body.wo_ids:
        raise HTTPException(status_code=400, detail="wo_ids cannot be empty.")

    now         = datetime.now(timezone.utc).isoformat()
    resolved_by = user.get("username", "unknown")
    resolved    = []
    not_found   = []

    for wo_id in body.wo_ids:
        existing = fetch_all(
            f"SELECT sr_no FROM {SCHEMA}.flags WHERE wo_id = ? AND flag_status = 1 LIMIT 1",
            [wo_id],
        )
        if not existing:
            not_found.append(wo_id)
            continue

        fetch_all(
            f"""
            UPDATE {SCHEMA}.flags
            SET    flag_status   = 0,
                   resolved_date = ?,
                   resolved_by   = ?
            WHERE  wo_id         = ?
              AND  flag_status   = 1
            """,
            [now, resolved_by, wo_id],
        )
        resolved.append(wo_id)

    return {
        "success":   True,
        "resolved":  resolved,
        "not_found": not_found,
        "message":   f"{len(resolved)} flag(s) resolved. {len(not_found)} had no active flag.",
    }