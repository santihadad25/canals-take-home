"""Telling "the database is having a moment" apart from "there is a bug".

Transient trouble (a restart, a dropped connection, a lock or statement timeout, a deadlock, a
full pool) is answered with a retryable 503. Retrying is always safe: every change is atomic, and
an Idempotency-Key makes a repeated request resume where it stopped. Even when the outcome of a
commit is unknown because the connection died during it, the order either exists (and the retry
resumes it, or the reconciler expires it) or it does not.
"""

import psycopg
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

# PostgreSQL error codes (SQLSTATE) that mean "try again". Codes of class 08 (connection
# exceptions) also count, they are matched by prefix.
TRANSIENT_SQLSTATES = {
    "40P01",  # deadlock detected
    "40001",  # serialization failure
    "55P03",  # lock not available (our lock_timeout)
    "57014",  # query canceled (our statement_timeout)
    "57P01",  # the server is shutting down on an administrator's command
    "57P02",  # the server crashed and is shutting down
    "57P03",  # cannot connect now: the server is starting up or shutting down
    "53300",  # too many connections
}

# Everything that can come out of a database call, wrapped by SQLAlchemy or not. The API registers
# a handler for each of these classes.
DATABASE_ERROR_TYPES: tuple[type[BaseException], ...] = (
    DBAPIError,
    PoolTimeoutError,
    psycopg.Error,
    OSError,
)


def is_transient_database_error(error: BaseException) -> bool:
    if isinstance(error, PoolTimeoutError | OperationalError | InterfaceError):
        return True
    if isinstance(error, DBAPIError):
        return error.connection_invalidated or _is_transient_cause(error.orig)
    return _is_transient_cause(error)


def _is_transient_cause(error: BaseException | None) -> bool:
    """Also covers what the driver raises *unwrapped*, for example from a commit whose connection
    died halfway."""
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate:
        return sqlstate in TRANSIENT_SQLSTATES or sqlstate.startswith("08")
    # No SQLSTATE: PostgreSQL did not answer, the connection itself failed.
    return isinstance(error, psycopg.OperationalError | psycopg.InterfaceError | OSError)


def describe_database_error(error: BaseException) -> str:
    """For logs: the type of the error, plus the PostgreSQL error code when there is one."""
    cause = error.orig if isinstance(error, DBAPIError) else error
    sqlstate = getattr(cause, "sqlstate", None)
    error_type = type(error).__name__
    return f"{error_type} (sqlstate {sqlstate})" if sqlstate else error_type
