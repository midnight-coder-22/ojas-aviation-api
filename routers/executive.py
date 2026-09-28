# =============================================================================
# routers/executive.py - Executive Dashboard Routes
#
# GET /api/executive/overdue-by-department  - KPI 1: Overdue WOs per dept, flagged/unflagged
# GET /api/executive/mi-pending              - KPI 2: WOs with material issue still pending
# GET /api/executive/pending-watchlist       - KPI 0.5: pending SO lines + pending GRNs
# GET /api/executive/loss-trend              - KPI 3: commitment-fail loss, per financial year
# GET /api/executive/delay-overdue-trend     - KPI 4: daily Delayed+Overdue snapshot, per dept
# GET /api/executive/data-reminder           - "please run Post Data today" banner flag
#
# KPI 1, 2 and the watchlist are pure live reads over tables the existing
# pipeline already refreshes on every Post Data (dept_* tables, rpt_cust_po_wo,
# rpt_grn_qc, rpt_wo_mi, qc_entries) - nothing here changes any notebook.
#
# KPI 3 and KPI 4 are the only two that persist anything new
# (ojas_aviation.exec_loss_events / exec_delay_overdue_snapshots), because
# they must remember things across time that those snapshot-and-overwrite
# tables never keep. Both tables are created lazily by this router
# (_ensure_tables) rather than by a notebook - see the plan this was built
# from for why, and say so if you'd rather it were a notebook DDL cell. Both
# are written via MERGE (not SELECT-then-INSERT) so concurrent requests, or
# multiple Cloud Run instances, can't double-record the same event/snapshot.
#
# Performance notes (this file is on the hot path of the Executive page,
# which fires several of these requests at once):
# - _get_all_department_rows() caches the 6-table UNION ALL for a few
#   seconds so a single page load doesn't re-run it once per endpoint.
# - Independent Databricks round trips within one endpoint (e.g. the
#   watchlist's 3 queries) run concurrently via a shared thread pool -
#   database.py's driver is synchronous, so this is the only way to avoid
#   paying N network round trips back-to-back for N independent queries.
# =============================================================================

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from databricks.sql.exc import Error as DatabricksError
from fastapi import APIRouter, Depends, HTTPException

from config import settings
from database import fetch_all
from dependencies import get_current_user
from models import (
    DataReminderResponse,
    DelayOverdueTrendPoint,
    LossTrendPoint,
    MiPendingWorkOrder,
    OverdueByDepartmentRow,
    PendingWatchlistRow,
)
from routers.dashboard import (
    BUSINESS_TIMEZONE,
    DEPARTMENTS,
    _build_all_departments_query_with_flags,
    _build_department_summary,
    _has_vendor_movement,
    _prepare_dashboard_rows,
    _to_calendar_date,
    _to_ist_date,
)

router = APIRouter(prefix="/api/executive", tags=["Executive"])

SCHEMA = settings.databricks_schema
QC_TABLE = f"{SCHEMA}.qc_entries"
LOSS_EVENTS_TABLE = f"{SCHEMA}.exec_loss_events"
SNAPSHOTS_TABLE = f"{SCHEMA}.exec_delay_overdue_snapshots"

LOSS_RATE = 0.05
REMINDER_TIME = (8, 45)  # IST, (hour, minute)
SNAPSHOT_HOUR = 9        # IST
ALL_DEPT_CACHE_TTL_SECONDS = 8

_tables_ensured = False
_all_dept_cache: dict = {"rows": None, "fetched_at": 0.0}

# Shared across requests so independent Databricks round trips within one
# endpoint run concurrently instead of back-to-back; sized comfortably under
# database.py's connection pool (default 5) since at most 3 queries overlap
# from any single endpoint today.
_io_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="executive-io")


# =============================================================================
# TEXT / NUMBER / DATE PARSING
#
# rpt_* tables store every column as trimmed text (07_Report_Tables never
# parses), with the same ERP export quirks as everywhere else: DD/MM/YYYY
# text, sometimes an Excel serial. Mirrors _qc_date/_qc_number in
# 06_QC_Processing.py so the same input produces the same answer everywhere.
# =============================================================================

def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return text or None


def _clean_number(value) -> Optional[float]:
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _parse_erp_date(value) -> Optional[date]:
    text = _clean(value)
    if text is None:
        return None

    if re.fullmatch(r"\d+(\.\d+)?", text):
        serial = float(text)
        if 25000 <= serial <= 60000:
            return (datetime(1899, 12, 30) + timedelta(days=serial)).date()
        return None

    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = map(int, match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", text)
    if match:
        day, month, year = map(int, match.groups())
        try:
            return date(year + 2000 if year < 100 else year, month, day)
        except ValueError:
            return None

    return None


def _ageing_days(today: date, past: Optional[date]) -> Optional[int]:
    """Days since `past`, floored at 0 - a bad ERP date entered in the future
    must not produce a negative ageing value that sorts as "freshest" or gets
    silently bucketed into the 0-7 band."""
    if past is None:
        return None
    return max(0, (today - past).days)


def _financial_year(day: date) -> str:
    """Indian FY, Apr-Mar, labelled like the ERP's own WO numbering (e.g. 2026-27)."""
    start_year = day.year if day.month >= 4 else day.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def _latest_refreshed(rows: list[dict]) -> Optional[datetime]:
    return max(
        (row["last_refreshed"] for row in rows if row.get("last_refreshed")),
        default=None,
    )


def _earliest_refreshed(rows: list[dict]) -> Optional[datetime]:
    """The staleest department's last refresh - used for the reminder banner so
    a partial Post Data failure (one dept table not rewritten) still shows it,
    where MAX would hide behind whichever department did refresh."""
    return min(
        (row["last_refreshed"] for row in rows if row.get("last_refreshed")),
        default=None,
    )


def _fetch_rpt_or_503(query: str, params: list, report_label: str) -> list[dict]:
    try:
        return fetch_all(query, params)
    except DatabricksError as error:
        if "TABLE_OR_VIEW_NOT_FOUND" in str(error):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"{report_label} has not been generated yet. "
                    "Update the sheet in Edit Data and press Post Data."
                ),
            ) from error
        raise


def _ensure_tables() -> None:
    global _tables_ensured
    if _tables_ensured:
        return

    fetch_all(f"""
        CREATE TABLE IF NOT EXISTS {LOSS_EVENTS_TABLE} (
            event_id STRING, so_no STRING, item_no STRING, customer_name STRING,
            due_date DATE, financial_year STRING, qty DOUBLE, bal_qty DOUBLE,
            rate DOUBLE, loss_amount DOUBLE, detected_at TIMESTAMP,
            is_baseline BOOLEAN
        )
    """)
    fetch_all(f"""
        CREATE TABLE IF NOT EXISTS {SNAPSHOTS_TABLE} (
            snapshot_date DATE, department STRING, delayed_overdue_count INT,
            last_refreshed_seen TIMESTAMP, captured_at TIMESTAMP
        )
    """)
    _tables_ensured = True


def _table_has_rows(table: str) -> bool:
    return bool(fetch_all(f"SELECT 1 AS present FROM {table} LIMIT 1"))


def _merge_rows(table: str, rows: list[dict], key_columns: list[str]) -> None:
    """
    INSERT-if-not-already-present for every row in `rows`, atomically, via
    Delta's MERGE - not a SELECT-existing-then-INSERT-missing from Python,
    which races under concurrent requests (or multiple Cloud Run instances):
    two overlapping calls could both decide the same row is new and both
    INSERT it, double-counting it forever with no unique constraint to catch
    it. MERGE's WHEN NOT MATCHED is a single atomic statement per call.

    Columns are derived once from each row's own keys, so the SQL column
    list and the parameter list can never drift out of sync with each other.
    """
    if not rows:
        return

    columns = list(rows[0].keys())
    row_placeholder = f"({', '.join(['?'] * len(columns))})"
    values_sql = ", ".join([row_placeholder] * len(rows))
    params = [row[column] for row in rows for column in columns]

    source_columns = ", ".join(columns)
    insert_columns = ", ".join(columns)
    insert_values = ", ".join(f"source.{column}" for column in columns)
    match_condition = " AND ".join(f"target.{column} = source.{column}" for column in key_columns)

    fetch_all(
        f"""
        MERGE INTO {table} AS target
        USING (VALUES {values_sql}) AS source({source_columns})
        ON {match_condition}
        WHEN NOT MATCHED THEN INSERT ({insert_columns}) VALUES ({insert_values})
        """,
        params,
    )


# =============================================================================
# LIVE DEPARTMENT ROWS (shared by every KPI below)
#
# dashboard.py's UNION ALL with live WO-level flags (the same query the
# department cards use), because the stored has_active_flag column is only
# as fresh as the last Post Data run. Without it, raising/resolving a flag
# would not move a WO between KPI 1's flagged/unflagged bars until the next
# Post Data.
#
# Cached briefly: a single Executive page load fires this from 4 different
# endpoints (overdue-by-department, mi-pending, delay-overdue-trend,
# data-reminder) within the same second: querying the warehouse once and
# reusing the result avoids 4 full 6-table scans for 4 requests using the
# same underlying data.
# =============================================================================

def _get_all_department_rows() -> list[dict]:
    now = time.monotonic()
    cached_rows = _all_dept_cache["rows"]

    if cached_rows is not None and (now - _all_dept_cache["fetched_at"]) < ALL_DEPT_CACHE_TTL_SECONDS:
        return cached_rows

    rows = fetch_all(_build_all_departments_query_with_flags())
    _all_dept_cache["rows"] = rows
    _all_dept_cache["fetched_at"] = now
    return rows


# =============================================================================
# KPI 1 - Overdue work orders per department, split flagged / unflagged
#
# WOS/OWS-only, via the same live _derive_dashboard_status every department
# dashboard already uses. Deliberately independent of KPI 3's SO/PO-line
# commitment rule - see root CLAUDE.md.
# =============================================================================

def build_overdue_by_department(all_rows: list[dict]) -> list[dict]:
    prepared = _prepare_dashboard_rows(all_rows)
    counts = {
        department: {"flagged": 0, "unflagged": 0, "vendor_flagged": 0, "vendor_unflagged": 0}
        for department in DEPARTMENTS
    }

    for row in prepared:
        if row.get("status") != "Overdue":
            continue
        department = row.get("department")
        if department not in counts:
            continue
        bucket = "flagged" if bool(row.get("has_active_flag")) else "unflagged"
        counts[department][bucket] += 1
        if _has_vendor_movement(row):
            counts[department][f"vendor_{bucket}"] += 1

    return [
        {
            "department": department,
            **counts[department],
            "total": counts[department]["flagged"] + counts[department]["unflagged"],
        }
        for department in DEPARTMENTS
    ]


@router.get(
    "/overdue-by-department",
    response_model=list[OverdueByDepartmentRow],
    summary="Overdue work orders per department, split by flag status",
)
def get_overdue_by_department(user: dict = Depends(get_current_user)):
    all_rows = _get_all_department_rows()
    return [OverdueByDepartmentRow(**row) for row in build_overdue_by_department(all_rows)]


# =============================================================================
# KPI 2 - Work orders with material issue still pending, vs ageing
#
# Rule: a WO counts as MI-pending when the WORK ORDER VS MATERIAL ISSUE report
# (rpt_wo_mi) has Issue Status PENDING or PARTIAL on at least one of its BOM
# lines - the same "repeated lines merge; all COMPLETED means COMPLETED" rule
# 06_QC_Processing.py already uses for Inward QC, just per-WO instead of
# per-(WO, item). Restricted to WOs still present in the live dept_* universe,
# which also supplies the department tag for card-click filtering.
# =============================================================================

def build_wo_mi_pending_status(wo_mi_rows: list[dict]) -> dict[str, dict]:
    by_wo: dict[str, dict] = {}

    for row in wo_mi_rows:
        wo_id = _clean(row.get("work_order_no"))
        if not wo_id:
            continue
        entry = by_wo.setdefault(wo_id, {"statuses": set(), "work_order_date": None})
        status = _clean(row.get("issue_status"))
        if status:
            entry["statuses"].add(status.upper())
        if entry["work_order_date"] is None:
            parsed = _parse_erp_date(row.get("work_order_date"))
            if parsed is not None:
                entry["work_order_date"] = parsed

    return {
        wo_id: entry
        for wo_id, entry in by_wo.items()
        if entry["statuses"] and entry["statuses"] != {"COMPLETED"}
    }


def build_mi_pending_rows(
    all_dept_rows: list[dict],
    wo_mi_rows: list[dict],
    today: date,
) -> list[dict]:
    pending_by_wo = build_wo_mi_pending_status(wo_mi_rows)
    if not pending_by_wo:
        return []

    dept_row_by_wo: dict[str, dict] = {}
    for row in all_dept_rows:
        wo_id = _clean(row.get("wo_id"))
        if wo_id and row.get("department"):
            dept_row_by_wo.setdefault(wo_id, row)

    rows = []
    for wo_id, entry in pending_by_wo.items():
        dept_row = dept_row_by_wo.get(wo_id)
        if dept_row is None:
            continue  # not part of the current live WIP universe (e.g. already Completed)

        rows.append({
            "wo_id": wo_id,
            "department": dept_row["department"],
            "ageing_days": _ageing_days(today, entry["work_order_date"]),
            "vendor_involved": _has_vendor_movement(dept_row),
        })

    return rows


@router.get(
    "/mi-pending",
    response_model=list[MiPendingWorkOrder],
    summary="Work orders with material issue still pending, for the ageing chart",
)
def get_mi_pending(user: dict = Depends(get_current_user)):
    # Independent of each other (joined only afterward, in Python) - run concurrently.
    all_rows_future = _io_pool.submit(_get_all_department_rows)
    wo_mi_future = _io_pool.submit(
        _fetch_rpt_or_503,
        f"SELECT work_order_no, work_order_date, issue_status FROM {SCHEMA}.rpt_wo_mi",
        [],
        "The Work Order vs Material Issue report",
    )
    all_rows = all_rows_future.result()
    wo_mi_rows = wo_mi_future.result()

    today = datetime.now(BUSINESS_TIMEZONE).date()
    return [
        MiPendingWorkOrder(**row)
        for row in build_mi_pending_rows(all_rows, wo_mi_rows, today)
    ]


# =============================================================================
# KPI 0.5 - Pending watchlist: SO lines awaiting production + pending GRNs
#
# SO-line rows: rpt_cust_po_wo where bal_qty > 0 (SONo repeats per order, so
# (sono, itemno) is the closest thing to a line key - confirmed there is no
# better one). Material Availability is left "not available" for these: no
# report ties warehouse stock to a pre-WO SO line.
#
# GRN-line rows: every rpt_grn_qc row (the report itself is already scoped to
# "pending"). Material Availability reuses qc_entries (qc_type='Inward'),
# which already tracks exactly this via the production Inward-QC rule: a
# (grn_no, item_no) with an open entry there still needs material issued to
# some WO -> No; no open entry -> Yes (every matching WO is COMPLETED, or
# there was no matching WO at all). If qc_entries doesn't exist yet, Material
# Availability is reported as unknown ("-") rather than a false "Yes" -
# absence of an open entry means something different when we can't see the
# table at all versus when we can see it and it's genuinely empty.
# =============================================================================

def _is_closed_po_line(row: dict) -> bool:
    """
    OpenClose = Close means the customer PO was closed, so its remaining
    balance is no longer owed (user decision 2026-09-28): such lines are not
    pending and cannot cause a loss. A blank or missing value counts as open.
    """
    return (_clean(row.get("openclose")) or "").upper() in {"CLOSE", "CLOSED"}


def build_pending_watchlist_rows(
    cust_po_rows: list[dict],
    grn_rows: list[dict],
    open_inward_keys: set[tuple[Optional[str], Optional[str]]],
    qc_entries_available: bool,
    today: date,
) -> list[dict]:
    rows = []

    for row in cust_po_rows:
        bal_qty = _clean_number(row.get("balqty"))
        if not bal_qty or bal_qty <= 0 or _is_closed_po_line(row):
            continue

        rows.append({
            "kind": "SO",
            "reference_no": _clean(row.get("sono")),
            "item_no": _clean(row.get("itemno")),
            "description": _clean(row.get("custname")) or _clean(row.get("itemdesc")),
            "material_available": None,
            "ageing_days": _ageing_days(today, _parse_erp_date(row.get("podate"))),
        })

    for row in grn_rows:
        grn_no = _clean(row.get("grnno"))
        item_no = _clean(row.get("itemno"))
        is_open = (grn_no, item_no) in open_inward_keys
        material_available = (not is_open) if qc_entries_available else None

        rows.append({
            "kind": "GRN",
            "reference_no": grn_no,
            "item_no": item_no,
            "description": _clean(row.get("suppliername")) or _clean(row.get("itemdesc")),
            "material_available": material_available,
            "ageing_days": _ageing_days(today, _parse_erp_date(row.get("grndate"))),
        })

    return rows


def _fetch_open_inward_keys() -> tuple[set[tuple[Optional[str], Optional[str]]], bool]:
    """Returns (open (grn_no, item_no) keys, whether qc_entries could be read at all)."""
    try:
        inward_rows = fetch_all(f"SELECT grn_no, item_no FROM {QC_TABLE} WHERE qc_type = 'Inward'")
    except DatabricksError as error:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(error):
            raise
        return set(), False

    return {(row.get("grn_no"), row.get("item_no")) for row in inward_rows}, True


@router.get(
    "/pending-watchlist",
    response_model=list[PendingWatchlistRow],
    summary="Pending customer SO lines and pending GRNs, with material availability",
)
def get_pending_watchlist(user: dict = Depends(get_current_user)):
    # Three independent Databricks round trips - run them concurrently rather
    # than paying 3x one round-trip's latency back-to-back.
    cust_po_future = _io_pool.submit(
        _fetch_rpt_or_503,
        f"SELECT sono, itemno, custname, itemdesc, balqty, podate, openclose FROM {SCHEMA}.rpt_cust_po_wo",
        [],
        "The Pending Customer PO vs WO report",
    )
    grn_future = _io_pool.submit(
        _fetch_rpt_or_503,
        f"SELECT grnno, itemno, itemdesc, suppliername, grndate FROM {SCHEMA}.rpt_grn_qc",
        [],
        "The Pending Purchase GRN QC report",
    )
    inward_future = _io_pool.submit(_fetch_open_inward_keys)

    cust_po_rows = cust_po_future.result()
    grn_rows = grn_future.result()
    open_inward_keys, qc_entries_available = inward_future.result()

    today = datetime.now(BUSINESS_TIMEZONE).date()
    return [
        PendingWatchlistRow(**row)
        for row in build_pending_watchlist_rows(
            cust_po_rows, grn_rows, open_inward_keys, qc_entries_available, today,
        )
    ]


# =============================================================================
# KPI 3 - Loss vs time (a customer SO/PO line missing its DueDate)
#
# Independent of the WOS/OWS Overdue/Delayed rule used everywhere else
# (including KPI 1) - this is the Executive-only "commitment fail" the user
# asked for, scoped to rpt_cust_po_wo alone. First time an SO line is
# observed overdue-and-unfulfilled, one loss event is recorded and never
# re-fired for that line again (enforced by MERGE on event_id, not by
# pre-fetching every historical key from Python - see _merge_rows).
#
# The report has no line number and one (SONo, ItemNo) can carry several
# lines (different quantities or due dates), so a line is
# SONo:ItemNo:DueDate:n, n numbering the lines that share the first three.
# Event ids written before 2026-09-28 were just SONo:ItemNo, which folded
# those lines into one event; _migrate_legacy_loss_events converts them.
# =============================================================================

def find_current_breaches(
    cust_po_rows: list[dict],
    today: date,
    detected_at: datetime,
) -> list[dict]:
    """Every SO/PO line currently overdue-and-unfulfilled - not filtered by
    what's already recorded; _merge_rows' MERGE handles that atomically."""
    lines_by_key: dict[tuple[str, str, date], list[dict]] = {}

    for row in cust_po_rows:
        so_no = _clean(row.get("sono"))
        item_no = _clean(row.get("itemno"))
        if not so_no or not item_no:
            continue

        due_date = _parse_erp_date(row.get("duedate"))
        bal_qty = _clean_number(row.get("balqty"))
        if due_date is None or due_date >= today or not bal_qty or bal_qty <= 0:
            continue
        if _is_closed_po_line(row):
            continue

        rate = _clean_number(row.get("rate")) or 0.0
        qty = _clean_number(row.get("qty")) or 0.0

        lines_by_key.setdefault((so_no, item_no, due_date), []).append({
            "so_no": so_no,
            "item_no": item_no,
            "customer_name": _clean(row.get("custname")),
            "due_date": due_date,
            "financial_year": _financial_year(due_date),
            "qty": qty,
            "bal_qty": bal_qty,
            "rate": rate,
            "loss_amount": round(LOSS_RATE * rate * qty, 2),
            "detected_at": detected_at,
        })

    events = []
    for (so_no, item_no, due_date), lines in lines_by_key.items():
        # Number the lines in a fixed order so a re-ordered report gives
        # the same ids.
        lines.sort(key=lambda line: (line["qty"], line["rate"], line["bal_qty"]))
        for position, line in enumerate(lines):
            events.append({
                "event_id": f"{so_no}:{item_no}:{due_date.isoformat()}:{position}",
                **line,
            })

    return events


def _legacy_event_id(event: dict) -> str:
    return f"{event['so_no']}:{event['item_no']}"


def migrate_legacy_loss_events(
    breaches: list[dict],
    legacy_rows: list[dict],
    activation_date: date,
    report_keys: frozenset[str] = frozenset(),
) -> tuple[list[dict], list[str]]:
    """
    Pure part of the one-time move from SONo:ItemNo ids to line ids.

    Returns (line events to insert, legacy ids to delete). Every current
    breach whose SONo:ItemNo had a legacy event becomes its own line event;
    it is baseline exactly when its due date is before the day the KPI first
    ran (it was already breaching then), so lines the old id had folded away
    are recovered as real events. A legacy event whose SONo:ItemNo is still
    in the report (report_keys) but no longer breaches - a closed PO - is
    dropped. Legacy events whose line has left the report (since fulfilled)
    are kept as they are.
    """
    legacy_by_id = {row["event_id"]: row for row in legacy_rows}
    to_insert = []
    replaced = {legacy_id for legacy_id in legacy_by_id if legacy_id in report_keys}

    for event in breaches:
        legacy = legacy_by_id.get(_legacy_event_id(event))
        if legacy is None:
            continue
        replaced.add(legacy["event_id"])
        is_baseline = event["due_date"] < activation_date
        to_insert.append({
            **event,
            "detected_at": legacy.get("detected_at") or event["detected_at"],
            "is_baseline": is_baseline,
        })

    return to_insert, sorted(replaced)


def _migrate_legacy_loss_events(breaches: list[dict], report_keys: frozenset[str]) -> None:
    legacy_rows = fetch_all(
        f"SELECT event_id, detected_at FROM {LOSS_EVENTS_TABLE} "
        "WHERE size(split(event_id, ':')) = 2"
    )
    if not legacy_rows:
        return

    first_run = fetch_all(f"SELECT MIN(detected_at) AS first_detected FROM {LOSS_EVENTS_TABLE}")
    activation_date = _to_ist_date(first_run[0].get("first_detected")) if first_run else None
    if activation_date is None:
        return

    to_insert, replaced_ids = migrate_legacy_loss_events(
        breaches, legacy_rows, activation_date, report_keys,
    )

    # Insert first: if the DELETE below never runs, the next call finds the
    # same legacy ids and repeats both steps (the MERGE skips what exists).
    _merge_rows(LOSS_EVENTS_TABLE, to_insert, key_columns=["event_id"])
    for start in range(0, len(replaced_ids), 500):
        batch = replaced_ids[start:start + 500]
        fetch_all(
            f"DELETE FROM {LOSS_EVENTS_TABLE} WHERE event_id IN ({', '.join(['?'] * len(batch))})",
            batch,
        )


def build_loss_trend(events: list[dict], financial_year: str) -> list[dict]:
    monthly_totals: dict[str, float] = {}

    for event in events:
        if event["financial_year"] != financial_year:
            continue
        due_date = event["due_date"]
        month_key = f"{due_date.year:04d}-{due_date.month:02d}"
        monthly_totals[month_key] = monthly_totals.get(month_key, 0.0) + event["loss_amount"]

    running_total = 0.0
    points = []
    for month_key in sorted(monthly_totals):
        running_total += monthly_totals[month_key]
        points.append({
            "month": month_key,
            "loss_amount": round(monthly_totals[month_key], 2),
            "cumulative_loss": round(running_total, 2),
        })

    return points


def _record_new_loss_events(cust_po_rows: list[dict], today: date, detected_at: datetime) -> None:
    """
    The first time this ever runs, every already-breaching SO line (some
    years old in real ERP data) gets seeded as a baseline instead of being
    dumped into the chart as a giant day-one loss spike - the same "start
    with today, don't backfill history" rule the user specified for KPI 4.
    Every run after that records a real, chart-visible event only for lines
    that start breaching for the first time from here on.
    """
    _ensure_tables()
    is_baseline_seed = not _table_has_rows(LOSS_EVENTS_TABLE)
    breaches = find_current_breaches(cust_po_rows, today, detected_at)
    if not is_baseline_seed:
        report_keys = frozenset(
            f"{_clean(row.get('sono'))}:{_clean(row.get('itemno'))}"
            for row in cust_po_rows
            if _clean(row.get("sono")) and _clean(row.get("itemno"))
        )
        _migrate_legacy_loss_events(breaches, report_keys)
    rows = [{**event, "is_baseline": is_baseline_seed} for event in breaches]
    _merge_rows(LOSS_EVENTS_TABLE, rows, key_columns=["event_id"])


@router.get(
    "/loss-trend",
    response_model=list[LossTrendPoint],
    summary="Cumulative loss from missed customer commitments, for one financial year",
)
def get_loss_trend(financial_year: Optional[str] = None, user: dict = Depends(get_current_user)):
    cust_po_rows = _fetch_rpt_or_503(
        f"SELECT sono, itemno, custname, duedate, balqty, rate, qty, openclose FROM {SCHEMA}.rpt_cust_po_wo",
        [],
        "The Pending Customer PO vs WO report",
    )
    today = datetime.now(BUSINESS_TIMEZONE).date()

    _record_new_loss_events(cust_po_rows, today, datetime.now(BUSINESS_TIMEZONE))

    raw_events = fetch_all(
        f"SELECT so_no, item_no, due_date, financial_year, loss_amount "
        f"FROM {LOSS_EVENTS_TABLE} WHERE is_baseline = false"
    )
    events = [
        {**row, "due_date": _to_calendar_date(row["due_date"])}
        for row in raw_events
        if row.get("due_date") is not None
    ]

    target_fy = financial_year or _financial_year(today)
    return [LossTrendPoint(**point) for point in build_loss_trend(events, target_fy)]


# =============================================================================
# KPI 4 - Delayed + Overdue work orders per department, over time
#
# One line per department; y = Delayed + Overdue from the same
# status_breakdown _build_department_summary already computes for every
# other dashboard. Snapshot rule (evaluated opportunistically - no Cloud
# Scheduler): capture immediately if the table is empty ("start with
# today"); otherwise only when today is past the last snapshot date AND at
# least one Post Data refresh has happened since AND it's past 9 AM IST.
# Snapshot rows are written via MERGE keyed on (snapshot_date, department),
# so two requests racing to capture the same day can't leave duplicate rows.
# =============================================================================

def current_delay_overdue_counts(all_rows: list[dict]) -> dict[str, int]:
    rows_by_department: dict[str, list[dict]] = {department: [] for department in DEPARTMENTS}
    for row in all_rows:
        department = row.get("department")
        if department in rows_by_department:
            rows_by_department[department].append(row)

    counts = {}
    for department in DEPARTMENTS:
        summary = _build_department_summary(department, rows_by_department[department])
        counts[department] = (
            summary.status_breakdown.get("Delayed", 0)
            + summary.status_breakdown.get("Overdue", 0)
        )
    return counts


def _maybe_capture_snapshot(all_rows: list[dict], today: date, now_ist: datetime) -> None:
    _ensure_tables()

    latest_refreshed = _latest_refreshed(all_rows)
    last_snapshot_rows = fetch_all(
        f"""
        SELECT snapshot_date, last_refreshed_seen FROM {SNAPSHOTS_TABLE}
        ORDER BY snapshot_date DESC LIMIT 1
        """
    )

    if not last_snapshot_rows:
        should_capture = True
    else:
        last_snapshot_date = _to_calendar_date(last_snapshot_rows[0]["snapshot_date"])
        last_refreshed_seen = last_snapshot_rows[0]["last_refreshed_seen"]
        refreshed_since = bool(latest_refreshed) and (
            last_refreshed_seen is None or latest_refreshed > last_refreshed_seen
        )
        should_capture = bool(
            last_snapshot_date is not None
            and today > last_snapshot_date
            and refreshed_since
            and now_ist.hour >= SNAPSHOT_HOUR
        )

    if not should_capture:
        return

    counts = current_delay_overdue_counts(all_rows)
    rows = [
        {
            "snapshot_date": today,
            "department": department,
            "delayed_overdue_count": counts[department],
            "last_refreshed_seen": latest_refreshed,
            "captured_at": now_ist,
        }
        for department in DEPARTMENTS
    ]
    _merge_rows(SNAPSHOTS_TABLE, rows, key_columns=["snapshot_date", "department"])


@router.get(
    "/delay-overdue-trend",
    response_model=list[DelayOverdueTrendPoint],
    summary="Daily Delayed+Overdue work-order count per department, for one financial year",
)
def get_delay_overdue_trend(financial_year: Optional[str] = None, user: dict = Depends(get_current_user)):
    all_rows = _get_all_department_rows()
    today = datetime.now(BUSINESS_TIMEZONE).date()
    now_ist = datetime.now(BUSINESS_TIMEZONE)

    _maybe_capture_snapshot(all_rows, today, now_ist)

    raw_rows = fetch_all(
        f"SELECT snapshot_date, department, delayed_overdue_count FROM {SNAPSHOTS_TABLE} ORDER BY snapshot_date"
    )
    target_fy = financial_year or _financial_year(today)

    points = []
    for row in raw_rows:
        snapshot_date = _to_calendar_date(row["snapshot_date"])
        if snapshot_date is None or _financial_year(snapshot_date) != target_fy:
            continue
        points.append({
            "snapshot_date": snapshot_date,
            "department": row["department"],
            "count": row["delayed_overdue_count"],
        })

    return [DelayOverdueTrendPoint(**point) for point in points]


# =============================================================================
# Data-upload reminder banner (Admin role only, no push - polled lazily)
# =============================================================================

@router.get(
    "/data-reminder",
    response_model=DataReminderResponse,
    summary="Whether to show the Admin a 'please upload today's data' banner",
)
def get_data_reminder(user: dict = Depends(get_current_user)):
    if user.get("role") != "Admin":
        return DataReminderResponse(show=False)

    now_ist = datetime.now(BUSINESS_TIMEZONE)
    if (now_ist.hour, now_ist.minute) < REMINDER_TIME:
        return DataReminderResponse(show=False)

    all_rows = _get_all_department_rows()
    # Earliest, not latest: one department silently failing to refresh (a
    # partial Post Data run) must still trigger the reminder, not hide
    # behind whichever departments did refresh today.
    refreshed_today = _to_ist_date(_earliest_refreshed(all_rows)) == now_ist.date()

    return DataReminderResponse(
        show=not refreshed_today,
        message=(
            None if refreshed_today else
            "No data has been posted today. Open Edit Data and press Post Data "
            "once today's reports are ready."
        ),
    )
