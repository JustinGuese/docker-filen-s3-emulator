"""Application settings, layered from the .env convention."""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_ENV = os.getenv("APP_ENV", "local")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", f".env.{APP_ENV}", ".env.secret"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "filen-s3-emulator"
    log_level: str = "INFO"

    # The one key pair clients of this service sign with.
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # The Filen CLI gateway (`filen s3`) this service fronts.
    filen_endpoint: str = ""
    filen_access_key: str = ""
    filen_secret_key: str = ""
    filen_region: str = "filen"

    # The gateway buffers a whole PUT in memory, so a completed multipart upload -- which
    # becomes one PUT -- is capped. Tune against the gateway pod's memory.
    max_object_bytes: int = 1024**3
    staging_dir: str = "/staging"
    multipart_expiry_hours: int = 24
    list_cache_seconds: float = 15.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
