# =============================================================================
# routers/dashboard.py - Dashboard API Routes
# =============================================================================

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
    IncomingWorkOrder,
    WorkOrderKPI,
)

router = APIRouter(prefix="/api", tags=["Dashboard"])


# =============================================================================
# CONFIGURATION
# =============================================================================

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

BUSINESS_TIMEZONE = ZoneInfo("Asia/Kolkata")

CANONICAL_STATUS_ORDER = (
    "New",
    "Ongoing",
    "Delayed",
    "Overdue",
    "Completed",
)

STATUS_ALIASES = {
    "new": "New",
    "notstarted": "New",
    "ongoing": "Ongoing",
    "inprocess": "Ongoing",
    "inprogress": "Ongoing",
    "delayed": "Delayed",
    "overdue": "Overdue",
    "completed": "Completed",
    "complete": "Completed",
    "done": "Completed",
}


# =============================================================================
# STATUS HELPERS
# =============================================================================

def _to_calendar_date(value: object) -> date | None:
    """Convert Databricks date/timestamp/string values into a calendar date."""
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(BUSINESS_TIMEZONE).date()
        return value.date()

    if isinstance(value, date):
        return value

    normalized = str(value).strip()
    if not normalized:
        return None

    try:
        return date.fromisoformat(normalized[:10])
    except ValueError:
        return None


def _normalize_dashboard_status(value: object) -> str:
    """Map source/display variants into canonical dashboard statuses."""
    status_key = re.sub(
        r"[^a-z0-9]+",
        "",
        str(value or "").strip().lower(),
    )
    return STATUS_ALIASES.get(status_key, "New")


def _derive_dashboard_status(row: dict, today: date) -> str:
    """
    Derive the live dashboard status.

    Rules:
    - If Dept Due Dt is before today, status is Delayed.
    - Otherwise Completed remains Completed.
    - Existing Delayed stays Delayed.
    - If Dept Due Dt has not passed but the source status is Overdue, keep Overdue.
    - If Dept Due Dt has not passed but WO Due Dt is before today, use Overdue.
    - Otherwise New/InProcess-style values resolve to New/Ongoing.
    """
    base_status = _normalize_dashboard_status(row.get("status"))

    dept_due_date = _to_calendar_date(row.get("dept_target_date"))
    if dept_due_date is not None and dept_due_date < today:
        return "Delayed"

    if base_status == "Completed":
        return "Completed"

    if base_status == "Delayed":
        return "Delayed"

    if base_status == "Overdue":
        return "Overdue"

    wo_due_date = _to_calendar_date(row.get("wo_target_date"))
    if wo_due_date is not None and wo_due_date < today:
        return "Overdue"

    return base_status


def _prepare_dashboard_rows(rows: list[dict]) -> list[dict]:
    """Return copied rows containing the live canonical dashboard status."""
    if not rows:
        return []

    today = datetime.now(BUSINESS_TIMEZONE).date()
    prepared_rows: list[dict] = []

    for row in rows:
        prepared_row = dict(row)
        prepared_row["status"] = _derive_dashboard_status(
            prepared_row,
            today,
        )
        prepared_rows.append(prepared_row)

    return prepared_rows


# =============================================================================
# DEPARTMENT HELPERS
# =============================================================================

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
    prepared_rows = _prepare_dashboard_rows(rows)

    if not prepared_rows:
        return DepartmentSummary(
            department=department,
            total_wos=0,
            qc_alert_count=0,
            mi_alert_count=0,
            flagged_count=0,
            status_breakdown={
                status: 0 for status in CANONICAL_STATUS_ORDER
            },
            priority_breakdown={
                "Low": 0,
                "Medium": 0,
                "High": 0,
            },
            last_refreshed=None,
        )

    qc_alert_count = sum(
        1 for row in prepared_rows if bool(row.get("qc_alert"))
    )
    mi_alert_count = sum(
        1 for row in prepared_rows if bool(row.get("mi_alert"))
    )
    flagged_count = sum(
        1 for row in prepared_rows if bool(row.get("has_active_flag"))
    )

    status_breakdown: dict[str, int] = {
        status: 0 for status in CANONICAL_STATUS_ORDER
    }
    priority_breakdown: dict[str, int] = {
        "Low": 0,
        "Medium": 0,
        "High": 0,
    }

    for row in prepared_rows:
        status = str(row.get("status") or "New").strip() or "New"
        priority = str(row.get("priority") or "Low").strip() or "Low"

        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        priority_breakdown[priority] = priority_breakdown.get(priority, 0) + 1

    return DepartmentSummary(
        department=department,
        total_wos=len(prepared_rows),
        qc_alert_count=qc_alert_count,
        mi_alert_count=mi_alert_count,
        flagged_count=flagged_count,
        status_breakdown=status_breakdown,
        priority_breakdown=priority_breakdown,
        last_refreshed=prepared_rows[0].get("last_refreshed"),
    )


# =============================================================================
# INCOMING FLOW
# =============================================================================

def _build_incoming_flow_query(
    target_department: str,
) -> tuple[str, list[str]]:
    """
    Return every distinct work order incoming to the selected department.

    The same query resolves the live active-flag state so the frontend can
    populate the incoming chart and popup from one API request.
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
                CAST(source_rows.wo_id AS STRING) AS wo_id,
                CAST(source_rows.item_no AS STRING) AS item_no,
                CAST(source_rows.wo_name AS STRING) AS wo_name,
                source_rows.dept_in_date,
                source_rows.wo_target_date,
                source_rows.dept_target_date,
                source_rows.wo_ageing_days,
                source_rows.dept_ageing_days,
                source_rows.planned_qty,
                CAST(source_rows.next_dept AS STRING) AS next_dept,
                CAST(source_rows.priority AS STRING) AS priority,
                CAST(source_rows.status AS STRING) AS status,
                source_rows.expected_steps,
                source_rows.done_steps,
                source_rows.qc_alert,
                source_rows.mi_alert,
                COALESCE(
                    active_flags.has_active_flag,
                    CAST(source_rows.has_active_flag AS BOOLEAN),
                    FALSE
                ) AS has_active_flag,
                source_rows.last_refreshed
            FROM {table_name} AS source_rows
            LEFT JOIN active_flags
              ON active_flags.wo_id = CAST(source_rows.wo_id AS STRING)
            WHERE UPPER(TRIM(COALESCE(source_rows.next_dept, ''))) = ?
              AND source_rows.wo_id IS NOT NULL
              AND TRIM(CAST(source_rows.wo_id AS STRING)) <> ''
            """
        )
        params.append(target_department)

    union_sql = "\nUNION ALL\n".join(union_parts)
    flags_table = f"{settings.databricks_schema}.flags"

    query = f"""
        WITH active_flags AS (
            SELECT
                CAST(wo_id AS STRING) AS wo_id,
                CASE
                    WHEN MAX(
                        CASE
                            WHEN CAST(flag_status AS INT) = 1 THEN 1
                            ELSE 0
                        END
                    ) = 1 THEN TRUE
                    ELSE FALSE
                END AS has_active_flag
            FROM {flags_table}
            WHERE wo_id IS NOT NULL
            GROUP BY CAST(wo_id AS STRING)
        ),
        incoming_raw AS (
            {union_sql}
        ),
        incoming_ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY source_department, wo_id
                    ORDER BY last_refreshed DESC NULLS LAST
                ) AS row_number
            FROM incoming_raw
        )
        SELECT
            source_department,
            wo_id,
            item_no,
            wo_name,
            dept_in_date,
            wo_target_date,
            dept_target_date,
            wo_ageing_days,
            dept_ageing_days,
            planned_qty,
            next_dept,
            priority,
            status,
            expected_steps,
            done_steps,
            qc_alert,
            mi_alert,
            has_active_flag,
            last_refreshed
        FROM incoming_ranked
        WHERE row_number = 1
        ORDER BY
            source_department,
            wo_ageing_days DESC NULLS LAST,
            wo_id
    """

    return query, params


# =============================================================================
# ROUTES
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
    summary="Get incoming work orders and source-department priority totals",
)
def get_incoming_flow(
    department: str,
    user: dict = Depends(get_current_user),
):
    target_department = _resolve_department(department)
    query, params = _build_incoming_flow_query(target_department)
    raw_rows = fetch_all(query, params)
    prepared_rows = _prepare_dashboard_rows(raw_rows)

    work_orders = [
        IncomingWorkOrder(**row)
        for row in prepared_rows
    ]

    counts_by_source = {
        source_department: {
            "low": 0,
            "medium": 0,
            "high": 0,
        }
        for source_department in DEPARTMENTS
        if source_department != target_department
    }

    for work_order in work_orders:
        source_counts = counts_by_source.setdefault(
            work_order.source_department,
            {"low": 0, "medium": 0, "high": 0},
        )
        priority_key = str(
            work_order.priority or "Low"
        ).strip().lower()

        if priority_key not in source_counts:
            priority_key = "low"

        source_counts[priority_key] += 1

    response_rows: list[IncomingFlowRow] = []

    for source_department, priority_counts in counts_by_source.items():
        low = int(priority_counts["low"])
        medium = int(priority_counts["medium"])
        high = int(priority_counts["high"])

        response_rows.append(
            IncomingFlowRow(
                source_department=source_department,
                low=low,
                medium=medium,
                high=high,
                total=low + medium + high,
            )
        )

    response_rows.sort(
        key=lambda item: (-item.total, item.source_department)
    )

    return IncomingFlowResponse(
        target_department=target_department,
        total_wos=len(work_orders),
        data=response_rows,
        work_orders=work_orders,
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
    rows = _prepare_dashboard_rows(raw_rows)

    return DepartmentResponse(
        department=resolved_department,
        record_count=len(rows),
        data=[WorkOrderKPI(**row) for row in rows],
    )
