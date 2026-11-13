from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GOVERNANCE_", env_file=".env", extra="ignore"
    )

    environment: Literal["local", "staging", "production"] = "local"
    public_base_path: str = "/api/v1"
    port: int = Field(default=8005, ge=1024, le=65535)
    # Supervisor routes stay unavailable (503) until the verifier is configured.
    admin_verifier_base_url: AnyHttpUrl | None = None
    admin_verifier_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    database: Path | None = None
    # Pending cases expire, and are denied, after this window.
    review_window_minutes: int = Field(default=1440, ge=1, le=10080)
    qa_upstream_base_url: AnyHttpUrl | None = None
    qa_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    qa_policy_path: Path = Path("policies/search-qa-review.draft-1.json")
    qa_retain_synthetic_deliveries: bool = False
    qa_release_key: SecretStr | None = Field(default=None, min_length=32)

    @field_validator("qa_upstream_base_url", "admin_verifier_base_url")
    @classmethod
    def validate_service_origin(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and (
            value.path not in ("", "/", None)
            or value.query is not None
            or value.fragment is not None
            or value.username is not None
            or value.password is not None
        ):
            raise ValueError("Configure an upstream origin without credentials or path")
        return value

    @field_validator("public_base_path")
    @classmethod
    def validate_public_base_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or any(character in value for character in ("?", "#", "\\", "{", "}"))
            or "//" in value
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Use an absolute URL path without query or fragment")
        return value.rstrip("/") or "/"

    @model_validator(mode="after")
    def require_qa_settings(self) -> "Settings":
        if self.qa_retain_synthetic_deliveries and self.environment != "local":
            raise ValueError(
                "Synthetic delivery retention requires the local environment"
            )
        if self.qa_upstream_base_url is not None and (
            self.database is None or self.qa_release_key is None
        ):
            raise ValueError("Governed Q&A requires a database and a release key")
        return self
