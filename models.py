# =============================================================================
# models.py — Pydantic request and response models
# =============================================================================

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# =============================================================================
# AUTHENTICATION
# =============================================================================

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

    user_id: str
    full_name: str
    username: str
    role: str
    department: str
    dashboard_access: str

    can_edit_data: bool
    can_flag: bool
    can_resolve_flag: bool


# =============================================================================
# DASHBOARD
# =============================================================================

class WorkOrderKPI(BaseModel):
    """
    One work-order row returned by a department dashboard endpoint.

    The fields must remain aligned with the columns written by the
    Databricks department-table refresh pipeline.
    """

    model_config = ConfigDict(from_attributes=True)

    wo_id: str
    wo_name: str

    dept_in_date: Optional[date] = None

    # Overall work-order target date from OWS.
    wo_target_date: Optional[date] = None

    # Target date for the work order's current dashboard department.
    dept_target_date: Optional[date] = None

    wo_ageing_days: Optional[int] = None
    dept_ageing_days: Optional[int] = None

    planned_qty: int

    next_dept: Optional[str] = None

    priority: str
    status: str

    expected_steps: int
    done_steps: int

    qc_alert: bool
    mi_alert: bool

    has_active_flag: bool = False

    last_refreshed: Optional[datetime] = None


class DepartmentResponse(BaseModel):
    department: str
    record_count: int
    data: list[WorkOrderKPI]


class DepartmentSummary(BaseModel):
    department: str

    total_wos: int
    qc_alert_count: int
    mi_alert_count: int
    flagged_count: int = 0

    status_breakdown: dict[str, int]
    priority_breakdown: dict[str, int]

    last_refreshed: Optional[datetime] = None


class IncomingFlowRow(BaseModel):
    """
    Incoming WO counts from one source department,
    separated by priority.
    """
    source_department: str
    low: int = 0
    medium: int = 0
    high: int = 0
    total: int = 0


class IncomingFlowResponse(BaseModel):
    """
    Complete incoming-flow KPI response for one target department.
    """
    target_department: str
    total_wos: int
    data: list[IncomingFlowRow]


# =============================================================================
# FLAGS
# =============================================================================

class FlagCreateRequest(BaseModel):
    wo_ids: list[str]
    item_no: Optional[str] = None
    department: str


class FlagResolveRequest(BaseModel):
    wo_ids: list[str]


class FlagRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sr_no: Optional[int] = None
    wo_id: str
    item_no: Optional[str] = None
    department: str
    flag_status: int

    raised_date: Optional[datetime] = None
    resolved_date: Optional[datetime] = None

    raised_by: Optional[str] = None
    resolved_by: Optional[str] = None


# =============================================================================
# EDIT DATA — GOOGLE SHEETS
# =============================================================================

class SheetDataResponse(BaseModel):
    sheet_name: str
    headers: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    total_rows: int = 0


class SheetWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_name: str
    headers: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)


class SheetWriteResponse(BaseModel):
    success: bool
    message: str

    sheet_name: Optional[str] = None
    rows_written: int = 0
    job_triggered: bool = False