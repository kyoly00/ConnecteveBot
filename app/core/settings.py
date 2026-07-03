"""
ConnBot 배포·보안 필수 환경변수 (pydantic-settings).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENVIRONMENT: str = Field(default="development", description="development | production")

    DATABASE_URL: str | None = None
    SLACK_BOT_TOKEN: str = ""
    SLACK_SIGNING_SECRET: str = ""

    OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE: str = ""
    OUTLOOK_ROOM_WEBHOOK_NOTIFICATION_URL: str = ""
    OUTLOOK_ROOM_WEBHOOK_ENABLED: bool = True

    CONFLUENCE_WEBHOOK_SECRET: str = ""
    CONFLUENCE_WEBHOOK_ALLOWED_IPS: str = Field(
        default="",
        description="쉼표 구분 IP/CIDR. 비어 있으면 IP 검사 생략.",
    )
    CONFLUENCE_WEBHOOK_REQUIRE_AUTH: bool = Field(
        default=False,
        description="True면 secret/IP 검증. production은 항상 검증.",
    )

    PUBLIC_BASE_URL: str = ""

    @field_validator(
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE",
        "CONFLUENCE_WEBHOOK_SECRET",
        "PUBLIC_BASE_URL",
        mode="before",
    )
    @classmethod
    def _strip_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in ("production", "prod")

    @property
    def confluence_allowed_ip_list(self) -> list[str]:
        return [
            part.strip()
            for part in (self.CONFLUENCE_WEBHOOK_ALLOWED_IPS or "").split(",")
            if part.strip()
        ]

    def readiness_config_checks(self) -> dict[str, bool]:
        return {
            "database_url_configured": bool(self.DATABASE_URL),
            "slack_signing_secret_configured": bool(self.SLACK_SIGNING_SECRET),
            "graph_client_state_configured": bool(self.OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE),
            "confluence_webhook_secret_configured": bool(self.CONFLUENCE_WEBHOOK_SECRET),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
