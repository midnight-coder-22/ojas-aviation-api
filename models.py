# =============================================================================
# models.py — Pydantic Response Models
#
# These models define the exact shape of every API response.
# Pydantic validates the data coming from Databricks before it is sent
# to the frontend, catching any type mismatches early.
# =============================================================================

from __future__ import annotations
from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, EmailStr
from typing import Optional, Any, Dict, List


class SheetDataResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = True
    sheet_name: Optional[str] = None
    table_name: Optional[str] = None
    columns: List[str] = []
    rows: List[Dict[str, Any]] = []
    data: List[Dict[str, Any]] = []
    total_rows: Optional[int] = None
    message: Optional[str] = None


class SheetWriteRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    sheet_name: Optional[str] = None
    table_name: Optional[str] = None
    record_id: Optional[str] = None
    row_id: Optional[str] = None
    row_index: Optional[int] = None
    column_name: Optional[str] = None
    value: Optional[Any] = None
    data: Optional[Dict[str, Any]] = None
    updates: Optional[Dict[str, Any]] = None
    updated_by: Optional[str] = None


class SheetWriteResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = True
    message: Optional[str] = None
    sheet_name: Optional[str] = None
    table_name: Optional[str] = None
    record_id: Optional[str] = None
    row_id: Optional[str] = None
    rows_affected: Optional[int] = None
    updated_data: Optional[Dict[str, Any]] = None


# class LoginRequest(BaseModel):
#     email: EmailStr
#     password: str
class LoginRequest(BaseModel):
    username: str
    password: str

# class LoginResponse(BaseModel):
#     access_token: str
#     token_type: str = "bearer"
#     user_id: Optional[int] = None
#     email: Optional[EmailStr] = None
class LoginResponse(BaseModel):
    access_token: str
    user_id: str
    full_name: str
    username: str
    role: str
    department: str
    dashboard_access: str
    can_edit_data: bool
    can_flag: bool
    can_resolve_flag: bool


class FlagCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    table_name: Optional[str] = None
    record_id: Optional[str] = None
    column_name: Optional[str] = None
    reason: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = None
    created_by: Optional[str] = None


class FlagResolveRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    flag_id: Optional[str] = None
    resolution: Optional[str] = None
    resolved_by: Optional[str] = None
    notes: Optional[str] = None


class FlagRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    flag_id: Optional[str] = None
    table_name: Optional[str] = None
    record_id: Optional[str] = None
    column_name: Optional[str] = None
    reason: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    created_by: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution: Optional[str] = None
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None


# -----------------------------------------------------------------------------
# Single Work Order KPI record — one row from a department Delta table
# -----------------------------------------------------------------------------
class WorkOrderKPI(BaseModel):
    wo_id:            str                # Work Order ID
    wo_name:          str                # Item name for dashboard display
    dept_in_date:     Optional[date]     # Date WO arrived at current department (can be null)
    wo_ageing_days:   Optional[int]      # Total days since WO was opened (null if no start date)
    dept_ageing_days: Optional[int]      # Days in current department (null if no dept-in date)
    planned_qty:      int                # Planned production quantity
    next_dept:        Optional[str]      # Next department in sequence (null if last/none)
    priority:         str                # Low / Medium / High
    status:           str                # New / InProcess / Completed
    expected_steps:   int                # Total expected production steps
    done_steps:       int                # Steps completed so far
    qc_alert:         bool               # True → QC inspection required after this dept
    mi_alert:         bool               # True → Material Issue needed before next dept
    last_refreshed:   Optional[datetime] = None # Timestamp of last pipeline refresh

    class Config:
        # Allow instantiation directly from ORM/dict objects
        from_attributes = True


# -----------------------------------------------------------------------------
# Department-level response — wraps the list of WO records with metadata
# -----------------------------------------------------------------------------
class DepartmentResponse(BaseModel):
    department:    str                   # Canonical department name
    record_count:  int                   # Total WOs in this department
    data:          list[WorkOrderKPI]    # Full list of WO KPI records


# -----------------------------------------------------------------------------
# Lightweight summary — for overview cards without loading all row data
# -----------------------------------------------------------------------------
class DepartmentSummary(BaseModel):
    department:       str
    total_wos:        int                # Total work orders in department
    qc_alert_count:   int                # Number of WOs with QC alert active
    mi_alert_count:   int                # Number of WOs with MI alert active
    status_breakdown: dict[str, int]     # e.g. { "New": 10, "InProcess": 5, "Completed": 2 }
    priority_breakdown: dict[str, int]   # e.g. { "Low": 12, "Medium": 3, "High": 2 }
    last_refreshed:   Optional[datetime]
