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
    Insert flag rows for the WO IDs in the request, in a single batch.
    Each new WO gets its own row in the flags table with flag_status = 1.
    A WO that already has an active flag is skipped (idempotent).
    """
    wo_ids = list(dict.fromkeys(body.wo_ids))  # de-duplicate, keep order

    if not wo_ids:
        raise HTTPException(status_code=400, detail="wo_ids cannot be empty.")

    now       = datetime.now(timezone.utc).isoformat()
    raised_by = user.get("username", "unknown")

    # One query to find which of the requested WOs already have an active
    # flag, instead of one query per WO.
    placeholders = ", ".join(["?"] * len(wo_ids))
    existing_rows = fetch_all(
        f"""
        SELECT wo_id
        FROM   {SCHEMA}.flags
        WHERE  flag_status = 1
          AND  wo_id IN ({placeholders})
        """,
        wo_ids,
    )
    already_flagged = {row["wo_id"] for row in existing_rows}

    skipped  = [wo_id for wo_id in wo_ids if wo_id in already_flagged]
    inserted = [wo_id for wo_id in wo_ids if wo_id not in already_flagged]

    # One multi-row INSERT for every new flag, instead of one INSERT per WO.
    if inserted:
        values_placeholders = ", ".join(["(?, ?, ?, 1, ?, ?)"] * len(inserted))
        insert_params = []

        for wo_id in inserted:
            insert_params.extend(
                [wo_id, body.item_no or "", body.department, now, raised_by]
            )

        fetch_all(
            f"""
            INSERT INTO {SCHEMA}.flags
                (wo_id, item_no, department, flag_status, raised_date, raised_by)
            VALUES {values_placeholders}
            """,
            insert_params,
        )

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
    Sets flag_status = 0 and records resolved_date + resolved_by for every
    active flag row matching the given WO IDs, in a single batch.
    """
    wo_ids = list(dict.fromkeys(body.wo_ids))  # de-duplicate, keep order

    if not wo_ids:
        raise HTTPException(status_code=400, detail="wo_ids cannot be empty.")

    now         = datetime.now(timezone.utc).isoformat()
    resolved_by = user.get("username", "unknown")

    # One query to find which of the requested WOs currently have an
    # active flag, instead of one query per WO.
    placeholders = ", ".join(["?"] * len(wo_ids))
    existing_rows = fetch_all(
        f"""
        SELECT wo_id
        FROM   {SCHEMA}.flags
        WHERE  flag_status = 1
          AND  wo_id IN ({placeholders})
        """,
        wo_ids,
    )
    currently_flagged = {row["wo_id"] for row in existing_rows}

    resolved  = [wo_id for wo_id in wo_ids if wo_id in currently_flagged]
    not_found = [wo_id for wo_id in wo_ids if wo_id not in currently_flagged]

    # One UPDATE covering every resolved WO, instead of one UPDATE per WO.
    if resolved:
        update_placeholders = ", ".join(["?"] * len(resolved))
        fetch_all(
            f"""
            UPDATE {SCHEMA}.flags
            SET    flag_status   = 0,
                   resolved_date = ?,
                   resolved_by   = ?
            WHERE  flag_status   = 1
              AND  wo_id IN ({update_placeholders})
            """,
            [now, resolved_by, *resolved],
        )

    return {
        "success":   True,
        "resolved":  resolved,
        "not_found": not_found,
        "message":   f"{len(resolved)} flag(s) resolved. {len(not_found)} had no active flag.",
    }