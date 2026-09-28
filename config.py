from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    databricks_host: str
    databricks_http_path: str
    databricks_token: str
    databricks_schema: str = "ojas_aviation"
    # The Executive page alone fires ~9 requests at once, some with 2-3 queries each.
    databricks_pool_size: int = 8

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 525600
    jwt_expire_minutes: int = 525600

    cors_origin: str = "http://localhost:5173,http://127.0.0.1:5173,https://midnight-coder-22.github.io"

    wos_spreadsheet_id: str
    ows_spreadsheet_id: str

    # One Google Spreadsheet per ERP report (read from its first tab).
    grn_qc_spreadsheet_id: str | None = None
    wo_mi_spreadsheet_id: str | None = None
    vendor_inward_spreadsheet_id: str | None = None
    pdi_spreadsheet_id: str | None = None
    cust_po_wo_spreadsheet_id: str | None = None
    issue_vs_return_spreadsheet_id: str | None = None
    material_issue_spreadsheet_id: str | None = None
    po_grn_spreadsheet_id: str | None = None
    material_return_spreadsheet_id: str | None = None
    f7_inward_spreadsheet_id: str | None = None

    google_service_account_json: str | None = None
    databricks_job_id: str | None = None

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()