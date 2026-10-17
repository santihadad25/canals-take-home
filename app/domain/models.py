"""The business concepts of the order system: money, locations, stock items and the order itself.

Nothing in here talks to a database, a web framework or another service.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from app.domain.errors import InvalidCoordinates, InvalidOrder, InvalidStateTransition

_EARTH_RADIUS_IN_KM = 6371.0088


@dataclass(frozen=True)
class Money:
    """An amount in cents. Integer arithmetic only, never floats."""

    cents: int

    def __post_init__(self) -> None:
        if self.cents < 0:
            raise InvalidOrder("money cannot be negative")

    def multiplied_by(self, quantity: int) -> Money:
        return Money(self.cents * quantity)

    def __add__(self, other: Money) -> Money:
        return Money(self.cents + other.cents)


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            raise InvalidCoordinates(f"out of range: {self.latitude}, {self.longitude}")

    def distance_km_to(self, other: Coordinates) -> float:
        """Great-circle distance between two points, using the haversine formula."""
        latitude_1 = math.radians(self.latitude)
        longitude_1 = math.radians(self.longitude)
        latitude_2 = math.radians(other.latitude)
        longitude_2 = math.radians(other.longitude)

        latitude_difference = latitude_2 - latitude_1
        longitude_difference = longitude_2 - longitude_1
        haversine = (
            math.sin(latitude_difference / 2) ** 2
            + math.cos(latitude_1) * math.cos(latitude_2) * math.sin(longitude_difference / 2) ** 2
        )
        return 2 * _EARTH_RADIUS_IN_KM * math.asin(math.sqrt(min(1.0, haversine)))


@dataclass(frozen=True)
class Warehouse:
    id: int
    name: str
    location: Coordinates


@dataclass(frozen=True)
class RequestedItem:
    """What the customer asked for, before the price is known."""

    product_id: int
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidOrder("quantity must be positive")


@dataclass(frozen=True)
class OrderItem:
    """A line of an order. The unit price is the one the product had when it was bought."""

    product_id: int
    quantity: int
    unit_price: Money

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidOrder("quantity must be positive")

    @property
    def subtotal(self) -> Money:
        return self.unit_price.multiplied_by(self.quantity)


class OrderStatus(StrEnum):
    PENDING = "PENDING"  # stock reserved, payment not authorized yet
    AUTHORIZED = "AUTHORIZED"  # funds held on the card, not captured yet
    PAID = "PAID"  # funds captured: the order is confirmed
    PAYMENT_FAILED = "PAYMENT_FAILED"  # the payment was refused, nothing was captured
    PAYMENT_UNKNOWN = "PAYMENT_UNKNOWN"  # we do not know if the payment was authorized
    EXPIRED = "EXPIRED"  # the payment was not authorized in time, nothing was captured

    @property
    def is_final(self) -> bool:
        """A final order never changes again."""
        return not _ALLOWED_TRANSITIONS[self]

    @property
    def releases_stock(self) -> bool:
        """Entering one of these statuses gives the reserved stock back to the warehouse."""
        return self in (OrderStatus.PAYMENT_FAILED, OrderStatus.EXPIRED)

    def can_transition_to(self, new_status: OrderStatus) -> bool:
        return new_status in _ALLOWED_TRANSITIONS[self]


_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset(
        {
            OrderStatus.AUTHORIZED,
            OrderStatus.PAYMENT_FAILED,
            OrderStatus.PAYMENT_UNKNOWN,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PAYMENT_UNKNOWN: frozenset(
        {OrderStatus.AUTHORIZED, OrderStatus.PAYMENT_FAILED, OrderStatus.EXPIRED}
    ),
    # An authorized order never expires: its funds are held and the sale is accepted, so it is
    # completed (captured) or, if the provider says the authorization is gone, it fails.
    OrderStatus.AUTHORIZED: frozenset({OrderStatus.PAID, OrderStatus.PAYMENT_FAILED}),
    OrderStatus.PAID: frozenset(),
    OrderStatus.PAYMENT_FAILED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}

# The orders whose payment has not been authorized (yet). Only these orders can expire.
STATUSES_AWAITING_AUTHORIZATION = frozenset({OrderStatus.PENDING, OrderStatus.PAYMENT_UNKNOWN})


def ensure_can_transition(from_statuses: Collection[OrderStatus], new_status: OrderStatus) -> None:
    """Raises InvalidStateTransition if any of these statuses cannot move to the new one."""
    for from_status in from_statuses:
        if not from_status.can_transition_to(new_status):
            raise InvalidStateTransition(f"{from_status.value} -> {new_status.value}")


def ensure_order_data_is_valid(
    *, customer_id: str, shipping_address: str, product_ids: Sequence[int]
) -> None:
    """The rules every order has to meet. Raises InvalidOrder when one is broken."""
    if not customer_id.strip():
        raise InvalidOrder("customer_id is required")
    if not shipping_address.strip():
        raise InvalidOrder("shipping_address is required")
    if not product_ids:
        raise InvalidOrder("an order needs at least one item")
    if len(set(product_ids)) != len(product_ids):
        raise InvalidOrder("a product can only appear once per order")


@dataclass(frozen=True)
class Order:
    id: UUID
    customer_id: str  # an id owned by another system: it is stored as given, never validated
    idempotency_key: UUID
    request_hash: str
    shipping_address: str
    shipping_location: Coordinates
    warehouse_id: int
    items: tuple[OrderItem, ...]
    total: Money
    status: OrderStatus
    expires_at: datetime  # the deadline for the payment to get authorized
    created_at: datetime
    payment_id: str | None = None
    failure_code: str | None = None

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        customer_id: str,
        idempotency_key: UUID,
        request_hash: str,
        shipping_address: str,
        shipping_location: Coordinates,
        warehouse_id: int,
        items: Sequence[OrderItem],
        now: datetime,
        time_to_live: timedelta,
    ) -> Order:
        ensure_order_data_is_valid(
            customer_id=customer_id,
            shipping_address=shipping_address,
            product_ids=[item.product_id for item in items],
        )

        total = Money(0)
        for item in items:
            total = total + item.subtotal

        return cls(
            id=id,
            customer_id=customer_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            shipping_address=shipping_address,
            shipping_location=shipping_location,
            warehouse_id=warehouse_id,
            items=tuple(items),
            total=total,
            status=OrderStatus.PENDING,
            expires_at=now + time_to_live,
            created_at=now,
        )

    def is_overdue(self, now: datetime) -> bool:
        """Is it past its deadline without a payment that was authorized? Then it has to expire.

        The deadline only applies until the payment is authorized. From then on the order does not
        expire any more: its capture is retried until it goes through.
        """
        return self.status in STATUSES_AWAITING_AUTHORIZATION and now >= self.expires_at
