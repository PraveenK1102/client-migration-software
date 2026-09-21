"""Application configuration.

All model/runtime knobs are read from the environment or the backend ``.env`` file.
The model endpoint and provider are server-controlled; uploaded content can never
change them (see the untrusted-input handling in ``llm/prompt.py``).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo layout anchors (…/backend/app/config.py -> repo root is two parents up from app).
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"


class Settings(BaseSettings):
    """Runtime settings.

    Field names map to UPPER_CASE env vars (pydantic-settings is case-insensitive),
    e.g. ``llm_provider`` <- ``LLM_PROVIDER``.
    """

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Model provider ---------------------------------------------------
    llm_provider: str = "groq"            # "groq" (real) or "fake" (explicitly test/demo only)
    groq_model: str = "openai/gpt-oss-20b"
    groq_api_key: str | None = None

    # --- Model call limits (initial configurable limits, not performance claims) ---
    llm_timeout_seconds: float = 60.0     # per-request timeout
    llm_max_attempts: int = 3             # total API attempts per proposal (incl. corrective retry)
    llm_max_concurrency: int = 2          # bounded concurrent model calls

    # --- Estimated AI cost (configurable per deployment, NOT hard-coded in the UI) ---
    # USD per 1,000,000 tokens; the estimated cost is computed server-side from the persisted token
    # usage. Defaults track the Groq openai/gpt-oss-20b published rate and are overridable per model/
    # deployment via LLM_PRICE_INPUT_PER_1M / LLM_PRICE_OUTPUT_PER_1M in backend/.env.
    llm_price_input_per_1m: float = 0.10
    llm_price_output_per_1m: float = 0.50

    # --- M3E: enum value-domain batching (decouple model calls from EMPLOYEE ROW COUNT) ---
    # Once a source column is accepted as mapping to a target ENUM/BOOLEAN, only its still-unresolved
    # DISTINCT values (after deterministic normalization) are sent to the model, in bounded SEQUENTIAL
    # batches of this size. So 100k rows with 150 unresolved distinct labels = ceil(150/25)=6 calls,
    # never per-row calls. Chosen conservatively for the Groq free tier after eyeballing token size.
    llm_enum_batch_size: int = 25         # distinct UNRESOLVED enum values per transform call
    # Bounded distinct-domain budget sent to the model per column. A domain larger than this is NOT
    # silently truncated: the overflow is surfaced as a scoped review/capacity decision (never lost).
    llm_enum_max_distinct: int = 2000

    # --- Ingestion limits (enforced while reading, not from the extension) ---
    max_upload_bytes: int = 5 * 1024 * 1024   # 5 MB per file
    max_rows_per_table: int = 5000            # bounded rows per source table
    max_files_per_job: int = 10
    sample_values_per_column: int = 5         # bounded representative values sent to the model
    sample_value_max_chars: int = 120

    # --- Target schema ----------------------------------------------------
    # Versioned contract file under schemas/ (YAML or JSON). employee.v2 = representative HR
    # contract with collections + tenant custom attributes; employee.v1 (six fields) still loads.
    schema_version: str = "employee.v2"
    default_tenant_id: str = "default"        # tenant used when an upload names none
    seed_demo_tenants: bool = True            # load schemas/tenants/*.yaml demo seeds at startup
                                              # (set false for a clean instance with no built-in orgs)

    # --- SQLite hardening (single-node runtime DB) -----------------------
    sqlite_busy_timeout_ms: int = 5000        # wait, don't fail, on a briefly-locked write

    # --- Durable work queue + bounded local workers ----------------------
    max_file_workers: int = 4                 # bounded local worker tasks (no unbounded create_task)
    work_lease_seconds: int = 120             # a processing item is reclaimable after this if not renewed
    work_poll_interval_seconds: float = 0.25
    work_max_attempts: int = 5
    start_workers: bool = True                # lifespan starts the worker pool (tests may disable)
    worker_id: str = ""                       # resolved per-process if blank

    # --- M3B: autonomy + target delivery (writes go ONLY through TargetEmployeeGateway) ----
    auto_continue: bool = True                # safe stages chain automatically (prep -> reconcile -> deliver)
    target_timeout_seconds: float = 10.0      # bounded per-request timeout for target HTTP calls
    target_max_attempts: int = 5              # network attempts per delivery/rollback operation
    target_max_concurrency: int = 4           # concurrent target write requests (bounded semaphore)
    target_retry_base_seconds: float = 0.5    # exponential backoff base (with jitter)
    target_retry_max_seconds: float = 30.0    # backoff cap (also caps an honoured Retry-After)
    delivery_crash_after_send: bool = False   # TEST-ONLY: hard-exit right after the target accepted a write

    # --- Mock target system (HTTP boundary) ------------------------------
    # By default the mock target runs IN-PROCESS behind an httpx ASGI transport so the
    # single-node app (and the offline tests) reconcile over the same HTTP+gateway seam
    # without a separate port. Set target_inprocess=False to talk to a target service
    # running at target_base_url instead (the production swap: point at the real system).
    target_inprocess: bool = True
    target_base_url: str = "http://127.0.0.1:8100"
    target_lookup_batch_size: int = 200

    # --- Observability / LangSmith tracing (OPTIONAL; the app runs fully with tracing off) ----
    # Engineering observability of the LangGraph runs (node timing, model calls, latency/tokens).
    # Disabled unless both a key is present AND tracing is turned on. PII-safe: only bounded profile
    # evidence ever reaches a model call, so a trace never carries whole employee rows.
    langsmith_api_key: str | None = None
    langsmith_tracing: bool = False
    langsmith_project: str = "darwinbox-migration"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # --- Storage (kept out of git; server-generated names only) -----------
    data_dir: Path = REPO_ROOT / "data"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def blob_store_root(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def app_db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def target_db_path(self) -> Path:
        return self.data_dir / "target.db"     # mock target service's own SQLite file

    @property
    def checkpoint_db_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def resolved_worker_id(self) -> str:
        import os
        import socket
        return self.worker_id or f"{socket.gethostname()}-{os.getpid()}"

    @property
    def schema_path(self) -> Path:
        from .schema_loader import resolve_schema_path
        return resolve_schema_path(SCHEMAS_DIR, self.schema_version)

    @property
    def tenant_seeds_dir(self) -> Path:
        return SCHEMAS_DIR / "tenants"

    @property
    def llm_operation_deadline_seconds(self) -> float:
        """Overall deadline for a single proposal operation.

        Derived from the configured per-request timeout and attempt budget plus a
        small buffer for backoff sleeps. Kept internal so the documented .env set
        stays minimal; it is a bound, not a latency claim.
        """
        return self.llm_timeout_seconds * self.llm_max_attempts + 30.0

    @property
    def groq_key(self) -> str | None:
        """The Groq key, treating absent/blank/whitespace as unconfigured (None)."""
        cleaned = (self.groq_api_key or "").strip()
        return cleaned or None

    @property
    def langsmith_key(self) -> str | None:
        """The LangSmith key, treating absent/blank/whitespace as unconfigured (None)."""
        cleaned = (self.langsmith_api_key or "").strip()
        return cleaned or None

    @property
    def tracing_enabled(self) -> bool:
        """Tracing runs only when explicitly turned on AND a key is present."""
        return bool(self.langsmith_tracing and self.langsmith_key)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.blob_store_root.mkdir(parents=True, exist_ok=True)

    def provider_status(self) -> dict:
        """Non-secret provider-configuration diagnostics.

        This reports whether a credential is *present* — NOT whether it authenticates.
        A nonempty key is configured, not verified. Never returns the key or its length.
        Distinguishes configuration_missing from an empty inherited env override.
        """
        provider = self.llm_provider.lower()
        raw = self.groq_api_key
        env_val = os.environ.get("GROQ_API_KEY")
        inherited_empty_override = env_val is not None and env_val.strip() == ""

        if provider == "fake":
            return {
                "provider": "fake", "model": self.groq_model, "configured": True,
                "category": "configured", "requires_key": False,
                "key_present": False, "key_nonempty": False,
                "inherited_empty_override": inherited_empty_override,
                "env_file": str(BACKEND_DIR / ".env"),
                "env_file_exists": (BACKEND_DIR / ".env").exists(),
            }

        configured = provider == "groq" and self.groq_key is not None
        if configured:
            category = "configured"          # present, not yet verified via a live call
        elif inherited_empty_override:
            category = "configuration_missing_inherited_empty_override"
        else:
            category = "configuration_missing"
        return {
            "provider": provider, "model": self.groq_model, "configured": configured,
            "category": category, "requires_key": provider == "groq",
            "key_present": raw is not None,
            "key_nonempty": self.groq_key is not None,
            "inherited_empty_override": inherited_empty_override,
            "env_file": str(BACKEND_DIR / ".env"),
            "env_file_exists": (BACKEND_DIR / ".env").exists(),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
