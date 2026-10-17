"""The clock of the system: the time of the database server."""

from datetime import datetime

from sqlalchemy import Engine, text


class DatabaseClock:
    """Deadlines, leases and last-change times are written by some processes and compared by
    others, possibly on other machines. Reading the time from the one database they all share
    means a skewed local clock can never make them disagree.

    It borrows a connection for an instant, so it must not be called while a transaction is open.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self) -> datetime:
        with self._engine.connect() as connection:
            return connection.execute(text("SELECT clock_timestamp()")).scalar_one()
