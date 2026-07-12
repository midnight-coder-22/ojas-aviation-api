# =============================================================================
# routers/dashboard.py — Dashboard API Routes
#
# Endpoints:
#   GET /api/departments
#       Returns all valid department names.
#
#   GET /api/dashboard/all/summary
#       Returns aggregate KPI summaries for all departments.
#
#   GET /api/dashboard/{department}/summary
#       Returns aggregate KPI summary for one department.
#
#   GET /api/dashboard/{department}
#       Returns the full KPI dataset for one department.
# =============================================================================

from fastapi import APIRouter, HTTPException

from database import fetch_all
from models import WorkOrderKPI, DepartmentResponse, DepartmentSummary
from config import settings


# Create a router for all dashboard-related API endpoints.
# Every route defined here will start with /api.
router = APIRouter(prefix="/api", tags=["Dashboard"])


# -----------------------------------------------------------------------------
# Valid department names used by the API and frontend.
# -----------------------------------------------------------------------------
DEPARTMENTS = [
    "CNC",
    "VMC",
    "CONVENTIONAL",
    "SHEET METAL",
    "PRODUCTION",
    "EDM",
]


# -----------------------------------------------------------------------------
# Map each department to its corresponding Databricks Delta table.
# -----------------------------------------------------------------------------
DEPT_TABLE_MAP = {
    "CNC": f"{settings.databricks_schema}.dept_cnc",
    "VMC": f"{settings.databricks_schema}.dept_vmc",
    "CONVENTIONAL": f"{settings.databricks_schema}.dept_conventional",
    "SHEET METAL": f"{settings.databricks_schema}.dept_sheet_metal",
    "PRODUCTION": f"{settings.databricks_schema}.dept_production",
    "EDM": f"{settings.databricks_schema}.dept_edm",
}


def _resolve_department(dept_param: str) -> str:
    """
    Convert the department name from the URL into the canonical department name.

    Examples:
        cnc          -> CNC
        sheet-metal  -> SHEET METAL
        sheet_metal  -> SHEET METAL
        SHEET METAL  -> SHEET METAL
    """

    # Normalize the URL value so different department formats are accepted.
    normalized = dept_param.upper().replace("-", " ").replace("_", " ").strip()

    # Reject departments that are not part of the approved department list.
    if normalized not in DEPARTMENTS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Department '{dept_param}' not found. "
                f"Valid departments: {', '.join(DEPARTMENTS)}"
            ),
        )

    return normalized


def _build_department_summary(dept: str, rows: list[dict]) -> DepartmentSummary:
    """
    Build a DepartmentSummary response from raw database rows.

    This helper keeps the summary logic in one place so it can be reused by:
        - /dashboard/all/summary
        - /dashboard/{department}/summary
    """

    # Return an empty summary when the department table has no records.
    if not rows:
        return DepartmentSummary(
            department=dept,
            total_wos=0,
            qc_alert_count=0,
            mi_alert_count=0,
            status_breakdown={},
            priority_breakdown={},
            last_refreshed=None,
        )

    # Count work orders that have QC alerts.
    qc_count = sum(1 for row in rows if row.get("qc_alert"))

    # Count work orders that have MI alerts.
    mi_count = sum(1 for row in rows if row.get("mi_alert"))

    # Count work orders by status.
    # Missing or empty status values are grouped as "Unknown".
    status_breakdown: dict[str, int] = {}
    for row in rows:
        status = row.get("status") or "Unknown"
        status_breakdown[status] = status_breakdown.get(status, 0) + 1

    # Count work orders by priority.
    # Missing or empty priority values are grouped as "Low".
    priority_breakdown: dict[str, int] = {}
    for row in rows:
        priority = row.get("priority") or "Low"
        priority_breakdown[priority] = priority_breakdown.get(priority, 0) + 1

    # Assumes all rows in the table were refreshed at the same time.
    last_refreshed = rows[0].get("last_refreshed")

    return DepartmentSummary(
        department=dept,
        total_wos=len(rows),
        qc_alert_count=qc_count,
        mi_alert_count=mi_count,
        status_breakdown=status_breakdown,
        priority_breakdown=priority_breakdown,
        last_refreshed=last_refreshed,
    )


# -----------------------------------------------------------------------------
# GET /api/departments
#
# Returns all valid department names.
# -----------------------------------------------------------------------------
@router.get(
    "/departments",
    response_model=list[str],
    summary="List all departments",
)
def list_departments():
    """Return all valid department names."""
    return DEPARTMENTS


# -----------------------------------------------------------------------------
# GET /api/dashboard/all/summary
#
# IMPORTANT:
# This route must be declared BEFORE /dashboard/{department}/summary.
#
# Otherwise FastAPI may treat "all" as the department parameter and call:
#     /dashboard/{department}/summary
#
# That is what causes:
#     Department 'all' not found
# -----------------------------------------------------------------------------
@router.get(
    "/dashboard/all/summary",
    response_model=list[DepartmentSummary],
    summary="Get summary for all departments at once",
)
def get_all_departments_summary():
    """Return aggregate summary KPI data for every department."""

    summaries = []

    # Build one summary object per department.
    for dept in DEPARTMENTS:
        table = DEPT_TABLE_MAP[dept]

        # Fetch all rows for this department from Databricks.
        rows = fetch_all(f"SELECT * FROM {table}")

        # Convert raw rows into the API response model.
        summaries.append(_build_department_summary(dept, rows))

    return summaries


# -----------------------------------------------------------------------------
# GET /api/dashboard/{department}/summary
#
# Returns aggregate KPI data for one specific department.
#
# Example URLs:
#   /api/dashboard/cnc/summary
#   /api/dashboard/vmc/summary
#   /api/dashboard/sheet-metal/summary
# -----------------------------------------------------------------------------
@router.get(
    "/dashboard/{department}/summary",
    response_model=DepartmentSummary,
    summary="Get aggregate summary for a department",
)
def get_department_summary(department: str):
    """Return aggregate summary KPI data for the requested department."""

    # Convert the URL department value to the canonical department name.
    dept = _resolve_department(department)

    # Get the table name for this department.
    table = DEPT_TABLE_MAP[dept]

    # Fetch all department rows.
    rows = fetch_all(f"SELECT * FROM {table}")

    # Build and return the summary response.
    return _build_department_summary(dept, rows)


# -----------------------------------------------------------------------------
# GET /api/dashboard/{department}
#
# Returns the full KPI row-level dataset for one department.
#
# Example URLs:
#   /api/dashboard/cnc
#   /api/dashboard/vmc
#   /api/dashboard/sheet-metal
# -----------------------------------------------------------------------------
@router.get(
    "/dashboard/{department}",
    response_model=DepartmentResponse,
    summary="Get full KPI data for a department",
)
def get_department_dashboard(department: str):
    """Return all Work Order KPI records for the requested department."""

    # Convert the URL department value to the canonical department name.
    dept = _resolve_department(department)

    # Get the table name for this department.
    table = DEPT_TABLE_MAP[dept]

    # Fetch all KPI records for the department.
    # Oldest/highest ageing work orders appear first.
    rows = fetch_all(
        f"SELECT * FROM {table} ORDER BY wo_ageing_days DESC NULLS LAST"
    )

    # Convert raw database rows into validated response models.
    kpi_records = [WorkOrderKPI(**row) for row in rows]

    return DepartmentResponse(
        department=dept,
        record_count=len(rows),
        data=kpi_records,
    )


# =============================================================================
# routers/dashboard.py — Dashboard API Routes
#
# Route order is critical in FastAPI — static routes MUST be declared before
# dynamic routes, otherwise "all" gets matched as a department name.
#
# Correct order (enforced here):
#   GET /api/departments                ← static
#   GET /api/dashboard/all/summary      ← static, BEFORE /{department}/summary
#   GET /api/dashboard/{dept}/summary   ← dynamic
#   GET /api/dashboard/{dept}           ← dynamic
# =============================================================================

from fastapi import APIRouter, HTTPException, Depends
from database import fetch_all
from models import WorkOrderKPI, DepartmentResponse, DepartmentSummary
from dependencies import get_current_user
from config import settings

router = APIRouter(prefix="/api", tags=["Dashboard"])

DEPARTMENTS = ["CNC", "VMC", "CONVENTIONAL", "SHEET METAL", "PRODUCTION", "EDM"]

DEPT_TABLE_MAP = {
    "CNC":          f"{settings.databricks_schema}.dept_cnc",
    "VMC":          f"{settings.databricks_schema}.dept_vmc",
    "CONVENTIONAL": f"{settings.databricks_schema}.dept_conventional",
    "SHEET METAL":  f"{settings.databricks_schema}.dept_sheet_metal",
    "PRODUCTION":   f"{settings.databricks_schema}.dept_production",
    "EDM":          f"{settings.databricks_schema}.dept_edm",
}


def _resolve_department(dept_param: str) -> str:
    normalised = dept_param.upper().replace("-", " ").replace("_", " ").strip()
    if normalised not in DEPARTMENTS:
        raise HTTPException(
            status_code=404,
            detail=f"Department '{dept_param}' not found. "
                   f"Valid departments: {', '.join(DEPARTMENTS)}"
        )
    return normalised


# =============================================================================
# STATIC ROUTES — must come first
# =============================================================================

# GET /api/departments
@router.get("/departments", response_model=list[str], summary="List all departments")
def list_departments(user: dict = Depends(get_current_user)):
    return DEPARTMENTS


# GET /api/dashboard/all/summary  ← STATIC — must be before /{department}/summary
@router.get(
    "/dashboard/all/summary",
    response_model=list[DepartmentSummary],
    summary="Get summary for all departments at once"
)
def get_all_departments_summary(user: dict = Depends(get_current_user)):
    """
    Returns aggregate KPIs for every department in one call.
    Used by the Executive Dashboard overview.
    """
    summaries = []
    for dept in DEPARTMENTS:
        table = DEPT_TABLE_MAP[dept]
        rows  = fetch_all(f"SELECT * FROM {table}")

        if not rows:
            summaries.append(DepartmentSummary(
                department         = dept,
                total_wos          = 0,
                qc_alert_count     = 0,
                mi_alert_count     = 0,
                flagged_count      = 0,
                status_breakdown   = {},
                priority_breakdown = {},
                last_refreshed     = None,
            ))
            continue

        qc_count      = sum(1 for r in rows if r.get("qc_alert"))
        mi_count      = sum(1 for r in rows if r.get("mi_alert"))
        flagged_count = sum(1 for r in rows if r.get("has_active_flag"))

        status_breakdown: dict[str, int] = {}
        for r in rows:
            s = r.get("status") or "Unknown"
            status_breakdown[s] = status_breakdown.get(s, 0) + 1

        priority_breakdown: dict[str, int] = {}
        for r in rows:
            p = r.get("priority") or "Low"
            priority_breakdown[p] = priority_breakdown.get(p, 0) + 1

        summaries.append(DepartmentSummary(
            department         = dept,
            total_wos          = len(rows),
            qc_alert_count     = qc_count,
            mi_alert_count     = mi_count,
            flagged_count      = flagged_count,
            status_breakdown   = status_breakdown,
            priority_breakdown = priority_breakdown,
            last_refreshed     = rows[0].get("last_refreshed"),
        ))

    return summaries


# =============================================================================
# DYNAMIC ROUTES — must come after all static routes
# =============================================================================

# GET /api/dashboard/{department}/summary  ← DYNAMIC with /summary suffix
@router.get(
    "/dashboard/{department}/summary",
    response_model=DepartmentSummary,
    summary="Get aggregate summary for one department"
)
def get_department_summary(
    department: str,
    user: dict = Depends(get_current_user),
):
    dept  = _resolve_department(department)
    table = DEPT_TABLE_MAP[dept]
    rows  = fetch_all(f"SELECT * FROM {table}")

    if not rows:
        return DepartmentSummary(
            department         = dept,
            total_wos          = 0,
            qc_alert_count     = 0,
            mi_alert_count     = 0,
            flagged_count      = 0,
            status_breakdown   = {},
            priority_breakdown = {},
            last_refreshed     = None,
        )

    qc_count      = sum(1 for r in rows if r.get("qc_alert"))
    mi_count      = sum(1 for r in rows if r.get("mi_alert"))
    flagged_count = sum(1 for r in rows if r.get("has_active_flag"))

    status_breakdown: dict[str, int] = {}
    for r in rows:
        s = r.get("status") or "Unknown"
        status_breakdown[s] = status_breakdown.get(s, 0) + 1

    priority_breakdown: dict[str, int] = {}
    for r in rows:
        p = r.get("priority") or "Low"
        priority_breakdown[p] = priority_breakdown.get(p, 0) + 1

    return DepartmentSummary(
        department         = dept,
        total_wos          = len(rows),
        qc_alert_count     = qc_count,
        mi_alert_count     = mi_count,
        flagged_count      = flagged_count,
        status_breakdown   = status_breakdown,
        priority_breakdown = priority_breakdown,
        last_refreshed     = rows[0].get("last_refreshed"),
    )


# GET /api/dashboard/{department}  ← DYNAMIC — must be last
@router.get(
    "/dashboard/{department}",
    response_model=DepartmentResponse,
    summary="Get full KPI data for one department"
)
def get_department_dashboard(
    department: str,
    user: dict = Depends(get_current_user),
):
    dept  = _resolve_department(department)
    table = DEPT_TABLE_MAP[dept]
    rows  = fetch_all(
        f"SELECT * FROM {table} ORDER BY wo_ageing_days DESC NULLS LAST"
    )

    return DepartmentResponse(
        department   = dept,
        record_count = len(rows),
        data         = [WorkOrderKPI(**row) for row in rows],
    )
