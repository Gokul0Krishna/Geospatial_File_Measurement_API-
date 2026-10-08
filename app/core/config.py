"""Application settings.

Every tunable lives here and is overridable through environment variables (see
``.env.example``).  Nothing else in the code base reads ``os.environ``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MiB = 1024 * 1024
GiB = 1024 * MiB


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- General -----------------------------------------------------------------
    APP_NAME: str = "Geospatial File Measurement API"
    ENVIRONMENT: Literal["dev", "test", "prod"] = "dev"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # --- Database ----------------------------------------------------------------
    # Postgres (prod):  postgresql+asyncpg://user:pass@host:5432/db
    # MySQL (optional): mysql+asyncmy://user:pass@host:3306/db?charset=utf8mb4
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/geo.db"
    # Convenient locally; in production run `alembic upgrade head` and set this to false.
    AUTO_CREATE_TABLES: bool = True
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_STATEMENT_TIMEOUT_MS: int = 30_000

    # --- Storage -----------------------------------------------------------------
    STORAGE_DIR: Path = Path("./data/storage")

    # --- Processing --------------------------------------------------------------
    # inline: processed inside the upload request (zero infrastructure, great for local runs)
    # worker: queued on Redis and processed by `arq` workers (production)
    PROCESSING_MODE: Literal["inline", "worker"] = "inline"
    REDIS_URL: str = "redis://localhost:6379/0"
    WORKER_MAX_JOBS: int = 2
    JOB_TIMEOUT_S: int = 900
    JOB_MAX_TRIES: int = 3
    JOB_RETRY_BASE_DELAY_S: int = 5

    # --- Upload limits -----------------------------------------------------------
    MAX_UPLOAD_BYTES: int = 100 * MiB
    MAX_KML_BYTES: int = 50 * MiB  # KML is XML: no random access, whole-file parse
    UPLOAD_CHUNK_BYTES: int = 1 * MiB

    # --- ZIP safety --------------------------------------------------------------
    MAX_ZIP_ENTRIES: int = 100
    MAX_ZIP_UNCOMPRESSED_BYTES: int = 1 * GiB
    MAX_ZIP_COMPRESSION_RATIO: int = 100
    ZIP_RATIO_MIN_BYTES: int = 1 * MiB  # ratio only checked for entries at least this large

    # --- Geospatial --------------------------------------------------------------
    REQUIRE_PRJ: bool = True  # strict: a shapefile without a readable CRS is rejected
    ASSUMED_CRS: str = "EPSG:4326"  # only used when REQUIRE_PRJ=false
    MAX_FEATURES: int = 1_000_000
    MAX_VERTICES: int = 10_000_000
    READ_BATCH_SIZE: int = 5_000
    LONG_EXTENT_DEGREES: float = 3.0  # warn when a feature is wider than this (longitude)

    # --- Pagination --------------------------------------------------------------
    PAGE_DEFAULT_LIMIT: int = 100
    PAGE_MAX_LIMIT: int = 1000
    PAGE_MAX_LIMIT_WITH_GEOMETRY: int = 100
    PAGE_MAX_GEOMETRY_BYTES: int = 2 * MiB  # summed WKB size of geometries in one page

    # --- Backpressure ------------------------------------------------------------
    MAX_INFLIGHT_JOBS: int = 100  # global PENDING + PROCESSING files
    MAX_ACTIVE_FILES_PER_CLIENT: int = 5

    # --- Housekeeping ------------------------------------------------------------
    RUN_SWEEPER_IN_API: bool = True  # inline mode only; worker mode uses an arq cron job
    SWEEP_INTERVAL_S: int = 300
    STALE_TMP_AGE_S: int = 3_600
    STALE_PENDING_S: int = 3_600
    STALE_PROCESSING_S: int = 2 * 900 + 300
    DELETE_GRACE_S: int = 300
    FAILED_RETENTION_S: int = 7 * 24 * 3_600
    ORPHAN_BLOB_GRACE_S: int = 3_600

    @model_validator(mode="after")
    def _check_limits(self) -> Settings:
        if self.PAGE_DEFAULT_LIMIT > self.PAGE_MAX_LIMIT:
            raise ValueError("PAGE_DEFAULT_LIMIT must be <= PAGE_MAX_LIMIT")
        if self.PAGE_MAX_LIMIT_WITH_GEOMETRY > self.PAGE_MAX_LIMIT:
            raise ValueError("PAGE_MAX_LIMIT_WITH_GEOMETRY must be <= PAGE_MAX_LIMIT")
        if self.MAX_KML_BYTES > self.MAX_UPLOAD_BYTES:
            raise ValueError("MAX_KML_BYTES must be <= MAX_UPLOAD_BYTES")
        if self.JOB_MAX_TRIES < 1:
            raise ValueError("JOB_MAX_TRIES must be >= 1")
        return self

    @property
    def tmp_dir(self) -> Path:
        return self.STORAGE_DIR / "tmp"

    @property
    def work_dir(self) -> Path:
        return self.STORAGE_DIR / "work"

    @property
    def files_dir(self) -> Path:
        return self.STORAGE_DIR / "files"


@lru_cache
def get_settings() -> Settings:
    return Settings()
