# =============================================================================
# routers/dashboard.py - Dashboard API Routes
# =============================================================================

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

from config import settings
from database import fetch_all
from dependencies import get_current_user
from models import (
    DepartmentResponse,
    DepartmentSummary,
    IncomingFlowResponse,
    IncomingFlowRow,
    WorkOrderKPI,
)

router = APIRouter(prefix="/api", tags=["Dashboard"])

DEPARTMENTS = [
    "CNC",
    "VMC",
    "CONVENTIONAL",
    "SHEET METAL",
    "PRODUCTION",
    "EDM",
]

DEPT_TABLE_MAP = {
    "CNC": f"{settings.databricks_schema}.dept_cnc",
    "VMC": f"{settings.databricks_schema}.dept_vmc",
    "CONVENTIONAL": f"{settings.databricks_schema}.dept_conventional",
    "SHEET METAL": f"{settings.databricks_schema}.dept_sheet_metal",
    "PRODUCTION": f"{settings.databricks_schema}.dept_production",
    "EDM": f"{settings.databricks_schema}.dept_edm",
}

# Dashboard dates and "today" should follow the business timezone.
BUSINESS_TIMEZONE = ZoneInfo("Asia/Kolkata")

CANONICAL_STATUS_ORDER = (
    "New",
    "Ongoing",
    "Overdue",
    "Completed",
)

STATUS_ALIASES = {
    "new": "New",
    "notstarted": "New",

    "ongoing": "Ongoing",
    "inprocess": "Ongoing",
    "inprogress": "Ongoing",

    "overdue": "Overdue",

    "completed": "Completed",
    "complete": "Completed",
    "done": "Completed",
}


def _to_calendar_date(value: object) -> date | None:
    """
    Convert a Databricks date, timestamp or ISO-style string into a date.

    Blank or invalid values return None.
    """
    if value is None:
        return None

    # datetime must be checked before date because datetime inherits date.
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(
                BUSINESS_TIMEZONE
            ).date()

        return value.date()

    if isinstance(value, date):
        return value

    normalized = str(value).strip()

    if not normalized:
        return None

    try:
        # Supports YYYY-MM-DD as well as timestamps beginning with that date.
        return date.fromisoformat(
            normalized[:10]
        )
    except ValueError:
        return None


def _normalize_dashboard_status(
    value: object,
) -> str:
    """
    Convert source status variants into the four dashboard statuses.
    """
    status_key = re.sub(
        r"[^a-z0-9]+",
        "",
        str(value or "").strip().lower(),
    )

    # Unsupported or blank source values default to New.
    return STATUS_ALIASES.get(
        status_key,
        "New",
    )


def _derive_dashboard_status(
    row: dict,
    today: date,
) -> str:
    """
    Derive the live status of one work order.

    Rules:
    - Completed remains Completed.
    - An existing Overdue value remains Overdue.
    - Any active WO with either target date earlier than today is Overdue.
    - Otherwise, New/InProcess values become New/Ongoing.
    """
    base_status = _normalize_dashboard_status(
        row.get("status")
    )

    # Completed takes precedence over historical deadlines.
    if base_status == "Completed":
        return "Completed"

    if base_status == "Overdue":
        return "Overdue"

    wo_target_date = _to_calendar_date(
        row.get("wo_target_date")
    )

    dept_target_date = _to_calendar_date(
        row.get("dept_target_date")
    )

    has_expired_deadline = any(
        target_date is not None
        and target_date < today
        for target_date in (
            wo_target_date,
            dept_target_date,
        )
    )

    if has_expired_deadline:
        return "Overdue"

    return base_status


def _prepare_dashboard_rows(
    rows: list[dict],
) -> list[dict]:
    """
    Return copied rows containing their derived dashboard statuses.
    """
    if not rows:
        return []

    today = datetime.now(
        BUSINESS_TIMEZONE
    ).date()

    prepared_rows: list[dict] = []

    for row in rows:
        prepared_row = dict(row)

        prepared_row["status"] = (
            _derive_dashboard_status(
                prepared_row,
                today,
            )
        )

        prepared_rows.append(
            prepared_row
        )

    return prepared_rows

BUSINESS_TIMEZONE = ZoneInfo("Asia/Kolkata")

STATUS_ALIASES = {
    "new": "New",
    "notstarted": "New",

    "inprocess": "Ongoing",
    "inprogress": "Ongoing",
    "ongoing": "Ongoing",

    "completed": "Completed",
    "complete": "Completed",
    "done": "Completed",

    "overdue": "Overdue",
}


def _to_calendar_date(value: object) -> date | None:
    """
    Convert Databricks date/timestamp/string values into a calendar date.
    Invalid or blank values return None.
    """
    if value is None:
        return None

    # datetime is also a subclass of date, so test it first.
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    normalized = str(value).strip()

    if not normalized:
        return None

    try:
        # Supports both YYYY-MM-DD and timestamps beginning with YYYY-MM-DD.
        return date.fromisoformat(normalized[:10])
    except ValueError:
        return None


def _normalize_dashboard_status(value: object) -> str:
    """
    Map source/display variants into the four canonical dashboard statuses.
    """
    status_key = re.sub(
        r"[^a-z0-9]+",
        "",
        str(value or "").strip().lower(),
    )

    # Blank or unsupported source values fall back to New.
    return STATUS_ALIASES.get(status_key, "New")


def _derive_dashboard_status(
    row: dict,
    today: date,
) -> str:
    """
    Derive the live dashboard status for one work order.

    Completed takes precedence over the overdue calculation.
    Either target date being earlier than today makes an active WO overdue.
    """
    base_status = _normalize_dashboard_status(
        row.get("status")
    )

    if base_status == "Completed":
        return "Completed"

    if base_status == "Overdue":
        return "Overdue"

    deadlines = (
        _to_calendar_date(
            row.get("wo_target_date")
        ),
        _to_calendar_date(
            row.get("dept_target_date")
        ),
    )

    is_overdue = any(
        deadline is not None and deadline < today
        for deadline in deadlines
    )

    if is_overdue:
        return "Overdue"

    return base_status


def _prepare_dashboard_rows(
    rows: list[dict],
) -> list[dict]:
    """
    Return copied rows containing the live canonical dashboard status.
    """
    if not rows:
        return []

    today = datetime.now(
        BUSINESS_TIMEZONE
    ).date()

    prepared_rows: list[dict] = []

    for row in rows:
        prepared_row = dict(row)
        prepared_row["status"] = (
            _derive_dashboard_status(
                prepared_row,
                today,
            )
        )
        prepared_rows.append(prepared_row)

    return prepared_rows


def _resolve_department(dept_param: str) -> str:
    """Convert a URL department value to its canonical department name."""
    normalized = (
        dept_param.upper()
        .replace("-", " ")
        .replace("_", " ")
        .strip()
    )

    if normalized not in DEPARTMENTS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Department '{dept_param}' not found. "
                f"Valid departments: {', '.join(DEPARTMENTS)}"
            ),
        )

    return normalized


def _build_department_summary(
    department: str,
    rows: list[dict],
) -> DepartmentSummary:
    """Build the summary response for one department."""

    # Apply the same status rules used by the detailed endpoint.
    rows = _prepare_dashboard_rows(rows)
    if not rows:
        return DepartmentSummary(
            department=department,
            total_wos=0,
            qc_alert_count=0,
            mi_alert_count=0,
            flagged_count=0,
            status_breakdown={status: 0 for status in CANONICAL_STATUS_ORDER},
            priority_breakdown={"Low": 0, "Medium": 0, "High": 0,},
            last_refreshed=None,
        )

    qc_alert_count = sum(
        1 for row in rows if bool(row.get("qc_alert"))
    )
    mi_alert_count = sum(
        1 for row in rows if bool(row.get("mi_alert"))
    )
    flagged_count = sum(
        1 for row in rows if bool(row.get("has_active_flag"))
    )

    status_breakdown: dict[str, int] = {
    status: 0
    for status in CANONICAL_STATUS_ORDER
    }

    priority_breakdown: dict[str, int] = {
        "Low": 0,
        "Medium": 0,
        "High": 0,
    }

    for row in rows:
        status = str(row.get("status") or "Unknown").strip() or "Unknown"
        priority = str(row.get("priority") or "Low").strip() or "Low"

        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        priority_breakdown[priority] = (
            priority_breakdown.get(priority, 0) + 1
        )

    return DepartmentSummary(
        department=department,
        total_wos=len(rows),
        qc_alert_count=qc_alert_count,
        mi_alert_count=mi_alert_count,
        flagged_count=flagged_count,
        status_breakdown=status_breakdown,
        priority_breakdown=priority_breakdown,
        last_refreshed=rows[0].get("last_refreshed"),
    )


def _build_incoming_flow_query(
    target_department: str,
) -> tuple[str, list[str]]:
    """
    Search all other department tables for work orders whose next_dept is the
    selected target department, then group distinct WOs by source and priority.
    """
    union_parts: list[str] = []
    params: list[str] = []

    for source_department in DEPARTMENTS:
        if source_department == target_department:
            continue

        table_name = DEPT_TABLE_MAP[source_department]

        union_parts.append(
            f"""
            SELECT
                '{source_department}' AS source_department,
                CAST(wo_id AS STRING) AS wo_id,
                CASE
                    WHEN LOWER(TRIM(COALESCE(priority, 'Low'))) = 'high'
                        THEN 3
                    WHEN LOWER(TRIM(COALESCE(priority, 'Low'))) = 'medium'
                        THEN 2
                    ELSE 1
                END AS priority_rank
            FROM {table_name}
            WHERE UPPER(TRIM(COALESCE(next_dept, ''))) = ?
              AND wo_id IS NOT NULL
              AND TRIM(CAST(wo_id AS STRING)) <> ''
            """
        )
        params.append(target_department)

    union_sql = "\nUNION ALL\n".join(union_parts)

    query = f"""
        WITH incoming_raw AS (
            {union_sql}
        ),
        incoming_deduplicated AS (
            SELECT
                source_department,
                wo_id,
                MAX(priority_rank) AS priority_rank
            FROM incoming_raw
            GROUP BY source_department, wo_id
        )
        SELECT
            source_department,
            SUM(CASE WHEN priority_rank = 1 THEN 1 ELSE 0 END) AS low,
            SUM(CASE WHEN priority_rank = 2 THEN 1 ELSE 0 END) AS medium,
            SUM(CASE WHEN priority_rank = 3 THEN 1 ELSE 0 END) AS high,
            COUNT(*) AS total
        FROM incoming_deduplicated
        GROUP BY source_department
        ORDER BY total DESC, source_department
    """

    return query, params


# =============================================================================
# STATIC ROUTES
# =============================================================================

@router.get(
    "/departments",
    response_model=list[str],
    summary="List all departments",
)
def list_departments(
    user: dict = Depends(get_current_user),
):
    return DEPARTMENTS


@router.get(
    "/dashboard/all/summary",
    response_model=list[DepartmentSummary],
    summary="Get summary for all departments at once",
)
def get_all_departments_summary(
    user: dict = Depends(get_current_user),
):
    summaries: list[DepartmentSummary] = []

    for department in DEPARTMENTS:
        table_name = DEPT_TABLE_MAP[department]
        rows = fetch_all(f"SELECT * FROM {table_name}")
        summaries.append(
            _build_department_summary(department, rows)
        )

    return summaries


# =============================================================================
# DYNAMIC ROUTES
# Keep the generic /dashboard/{department} route last.
# =============================================================================

@router.get(
    "/dashboard/{department}/summary",
    response_model=DepartmentSummary,
    summary="Get aggregate summary for one department",
)
def get_department_summary(
    department: str,
    user: dict = Depends(get_current_user),
):
    resolved_department = _resolve_department(department)
    table_name = DEPT_TABLE_MAP[resolved_department]
    rows = fetch_all(f"SELECT * FROM {table_name}")

    return _build_department_summary(
        resolved_department,
        rows,
    )


@router.get(
    "/dashboard/{department}/incoming-flow",
    response_model=IncomingFlowResponse,
    summary="Get incoming work orders by source department and priority",
)
def get_incoming_flow(
    department: str,
    user: dict = Depends(get_current_user),
):
    target_department = _resolve_department(department)
    query, params = _build_incoming_flow_query(target_department)
    rows = fetch_all(query, params)

    rows_by_source = {
        str(row.get("source_department") or "").strip().upper(): row
        for row in rows
    }

    response_rows: list[IncomingFlowRow] = []

    for source_department in DEPARTMENTS:
        if source_department == target_department:
            continue

        row = rows_by_source.get(source_department, {})

        low = int(row.get("low") or 0)
        medium = int(row.get("medium") or 0)
        high = int(row.get("high") or 0)
        total = int(row.get("total") or 0)

        response_rows.append(
            IncomingFlowRow(
                source_department=source_department,
                low=low,
                medium=medium,
                high=high,
                total=total,
            )
        )

    response_rows.sort(
        key=lambda item: (-item.total, item.source_department)
    )

    return IncomingFlowResponse(
        target_department=target_department,
        total_wos=sum(item.total for item in response_rows),
        data=response_rows,
    )


@router.get(
    "/dashboard/{department}",
    response_model=DepartmentResponse,
    summary="Get full KPI data for one department",
)
def get_department_dashboard(
    department: str,
    user: dict = Depends(get_current_user),
):
    resolved_department = _resolve_department(department)
    table_name = DEPT_TABLE_MAP[resolved_department]

    raw_rows = fetch_all(
    f"""
    SELECT *
    FROM {table_name}
    ORDER BY wo_ageing_days DESC NULLS LAST
    """
    )

    # The detailed endpoint must return the derived status because both the
    # table and the Status chart consume these rows.
    rows = _prepare_dashboard_rows(
    raw_rows
    )
    rows = _prepare_dashboard_rows(raw_rows)

    return DepartmentResponse(
        department=resolved_department,
        record_count=len(rows),
        data=[WorkOrderKPI(**row) for row in rows],
    )