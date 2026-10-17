"""The database engine: SQLAlchemy's connection pool on top of psycopg 3."""

from sqlalchemy import Engine, create_engine

from app.config import Settings


def create_database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,  # a connection that died while idle is replaced, not handed out
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            # Settings of every session, sent to PostgreSQL when the connection is made:
            # * Limits enforced by the server, so a slow query or a lock held by someone else can
            #   never hang a request: the statement is canceled and the API answers 503.
            # * Time in UTC, so the timestamps that come back are always in UTC.
            "options": (
                f"-c statement_timeout={int(settings.db_statement_timeout_ms)}"
                f" -c lock_timeout={int(settings.db_lock_timeout_ms)}"
                " -c TimeZone=UTC"
            ),
        },
    )
