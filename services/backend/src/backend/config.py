from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRANSPORT_", env_file=".env", extra="ignore"
    )
    db_path: str = ".runtime/transport.sqlite"
    api_token: str = Field(min_length=16)
    ndtp_host: str = "127.0.0.1"
    ndtp_port: int = Field(default=9201, ge=0, le=65535)
    run_id: str = Field(default="live", min_length=1, max_length=100)
    schedule_version: str = "default"
    ml_url: str | None = None
    prediction_interval: float = Field(default=30, ge=0.1)
    ml_timeout: float = Field(default=8, gt=0, le=8)
    max_pending: int = Field(default=10000, ge=1)
    history_seconds: int = Field(default=1800, ge=60, le=86400)
    max_telemetry_per_cycle: int = Field(default=100000, ge=1, le=1000000)
    max_http_bytes: int = Field(default=2_000_000, ge=1024)
    max_connections: int = Field(default=1000, ge=1, le=10000)

    @field_validator("ml_url")
    @classmethod
    def url(cls, value):
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("ML URL must use http or https")
        return value or None
