from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (e.g. DATABASE_URL)."""

    model_config = SettingsConfigDict(extra="ignore")

    # PostgreSQL, through SQLAlchemy and the psycopg 3 driver.
    database_url: str = "postgresql+psycopg://canals:canals@localhost:5432/canals"
    # Every API worker and reconciler instance has its own pool, so (workers + reconcilers) *
    # (pool_size + max_overflow) must stay below Postgres' max_connections.
    db_pool_size: int = 8
    db_max_overflow: int = 8
    # How long a request waits for a free connection before it is answered with a 503.
    db_pool_timeout_seconds: float = 10.0
    # How long to wait for a new connection to be made.
    db_connect_timeout_seconds: int = 5
    # Server-side limits, so that a slow query or a lock held by someone else can never hang a
    # request: Postgres cancels the statement and the API answers 503 (the request is retryable).
    db_statement_timeout_ms: int = 10_000
    db_lock_timeout_ms: int = 5_000

    geocoding_url: str = "http://localhost:8101"
    payments_url: str = "http://localhost:8102"

    geocoding_timeout_seconds: float = 3.0
    # Geocoding changes nothing, so retrying it is always safe.
    geocoding_max_attempts: int = 2
    geocoding_retry_backoff_seconds: float = 0.1
    payments_timeout_seconds: float = 5.0
    # Total attempts per payment call. Every attempt reuses the same idempotency key.
    payments_max_attempts: int = 2
    payments_retry_backoff_seconds: float = 0.2

    # Outbound HTTP connections per process and per service. Excess calls wait for a free one,
    # and give up (as "not sent") when the timeout runs out.
    http_max_connections: int = 100
    # Idle connections are dropped before the services' own keep-alive (5 s for gunicorn) does it,
    # so a request is never sent on a connection the other side is just closing.
    http_keepalive_expiry_seconds: float = 2.0

    # Circuit breakers (one per outbound service, per process): open when at least `min_calls` of
    # the last `window` calls are known and `failure_rate` of them failed.
    breaker_window: int = 40
    breaker_min_calls: int = 20
    breaker_failure_rate: float = 0.5
    breaker_recovery_seconds: float = 10.0

    # How long stock stays reserved for an order that is not authorized yet. It must be longer than
    # the slowest request (about 27 s of geocoder and provider calls), so an order is never
    # expired while its own request is still working on it.
    order_ttl_seconds: int = 120

    reconciler_interval_seconds: float = 10.0
    # AUTHORIZED orders younger than this are left to the request that is still handling them.
    reconciler_authorized_grace_seconds: int = 30
    # Orders taken per run, and how many are processed at the same time. A batch should be done
    # well within the lease: batch_size / concurrency * (slowest provider round) < lease.
    reconciler_batch_size: int = 50
    reconciler_concurrency: int = 10
    # How long an instance keeps an order to itself. If it dies, the order is picked up again.
    reconciler_lease_seconds: int = 60

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
