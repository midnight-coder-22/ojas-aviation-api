# =============================================================================
# routers/qc.py - QC Dashboard Route
#
# GET /api/qc/dashboard - every open Inward / Inline / Final QC entry
#
# Lives under /api/qc rather than /api/dashboard/qc because
# /api/dashboard/{department} would capture that path first.
# =============================================================================

from datetime import date, datetime

from databricks.sql.exc import Error as DatabricksError
from fastapi import APIRouter, Depends, HTTPException

from config import settings
from database import fetch_all
from dependencies import get_current_user
from models import QcDashboardResponse, QcEntry
from routers.dashboard import (
    BUSINESS_TIMEZONE,
    _active_flags_cte,
    _as_utc,
    _days_since,
    _derive_dashboard_status,
    _to_calendar_date,
)

router = APIRouter(prefix="/api/qc", tags=["QC"])

QC_TABLE = f"{settings.databricks_schema}.qc_entries"


def _prepare_qc_row(row: dict, today: date) -> dict:
    """Add live QC ageing and the live WO status used by every dashboard."""
    prepared = dict(row)

    prepared["qc_ageing_days"] = _days_since(
        today, _to_calendar_date(prepared.get("qc_in_date")),
    )

    # Inward entries whose WO is not in the WIP report have no status to derive.
    if prepared.get("status"):
        prepared["status"] = _derive_dashboard_status(prepared, today)

    return prepared


@router.get(
    "/dashboard",
    response_model=QcDashboardResponse,
    summary="Get every open QC entry",
)
def get_qc_dashboard(
    user: dict = Depends(get_current_user),
):
    # Flags are per WO (see dashboard.py): a WO flagged from any department
    # shows as flagged here too, and Resolve Flag clears it everywhere.
    query = f"""
        WITH {_active_flags_cte()}
        SELECT
            qc.*,
            COALESCE(active_flags.has_active_flag, FALSE) AS has_active_flag
        FROM {QC_TABLE} AS qc
        LEFT JOIN active_flags
          ON active_flags.wo_id = qc.wo_id
        ORDER BY qc.qc_in_date ASC NULLS LAST, qc.entry_id
    """

    try:
        raw_rows = fetch_all(query)
    except DatabricksError as error:
        if "TABLE_OR_VIEW_NOT_FOUND" in str(error):
            raise HTTPException(
                status_code=503,
                detail=(
                    "QC data has not been generated yet. Update the QC sheets "
                    "in Edit Data and press Post Data."
                ),
            ) from error
        raise

    today = datetime.now(BUSINESS_TIMEZONE).date()
    rows = [_prepare_qc_row(row, today) for row in raw_rows]

    last_refreshed = max(
        (row["last_refreshed"] for row in rows if row.get("last_refreshed")),
        default=None,
    )

    return QcDashboardResponse(
        record_count=len(rows),
        last_refreshed=_as_utc(last_refreshed),
        data=[QcEntry(**row) for row in rows],
    )
