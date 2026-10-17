"""The interfaces the use cases need from the outside world (databases, other services, the clock).

The use cases only know these interfaces. The adapters implement them.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.domain.models import (
    Coordinates,
    Money,
    Order,
    OrderItem,
    OrderStatus,
    RequestedItem,
    Warehouse,
)

Clock = Callable[[], datetime]


class CatalogRepository(Protocol):
    def get_unit_prices(self, product_ids: Sequence[int]) -> dict[int, Money]:
        """The price of each product that exists. Unknown product ids are simply left out."""
        ...


class WarehouseRepository(Protocol):
    def find_warehouses_with_stock_for(
        self, requested_items: Sequence[RequestedItem]
    ) -> list[Warehouse]:
        """The warehouses that hold enough of EVERY requested product right now.

        This is a plain read, without locks, so it is only a pre-check. What really guarantees
        the stock is `reserve_stock`.
        """
        ...

    def reserve_stock(self, warehouse_id: int, items: Sequence[OrderItem]) -> bool:
        """Take the stock of all the items at once. False if any item is short.

        The caller must roll the transaction back when this returns False.
        """
        ...

    def release_stock(self, warehouse_id: int, items: Sequence[OrderItem]) -> None:
        """Give the stock of all the items back to the warehouse."""
        ...


class OrderRepository(Protocol):
    def find_by_id(self, order_id: UUID) -> Order | None: ...

    def find_by_idempotency_key(self, idempotency_key: UUID) -> Order | None: ...

    def add(self, order: Order) -> None:
        """Raises DuplicateIdempotencyKey if the idempotency key is already taken."""
        ...

    def try_change_status(
        self,
        order_id: UUID,
        *,
        expected_statuses: Collection[OrderStatus],
        new_status: OrderStatus,
        changed_at: datetime,
        payment_id: str | None = None,
        failure_code: str | None = None,
    ) -> bool:
        """Change the status, but only if the order currently has one of the expected statuses.

        Returns whether it was changed. This makes concurrent changes safe: when two actors try
        to move the same order, only one of them succeeds.
        """
        ...

    def claim_authorized_orders_idle_since(
        self, *, idle_since: datetime, now: datetime, lease_duration: timedelta, max_orders: int
    ) -> list[Order]:
        """Take up to `max_orders` AUTHORIZED orders that have not changed since `idle_since`.

        A claimed order is not handed to anybody else until its lease runs out, so several
        reconciler instances split the work. The caller must commit for the claim to stick.
        """
        ...

    def claim_overdue_orders(
        self, *, now: datetime, lease_duration: timedelta, max_orders: int
    ) -> list[Order]:
        """Same, for the orders that never got authorized and are past their deadline."""
        ...


class UnitOfWork(Protocol):
    """One database transaction, with the repositories that work inside it."""

    catalog: CatalogRepository
    warehouses: WarehouseRepository
    orders: OrderRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, exc_type, exc, traceback) -> None:
        """Rolls back anything that was not committed."""
        ...

    def commit(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]


class Geocoder(Protocol):
    def geocode(self, address: str) -> Coordinates:
        """Raises InvalidAddress or GeocodingUnavailable."""
        ...


class PaymentStatus(StrEnum):
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    CANCELED = "canceled"


class PaymentGateway(Protocol):
    """A card payment provider with two-step payments: authorize first, then capture or cancel.

    Every call that changes something takes an idempotency key: repeating it returns the original
    result instead of doing the work twice. Besides the errors documented below, every method
    raises PaymentNotSent or PaymentOutcomeUnknown for network-level problems.
    """

    def authorize(
        self, *, idempotency_key: str, amount: Money, description: str, card_number: str
    ) -> str:
        """Hold the funds and return the payment id. Raises PaymentRejected."""
        ...

    def capture(self, *, payment_id: str, idempotency_key: str) -> PaymentStatus:
        """CAPTURED on success, CANCELED if the authorization is no longer valid."""
        ...

    def cancel(self, *, payment_id: str, idempotency_key: str) -> PaymentStatus:
        """CANCELED on success, CAPTURED if it had already been captured."""
        ...

    def get_status(self, payment_id: str) -> PaymentStatus: ...
