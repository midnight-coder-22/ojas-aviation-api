# =============================================================================
# routers/edit_data.py — Edit Data Routes
#
# GET  /api/edit-data/wos       — read WorkOrderSummaryReport sheet as-is
# GET  /api/edit-data/ows       — read OperationWiseWIPStatas sheet as-is
# POST /api/edit-data/commit    — write updated rows to Google Sheets ONLY
# POST /api/edit-data/post-data — trigger Databricks pipeline job ONLY
#
# Permission: can_edit_data = 1
# =============================================================================

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import settings
from dependencies import require_permission
from models import (
    PostDataResponse,
    SheetDataResponse,
    SheetWriteRequest,
    SheetWriteResponse,
)


router = APIRouter(
    prefix="/api/edit-data",
    tags=["Edit Data"],
)

logger = logging.getLogger(__name__)


# Google Sheets API scope — read + write
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets"
]


# -----------------------------------------------------------------------------
# Google Sheet configuration
# -----------------------------------------------------------------------------

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

    tab_name = str(
        config["tab_name"]
    ).replace("'", "''")

    return (
        f"'{tab_name}'!"
        f"{config['columns']}"
    )


def _get_sheets_service():
    """
    Build an authenticated Google Sheets API client
    using GOOGLE_SERVICE_ACCOUNT_JSON.
    """

    if not settings.google_service_account_json:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google Sheets integration is not "
                "configured on this server."
            ),
        )

    try:
        sa_info = json.loads(
            settings.google_service_account_json
        )

        creds = (
            service_account
            .Credentials
            .from_service_account_info(
                sa_info,
                scopes=SCOPES,
            )
        )

        service = build(
            "sheets",
            "v4",
            credentials=creds,
            cache_discovery=False,
        )

        return service

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Google auth error: {error}"
            ),
        )


# -----------------------------------------------------------------------------
# Databricks
# -----------------------------------------------------------------------------


def _trigger_databricks_job() -> int:
    """
    Trigger the configured Databricks job.

    IMPORTANT:
    This function is called ONLY by /post-data.

    Google Sheet commits must never call this function.

    Returns:
        Databricks run_id
    """

    raw_job_id = str(
        settings.databricks_job_id or ""
    ).strip()

    if not raw_job_id:
        raise HTTPException(
            status_code=503,
            detail=(
                "DATABRICKS_JOB_ID is not configured."
            ),
        )

    try:
        job_id = int(raw_job_id)

    except ValueError:
        raise HTTPException(
            status_code=500,
            detail=(
                "DATABRICKS_JOB_ID must contain only "
                "the numeric Databricks job ID. "
                f"Received: {raw_job_id!r}"
            ),
        )

    databricks_host = str(
        settings.databricks_host or ""
    ).strip()

    databricks_host = (
        databricks_host
        .removeprefix("https://")
        .removeprefix("http://")
        .rstrip("/")
    )

    if not databricks_host:
        raise HTTPException(
            status_code=503,
            detail=(
                "DATABRICKS_HOST is not configured."
            ),
        )

    databricks_token = str(
        settings.databricks_token or ""
    ).strip()

    if not databricks_token:
        raise HTTPException(
            status_code=503,
            detail=(
                "DATABRICKS_TOKEN is not configured."
            ),
        )

    url = (
        f"https://{databricks_host}"
        "/api/2.1/jobs/run-now"
    )

    headers = {
        "Authorization": (
            f"Bearer {databricks_token}"
        ),
        "Content-Type": "application/json",
    }

    payload = {
        "job_id": job_id,
    }

    try:
        response = httpx.post(
            url,
            json=payload,
            headers=headers,
            timeout=20.0,
            follow_redirects=True,
        )

    except Exception as error:
        logger.exception(
            "Databricks job trigger failed."
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Could not connect to Databricks: "
                f"{error}"
            ),
        )

    if response.status_code not in {
        200,
        201,
        202,
    }:
        error_message = (
            "Databricks returned HTTP "
            f"{response.status_code}: "
            f"{response.text[:500]}"
        )

        logger.error(error_message)

        raise HTTPException(
            status_code=502,
            detail=error_message,
        )

    try:
        response_data = response.json()
    except Exception:
        response_data = {}

    run_id = response_data.get("run_id")

    if run_id is None:
        logger.error(
            "Databricks accepted the job but "
            "did not return run_id. Response: %s",
            response.text[:500],
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Databricks accepted the request "
                "but did not return a run_id."
            ),
        )

    logger.info(
        "Databricks job %s triggered. run_id=%s",
        job_id,
        run_id,
    )

    return int(run_id)


# -----------------------------------------------------------------------------
# GET /api/edit-data/wos
# -----------------------------------------------------------------------------


@router.get(
    "/wos",
    response_model=SheetDataResponse,
    summary="Read WorkOrderSummaryReport sheet",
)
def get_wos_sheet(
    user: dict = Depends(
        require_permission("can_edit_data")
    ),
):
    """
    Fetch the full WorkOrderSummaryReport
    Google Sheet as-is.

    Row 0 is treated as the header row.
    """

    config = SHEET_CONFIG["wos"]

    service = _get_sheets_service()

    try:
        result = (
            service
            .spreadsheets()
            .values()
            .get(
                spreadsheetId=(
                    config["spreadsheet_id"]
                ),
                range=_get_sheet_range(config),
            )
            .execute()
        )

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "Google Sheets read failed: "
                f"{error}"
            ),
        )

    values = result.get(
        "values",
        [],
    )

    if not values:
        return SheetDataResponse(
            sheet_name="wos",
            headers=[],
            rows=[],
            total_rows=0,
        )

    headers = values[0]
    data_rows = values[1:]

    return SheetDataResponse(
        sheet_name="wos",
        headers=headers,
        rows=data_rows,
        total_rows=len(data_rows),
    )


# -----------------------------------------------------------------------------
# GET /api/edit-data/ows
# -----------------------------------------------------------------------------


@router.get(
    "/ows",
    response_model=SheetDataResponse,
    summary="Read OperationWiseWIPStatas sheet",
)
def get_ows_sheet(
    user: dict = Depends(
        require_permission("can_edit_data")
    ),
):
    """
    Fetch the full OperationWiseWIPStatas
    Google Sheet as-is.

    Row 0 is treated as the header row.
    """

    config = SHEET_CONFIG["ows"]

    service = _get_sheets_service()

    try:
        result = (
            service
            .spreadsheets()
            .values()
            .get(
                spreadsheetId=(
                    config["spreadsheet_id"]
                ),
                range=_get_sheet_range(config),
            )
            .execute()
        )

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "Google Sheets read failed: "
                f"{error}"
            ),
        )

    values = result.get(
        "values",
        [],
    )

    if not values:
        return SheetDataResponse(
            sheet_name="ows",
            headers=[],
            rows=[],
            total_rows=0,
        )

    headers = values[0]
    data_rows = values[1:]

    return SheetDataResponse(
        sheet_name="ows",
        headers=headers,
        rows=data_rows,
        total_rows=len(data_rows),
    )


# -----------------------------------------------------------------------------
# POST /api/edit-data/commit
#
# IMPORTANT:
# This endpoint ONLY updates Google Sheets.
#
# It MUST NOT trigger Databricks.
# -----------------------------------------------------------------------------


@router.post(
    "/commit",
    response_model=SheetWriteResponse,
    summary="Write current sheet changes to Google Sheets",
)
def commit_changes(
    body: SheetWriteRequest,
    user: dict = Depends(
        require_permission("can_edit_data")
    ),
):
    """
    Replace the contents of the selected Google Sheet.

    body.sheet_name must be either:
        wos
        ows

    This endpoint intentionally does NOT
    trigger the Databricks pipeline.
    """

    if body.sheet_name not in SHEET_CONFIG:
        raise HTTPException(
            status_code=400,
            detail=(
                "sheet_name must be 'wos' or 'ows'. "
                f"Got: '{body.sheet_name}'"
            ),
        )

    config = SHEET_CONFIG[
        body.sheet_name
    ]

    service = _get_sheets_service()

    all_rows = [
        body.headers,
        *body.rows,
    ]

    rows_to_write = len(
        body.rows
    )

    try:
        # -------------------------------------------------------------
        # Clear existing content
        # -------------------------------------------------------------

        (
            service
            .spreadsheets()
            .values()
            .clear(
                spreadsheetId=(
                    config["spreadsheet_id"]
                ),
                range=_get_sheet_range(config),
            )
            .execute()
        )

        # -------------------------------------------------------------
        # Write submitted content
        # -------------------------------------------------------------

        (
            service
            .spreadsheets()
            .values()
            .update(
                spreadsheetId=(
                    config["spreadsheet_id"]
                ),
                range=_get_sheet_range(config),
                valueInputOption="USER_ENTERED",
                body={
                    "values": all_rows,
                },
            )
            .execute()
        )

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "Google Sheets write failed: "
                f"{error}"
            ),
        )

    logger.info(
        "Committed %s rows to %s.",
        rows_to_write,
        body.sheet_name.upper(),
    )

    # -----------------------------------------------------------------
    # DO NOT trigger Databricks here.
    # -----------------------------------------------------------------

    return SheetWriteResponse(
        success=True,
        message=(
            f"Successfully wrote "
            f"{rows_to_write} rows to "
            f"{body.sheet_name.upper()} "
            f"({config['tab_name']})."
        ),
        sheet_name=body.sheet_name,
        rows_written=rows_to_write,
        job_triggered=False,
    )


# -----------------------------------------------------------------------------
# POST /api/edit-data/post-data
#
# This is the ONLY Edit Data API that triggers Databricks.
#
# It does NOT write to Google Sheets.
# -----------------------------------------------------------------------------


@router.post(
    "/post-data",
    response_model=PostDataResponse,
    summary="Trigger Databricks data processing job",
)
def post_data(
    user: dict = Depends(
        require_permission("can_edit_data")
    ),
):
    """
    Trigger the configured Databricks job.

    The frontend enables this action only after
    both WOS and OWS have been committed.
    """

    run_id = _trigger_databricks_job()

    return PostDataResponse(
        success=True,
        message=(
            "Data posted successfully. "
            "Databricks processing has been triggered."
        ),
        job_triggered=True,
        run_id=run_id,
    )