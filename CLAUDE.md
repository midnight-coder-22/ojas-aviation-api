# ojas_api — FastAPI backend

Serves the Ojas Aviation dashboard from Delta tables in Databricks (schema `ojas_aviation`) and reads/writes the Google Sheets behind the Edit Data page. Deployed to Google Cloud Run. The workspace-level guide (data flow, business rules, local tooling) is `../CLAUDE.md`, which exists only on the developer machine.

## Layout
- `main.py`: app, CORS (`CORS_ORIGIN`, comma-separated), slowapi rate limit (300/day per IP), router registration, pool shutdown.
- `config.py`: pydantic settings from `.env` or Cloud Run env (`cloudrun.env.yaml` is the reference). Optional: `GOOGLE_SERVICE_ACCOUNT_JSON`, `DATABRICKS_JOB_ID`, and one `<KEY>_SPREADSHEET_ID` per ERP report sheet (e.g. `GRN_QC_SPREADSHEET_ID`).
- `database.py`: pooled Databricks SQL connections. `fetch_all(query, params)` uses `?` placeholders, returns a list of dicts, and retries once on a stale connection.
- `security.py`, `dependencies.py`: JWT auth. Use `Depends(get_current_user)`, `require_role(...)`, or `require_permission("can_edit_data" | "can_flag" | "can_resolve_flag")`.
- `models.py`: Pydantic request/response models (`WorkOrderKPI`, `DepartmentSummary`, `QcEntry`, ...).
- `routers/`
  - `auth.py`: `/api/auth/login`, `/api/auth/me`.
  - `dashboard.py`: `/api/departments`, `/api/dashboard/all/summary`, `/api/dashboard/{dept}`, `/summary`, `/incoming-flow`. Owns `_derive_dashboard_status` (live Overdue/Delayed), which `qc.py` reuses.
  - `qc.py`: `/api/qc/dashboard`. Returns every row of `qc_entries` plus live `qc_ageing_days` and `has_active_flag` (flags with department `QC`). Returns 503 while the table does not exist yet.
  - `executive.py`: `/api/executive/overdue-by-department`, `/mi-pending`, `/pending-watchlist`, `/loss-trend`, `/delay-overdue-trend`, `/data-reminder`. All but the last two are pure live reads over `dept_*`, `rpt_cust_po_wo`, `rpt_grn_qc`, `rpt_wo_mi` and `qc_entries` — no notebook changes needed. `loss-trend` and `delay-overdue-trend` also opportunistically write to two new tables this router creates itself (`exec_loss_events`, `exec_delay_overdue_snapshots`, via `CREATE TABLE IF NOT EXISTS` — a deliberate exception to "notebooks own schema", see the file's module docstring). `loss-trend` seeds any already-breaching commitments as a silent baseline on first-ever call instead of backfilling years of history into day one.
  - `flags.py`: `/api/flags`, `/api/flags/{dept}`, `/raise`, `/resolve`. A WO has at most one active flag across all departments.
  - `edit_data.py`: `SHEET_CONFIG` = `wos`/`ows` (tab `Sheet1`, A:T) plus the nine `REPORT_SHEETS` (own spreadsheet each, first tab, A:AZ). `GET /sheet/{key}` reads any of them (`/wos`, `/ows` are older aliases), `/commit` clears and rewrites a sheet and never triggers the job, `/post-data` calls the Databricks Jobs API run-now and is the only trigger.
- `databricks_save_tables.py`: legacy helper, not imported by the app.

## Conventions
- A new dashboard gets its own router and prefix (like `/api/qc`). A fixed path under `/api/dashboard/` would be captured by `/api/dashboard/{department}`, which is registered first.
- SQL: interpolate only `settings.databricks_schema` and constants; bind everything else with `?`.
- Make response fields `Optional` unless the pipeline guarantees them; one incomplete row must not fail the whole response.
- Delta `DATE`/`TIMESTAMP` values arrive as `date` / naive-UTC `datetime`. Business "today" is IST (`BUSINESS_TIMEZONE` in `dashboard.py`). Tag naive UTC timestamps with `_as_utc` (in `dashboard.py`) before they reach the browser. Every `last_refreshed` goes through it; otherwise browsers read them as local time, a 5.5 hour error in IST.
- A new ERP report sheet: add its key to `REPORT_SHEETS`, a `<key>_spreadsheet_id` setting in `config.py` (plus `.env` and `cloudrun.env.yaml`), and mirror the key in the frontend's `EDIT_SHEETS`. No new route is needed.

## Run, test, deploy
- `.venv/Scripts/python.exe -m uvicorn main:app --reload`. Needs `.env` and reads the production warehouse.
- There is no test suite in this repo. `../dev_tools/api_qc_test.py` exercises `qc.py` and the Edit Data config with Databricks and auth mocked; `../dev_tools/mock_api.py` serves the real QC router for frontend work.
- Deploy: Cloud Run source deploy (`.gcloudignore`, `Procfile`). Ask the user before deploying.
