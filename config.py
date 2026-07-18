from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    databricks_host: str
    databricks_http_path: str
    databricks_token: str
    databricks_schema: str = "ojas_aviation"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 525600
    jwt_expire_minutes: int = 525600

    cors_origin: str = "http://localhost:5173,http://127.0.0.1:5173,https://midnight-coder-22.github.io"

    wos_spreadsheet_id: str
    ows_spreadsheet_id: str

    google_service_account_json: str | None = None
    databricks_job_id: str | None = None

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()