# =============================================================================
# routers/edit_data.py — Edit Data Routes
#
# GET  /api/edit-data/wos    — read WorkOrderSummaryReport sheet as-is
# GET  /api/edit-data/ows    — read OperationWiseWIPStatas sheet as-is
# POST /api/edit-data/commit — write updated rows back to Google Sheets
#                              then trigger Databricks pipeline job
#
# Permission: can_edit_data = 1 (Admin and Executive by default)
# =============================================================================

import json
import httpx
from fastapi import APIRouter, HTTPException, Depends
from google.oauth2 import service_account
from googleapiclient.discovery import build

from models import SheetDataResponse, SheetWriteRequest, SheetWriteResponse
from dependencies import require_permission
from config import settings

router = APIRouter(prefix="/api/edit-data", tags=["Edit Data"])

# Google Sheets API scope — read + write
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Map API sheet keys to Google spreadsheet documents and worksheet tabs.
#
# spreadsheet_id = the Google Sheets document ID
# tab_name       = the worksheet/tab inside that document
# columns        = the columns used by the edit-data grid
SHEET_CONFIG = {
    "wos": {
        "spreadsheet_id": settings.wos_spreadsheet_id,
        "tab_name": "Sheet1",
        "columns": "A:T",
    },
    "ows": {
        "spreadsheet_id": settings.ows_spreadsheet_id,
        "tab_name": "Sheet1",
        "columns": "A:S",
    },
}


def _get_sheet_range(config: dict) -> str:
    """
    Build a valid Google Sheets A1 range.

    Quoting the tab name also supports spaces or special characters if the
    worksheet is renamed later.
    """
    tab_name = str(config["tab_name"]).replace("'", "''")
    return f"'{tab_name}'!{config['columns']}"


def _get_sheets_service():
    """
    Build an authenticated Google Sheets API client using the
    service account JSON from the environment variable.
    Raises a clear error if the service account is not configured.
    """
    if not settings.google_service_account_json:
        raise HTTPException(
            status_code=503,
            detail="Google Sheets integration is not configured on this server.",
        )
    try:
        sa_info      = json.loads(settings.google_service_account_json)
        creds        = service_account.Credentials.from_service_account_info(
                           sa_info, scopes=SCOPES)
        service      = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return service
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google auth error: {e}")


def _trigger_databricks_job() -> bool:
    """
    Trigger the Databricks pipeline job via the Jobs Runs Now API.
    Returns True if the trigger succeeded, False if it failed (non-fatal —
    the data write already completed so we don't want to roll it back).
    """
    if not settings.databricks_job_id:
        return False        # Job ID not configured — skip silently

    url     = f"https://{settings.databricks_host}/api/2.1/jobs/run-now"
    headers = {
        "Authorization": f"Bearer {settings.databricks_token}",
        "Content-Type":  "application/json",
    }
    payload = {"job_id": int(settings.databricks_job_id)}

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False        # Network issue — fail silently, data write succeeded


# -----------------------------------------------------------------------------
# GET /api/edit-data/wos
# Returns the raw WorkOrderSummaryReport sheet for the Edit Data grid.
# -----------------------------------------------------------------------------
@router.get(
    "/wos",
    response_model=SheetDataResponse,
    summary="Read WorkOrderSummaryReport sheet"
)
def get_wos_sheet(user: dict = Depends(require_permission("can_edit_data"))):
    """
    Fetch the full WorkOrderSummaryReport Google Sheet as-is.
    Row 0 is treated as the header row.
    """
    config  = SHEET_CONFIG["wos"]
    service = _get_sheets_service()

    try:
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=config["spreadsheet_id"], range=_get_sheet_range(config))
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google Sheets read failed: {e}")

    values = result.get("values", [])
    if not values:
        return SheetDataResponse(
            sheet_name="wos", headers=[], rows=[], total_rows=0
        )

    headers   = values[0]
    data_rows = values[1:]
    return SheetDataResponse(
        sheet_name = "wos",
        headers    = headers,
        rows       = data_rows,
        total_rows = len(data_rows),
    )


# -----------------------------------------------------------------------------
# GET /api/edit-data/ows
# Returns the raw OperationWiseWIPStatas sheet for the Edit Data grid.
# -----------------------------------------------------------------------------
@router.get(
    "/ows",
    response_model=SheetDataResponse,
    summary="Read OperationWiseWIPStatas sheet"
)
def get_ows_sheet(user: dict = Depends(require_permission("can_edit_data"))):
    """
    Fetch the full OperationWiseWIPStatas Google Sheet as-is.
    Row 0 is treated as the header row.
    """
    config  = SHEET_CONFIG["ows"]
    service = _get_sheets_service()

    try:
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=config["spreadsheet_id"], range=_get_sheet_range(config))
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google Sheets read failed: {e}")

    values = result.get("values", [])
    if not values:
        return SheetDataResponse(
            sheet_name="ows", headers=[], rows=[], total_rows=0
        )

    headers   = values[0]
    data_rows = values[1:]
    return SheetDataResponse(
        sheet_name = "ows",
        headers    = headers,
        rows       = data_rows,
        total_rows = len(data_rows),
    )


# -----------------------------------------------------------------------------
# POST /api/edit-data/commit
# Writes the edited dataset back to the appropriate Google Sheet, then
# triggers the Databricks pipeline job to re-process and refresh all tables.
# -----------------------------------------------------------------------------
@router.post(
    "/commit",
    response_model=SheetWriteResponse,
    summary="Write changes back to Google Sheets and refresh pipeline"
)
def commit_changes(
    body: SheetWriteRequest,
    user: dict = Depends(require_permission("can_edit_data")),
):
    """
    Replace the content of the specified Google Sheet with the submitted data,
    then trigger the Databricks pipeline job so Delta tables are refreshed.

    body.sheet_name must be "wos" or "ows".
    body.headers + body.rows form the complete new dataset (including header row).
    """
    if body.sheet_name not in SHEET_CONFIG:
        raise HTTPException(
            status_code=400,
            detail=f"sheet_name must be 'wos' or 'ows'. Got: '{body.sheet_name}'",
        )

    config  = SHEET_CONFIG[body.sheet_name]
    service = _get_sheets_service()

    # Combine header + data rows into the full write payload
    all_rows     = [body.headers] + body.rows
    rows_to_write = len(body.rows)

    try:
        # Clear the existing sheet content first
        service.spreadsheets().values().clear(
            spreadsheetId = config["spreadsheet_id"],
            range         = _get_sheet_range(config),
        ).execute()

        # Write the new data
        service.spreadsheets().values().update(
            spreadsheetId     = config["spreadsheet_id"],
            range             = _get_sheet_range(config),
            valueInputOption  = "USER_ENTERED",
            body              = {"values": all_rows},
        ).execute()

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google Sheets write failed: {e}")

    # Trigger the Databricks pipeline job (best-effort, non-fatal)
    job_triggered = _trigger_databricks_job()

    return SheetWriteResponse(
        success       = True,
        message=(
        f"Successfully wrote {rows_to_write} rows to "
        f"{body.sheet_name.upper()} ({config['tab_name']}). "
        + (
            "Pipeline refresh triggered."
            if job_triggered
            else "Pipeline refresh not triggered (job ID not configured)."
        )
    ),
        rows_written  = rows_to_write,
        job_triggered = job_triggered,
    )