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
import logging

router = APIRouter(prefix="/api/edit-data", tags=["Edit Data"])

logger = logging.getLogger(__name__)

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


def _trigger_databricks_job() -> tuple[bool, str | None]:
    """
    Trigger the Databricks data-refresh job.

    The Google Sheets write has already completed before this function runs,
    so a Databricks error must not turn the entire commit request into a 500.
    """

    raw_job_id = str(settings.databricks_job_id or "").strip()

    if not raw_job_id:
        return False, "DATABRICKS_JOB_ID is not configured."

    try:
        job_id = int(raw_job_id)

        databricks_host = str(settings.databricks_host or "").strip()
        databricks_host = databricks_host.removeprefix("https://")
        databricks_host = databricks_host.removeprefix("http://")
        databricks_host = databricks_host.rstrip("/")

        if not databricks_host:
            return False, "DATABRICKS_HOST is not configured."

        databricks_token = str(settings.databricks_token or "").strip()

        if not databricks_token:
            return False, "DATABRICKS_TOKEN is not configured."

        url = f"https://{databricks_host}/api/2.1/jobs/run-now"

        headers = {
            "Authorization": f"Bearer {databricks_token}",
            "Content-Type": "application/json",
        }

        payload = {
            "job_id": job_id,
        }

        response = httpx.post(
            url,
            json=payload,
            headers=headers,
            timeout=20.0,
            follow_redirects=True,
        )

        if response.status_code not in {200, 201, 202}:
            error_message = (
                f"Databricks returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

            logger.error(error_message)
            return False, error_message

        return True, None

    except ValueError:
        error_message = (
            "DATABRICKS_JOB_ID must contain only the numeric Databricks "
            f"job ID. Received: {raw_job_id!r}"
        )

        logger.exception(error_message)
        return False, error_message

    except Exception as error:
        error_message = f"Databricks job trigger failed: {error}"

        logger.exception(error_message)
        return False, error_message

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
    job_triggered, job_error = _trigger_databricks_job()

    if job_triggered:
        refresh_message = "Databricks pipeline refresh triggered."
    else:
        refresh_message = (
            "Google Sheets was updated, but the Databricks pipeline "
            f"was not triggered. Reason: {job_error}"
        )

    return SheetWriteResponse(
        success=True,
        message=(
            f"Successfully wrote {rows_to_write} rows to "
            f"{body.sheet_name.upper()} ({config['tab_name']}). "
            f"{refresh_message}"
        ),
        sheet_name=body.sheet_name,
        rows_written=rows_to_write,
        job_triggered=job_triggered,
    )