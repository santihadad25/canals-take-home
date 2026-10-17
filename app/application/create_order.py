import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

from app.application.errors import (
    DuplicateIdempotencyKey,
    IdempotencyKeyReused,
    OutOfStock,
    ProductNotFound,
)
from app.application.order_payment_service import OrderPaymentService
from app.application.ports import Clock, Geocoder, UnitOfWorkFactory
from app.domain.errors import InvalidOrder
from app.domain.models import (
    STATUSES_AWAITING_AUTHORIZATION,
    Money,
    Order,
    OrderItem,
    OrderStatus,
    RequestedItem,
    Warehouse,
    ensure_order_data_is_valid,
)
from app.domain.services import rank_warehouses_by_distance


@dataclass(frozen=True)
class CreateOrderCommand:
    idempotency_key: UUID
    customer_id: str
    shipping_address: str
    items: tuple[RequestedItem, ...]
    # Never stored, never logged, and left out of repr and of the request hash.
    card_number: str = field(repr=False)

    def __post_init__(self) -> None:
        ensure_order_data_is_valid(
            customer_id=self.customer_id,
            shipping_address=self.shipping_address,
            product_ids=[item.product_id for item in self.items],
        )
        if not self.card_number.strip():
            raise InvalidOrder("card_number is required")

    def request_hash(self) -> str:
        """A fingerprint of the request, to detect an idempotency key reused for another request.

        The card number is left out on purpose: nothing derived from it is ever stored.
        """
        canonical_request = json.dumps(
            {
                "customer_id": self.customer_id,
                "shipping_address": self.shipping_address,
                "items": sorted([item.product_id, item.quantity] for item in self.items),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical_request.encode()).hexdigest()


@dataclass(frozen=True)
class OrderResult:
    order: Order
    already_existed: bool = False  # True when this idempotency key had already created the order


@dataclass(frozen=True)
class _PlacementPlan:
    """What is needed to place a new order: the prices and the warehouses that could fill it."""

    unit_prices: dict[int, Money]
    candidate_warehouses: list[Warehouse]


class CreateOrder:
    """Places an order: reserves its stock in the closest warehouse that has everything, then
    authorizes and captures the payment. A retry with the same idempotency key resumes the order
    where it was left instead of creating or charging a second one."""

    def __init__(
        self,
        *,
        new_unit_of_work: UnitOfWorkFactory,
        geocoder: Geocoder,
        payment_service: OrderPaymentService,
        clock: Clock,
        order_time_to_live: timedelta,
    ) -> None:
        self._new_unit_of_work = new_unit_of_work
        self._geocoder = geocoder
        self._payment_service = payment_service
        self._clock = clock
        self._order_time_to_live = order_time_to_live

    def execute(self, command: CreateOrderCommand) -> OrderResult:
        request_hash = command.request_hash()

        existing_order = self._find_order_by_idempotency_key(command.idempotency_key)
        if existing_order is not None:
            return self._handle_existing_order(existing_order, command, request_hash)

        plan = self._plan_new_order(command)
        try:
            order = self._place_order(command, request_hash, plan)
        except DuplicateIdempotencyKey:
            # A concurrent request with the same idempotency key saved its order first: continue
            # with that order instead of creating a second one.
            existing_order = self._find_order_by_idempotency_key(command.idempotency_key)
            assert existing_order is not None
            return self._handle_existing_order(existing_order, command, request_hash)

        return self._complete_payment(order, command.card_number, already_existed=False)

    def _find_order_by_idempotency_key(self, idempotency_key: UUID) -> Order | None:
        with self._new_unit_of_work() as unit_of_work:
            return unit_of_work.orders.find_by_idempotency_key(idempotency_key)

    def _handle_existing_order(
        self, order: Order, command: CreateOrderCommand, request_hash: str
    ) -> OrderResult:
        if order.request_hash != request_hash:
            raise IdempotencyKeyReused("this Idempotency-Key was used for a different request")

        if order.status.is_final or order.is_overdue(self._clock()):
            # Either it is done, or it never got authorized and is past its deadline: then the
            # reconciler settles it and nothing new is started for it. (An AUTHORIZED order never
            # expires: a retry goes on to capture it.)
            return OrderResult(order, already_existed=True)

        return self._complete_payment(order, command.card_number, already_existed=True)

    def _plan_new_order(self, command: CreateOrderCommand) -> _PlacementPlan:
        """Read the prices and the warehouses that could fill a new order.

        Only reads: nothing is written and nothing is locked.
        """
        product_ids = [item.product_id for item in command.items]
        with self._new_unit_of_work() as unit_of_work:
            unit_prices = unit_of_work.catalog.get_unit_prices(product_ids)
            unknown_product_ids = sorted(set(product_ids) - unit_prices.keys())
            if unknown_product_ids:
                raise ProductNotFound(unknown_product_ids)

            # A cheap pre-check, so that an order nobody can fill never reaches the geocoder.
            candidate_warehouses = unit_of_work.warehouses.find_warehouses_with_stock_for(
                list(command.items)
            )

        if not candidate_warehouses:
            raise OutOfStock("no warehouse has all the requested products in stock")
        return _PlacementPlan(unit_prices, candidate_warehouses)

    def _place_order(
        self, command: CreateOrderCommand, request_hash: str, plan: _PlacementPlan
    ) -> Order:
        """Choose the closest warehouse that still has the stock, and reserve it.

        No transaction is open while the geocoder is called, and nothing is charged here.
        """
        # This also validates the address, even when only one warehouse could fill the order.
        shipping_location = self._geocoder.geocode(command.shipping_address)
        warehouses_closest_first = rank_warehouses_by_distance(
            plan.candidate_warehouses, shipping_location
        )

        now = self._clock()
        order_id = uuid4()
        order_items = tuple(
            OrderItem(item.product_id, item.quantity, plan.unit_prices[item.product_id])
            for item in command.items
        )
        for warehouse in warehouses_closest_first:
            order = Order.create(
                id=order_id,
                customer_id=command.customer_id,
                idempotency_key=command.idempotency_key,
                request_hash=request_hash,
                shipping_address=command.shipping_address,
                shipping_location=shipping_location,
                warehouse_id=warehouse.id,
                items=order_items,
                now=now,
                time_to_live=self._order_time_to_live,
            )
            if self._save_order_and_reserve_its_stock(order):
                return order

        raise OutOfStock("the stock changed while the order was being placed")

    def _save_order_and_reserve_its_stock(self, order: Order) -> bool:
        """In one transaction, save the order and take its stock from its warehouse.

        The stock pre-check made before took no locks, so the stock may have moved since: this
        conditional reservation is what really decides. When the stock is gone, nothing is saved:
        leaving the transaction without committing also rolls the order back.
        """
        with self._new_unit_of_work() as unit_of_work:
            unit_of_work.orders.add(order)
            stock_was_reserved = unit_of_work.warehouses.reserve_stock(
                order.warehouse_id, order.items
            )
            if not stock_was_reserved:
                return False
            unit_of_work.commit()
            return True

    def _complete_payment(
        self, order: Order, card_number: str, *, already_existed: bool
    ) -> OrderResult:
        """Take the order as far as it can go: authorize its payment, then capture it."""
        if order.status in STATUSES_AWAITING_AUTHORIZATION:
            order = self._payment_service.authorize(order, card_number)
        if order.status is OrderStatus.AUTHORIZED:
            order = self._payment_service.capture(order)
        return OrderResult(order, already_existed=already_existed)
