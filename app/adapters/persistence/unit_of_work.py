from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.repositories import (
    SqlCatalogRepository,
    SqlOrderRepository,
    SqlWarehouseRepository,
)


class SqlAlchemyUnitOfWork:
    """One database transaction. Whatever is not committed is rolled back when it ends."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._session_factory()
        self.catalog = SqlCatalogRepository(self._session)
        self.warehouses = SqlWarehouseRepository(self._session)
        self.orders = SqlOrderRepository(self._session)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self._session.rollback()  # does nothing after a commit
        finally:
            self._session.close()

    def commit(self) -> None:
        self._session.commit()
