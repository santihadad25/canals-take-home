"""The repositories: they translate between the domain objects and the PostgreSQL tables.

Every method costs a fixed number of SQL statements, however many items or orders it handles.
tests/test_query_budget.py guards that, so an N+1 cannot sneak back in.
"""

from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import (
    CTE,
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    and_,
    column,
    func,
    insert,
    literal,
    or_,
    select,
    update,
    values,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    OrderEventRow,
    OrderItemRow,
    OrderRow,
    ProductRow,
    StockRow,
    WarehouseRow,
)
from app.application.errors import DuplicateIdempotencyKey
from app.domain.models import (
    STATUSES_AWAITING_AUTHORIZATION,
    Coordinates,
    Money,
    Order,
    OrderItem,
    OrderStatus,
    RequestedItem,
    Warehouse,
    ensure_can_transition,
)


class SqlCatalogRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_unit_prices(self, product_ids: Sequence[int]) -> dict[int, Money]:
        rows = self._session.execute(
            select(ProductRow.id, ProductRow.unit_price_cents).where(ProductRow.id.in_(product_ids))
        )
        return {product_id: Money(price_in_cents) for product_id, price_in_cents in rows}


class SqlWarehouseRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_warehouses_with_stock_for(
        self, requested_items: Sequence[RequestedItem]
    ) -> list[Warehouse]:
        # A warehouse qualifies when it has a stock row, with enough quantity, for every requested
        # product. Product ids are unique within an order, so counting the matching rows is enough.
        #
        # It is written as a join against the requested (product, quantity) pairs so that
        # PostgreSQL answers it with an index-only scan on (product_id, warehouse_id) INCLUDE
        # (quantity), without touching the table. Measured with EXPLAIN on 1000 warehouses: 25
        # buffers instead of ~2100 for the equivalent chain of ORs, and it stays flat as the
        # stock grows.
        requested_quantities = _requested_quantities_table(
            (item.product_id, item.quantity) for item in requested_items
        )
        warehouse_ids_with_enough_stock = (
            select(StockRow.warehouse_id)
            .join(
                requested_quantities,
                and_(
                    StockRow.product_id == requested_quantities.c.product_id,
                    StockRow.quantity >= requested_quantities.c.quantity,
                ),
            )
            .group_by(StockRow.warehouse_id)
            .having(func.count() == len(requested_items))
        )
        warehouse_rows = self._session.execute(
            select(WarehouseRow)
            .where(WarehouseRow.id.in_(warehouse_ids_with_enough_stock))
            .order_by(WarehouseRow.id)
        )
        return [
            Warehouse(row.id, row.name, Coordinates(row.latitude, row.longitude))
            for row in warehouse_rows.scalars()
        ]

    def reserve_stock(self, warehouse_id: int, items: Sequence[OrderItem]) -> bool:
        reserved_product_ids = self._session.execute(_build_stock_reservation(warehouse_id, items))
        return len(reserved_product_ids.all()) == len(items)

    def release_stock(self, warehouse_id: int, items: Sequence[OrderItem]) -> None:
        released_product_ids = self._session.execute(_build_stock_release(warehouse_id, items))
        if len(released_product_ids.all()) != len(items):
            raise RuntimeError(f"stock row missing in warehouse {warehouse_id}")


def _requested_quantities_table(product_quantity_pairs):
    """A small inline table of (product_id, quantity) rows, to join the stock against."""
    return values(
        column("product_id", BigInteger), column("quantity", Integer), name="requested_quantities"
    ).data(list(product_quantity_pairs))


def _lock_stock_rows_in_product_order(warehouse_id: int, product_ids: list[int]) -> CTE:
    """Lock the stock rows of these products, always in product order.

    Two transactions that touch overlapping products therefore always wait for each other in the
    same order, which makes a deadlock between them impossible.
    """
    return (
        select(StockRow.product_id)
        .where(StockRow.warehouse_id == warehouse_id, StockRow.product_id.in_(product_ids))
        .order_by(StockRow.product_id)
        .with_for_update()
        .cte("locked_stock_rows")
    )


def _build_stock_reservation(warehouse_id: int, items: Sequence[OrderItem]):
    """ONE statement that takes the quantities of all the items from the warehouse.

    A row that does not have enough stock is left out of the update, so the caller sees fewer
    rows than items and rolls back: a short item never leaves a partial reservation.
    """
    items_by_product_id = _sorted_by_product_id(items)
    product_ids = [item.product_id for item in items_by_product_id]
    requested_quantities = _requested_quantities_table(
        (item.product_id, item.quantity) for item in items_by_product_id
    )
    locked_stock_rows = _lock_stock_rows_in_product_order(warehouse_id, product_ids)
    return (
        update(StockRow)
        .where(
            StockRow.warehouse_id == warehouse_id,
            StockRow.product_id == locked_stock_rows.c.product_id,
            StockRow.product_id == requested_quantities.c.product_id,
            StockRow.quantity >= requested_quantities.c.quantity,
        )
        .values(quantity=StockRow.quantity - requested_quantities.c.quantity)
        .returning(StockRow.product_id)
    )


def _build_stock_release(warehouse_id: int, items: Sequence[OrderItem]):
    """ONE statement that gives the quantities of all the items back to the warehouse."""
    items_by_product_id = _sorted_by_product_id(items)
    product_ids = [item.product_id for item in items_by_product_id]
    requested_quantities = _requested_quantities_table(
        (item.product_id, item.quantity) for item in items_by_product_id
    )
    locked_stock_rows = _lock_stock_rows_in_product_order(warehouse_id, product_ids)
    return (
        update(StockRow)
        .where(
            StockRow.warehouse_id == warehouse_id,
            StockRow.product_id == locked_stock_rows.c.product_id,
            StockRow.product_id == requested_quantities.c.product_id,
        )
        .values(quantity=StockRow.quantity + requested_quantities.c.quantity)
        .returning(StockRow.product_id)
    )


def _sorted_by_product_id(items: Sequence[OrderItem]) -> list[OrderItem]:
    items_by_product_id = sorted(items, key=lambda item: item.product_id)
    product_ids = [item.product_id for item in items_by_product_id]
    if len(set(product_ids)) != len(product_ids):
        raise ValueError("a product can only appear once")
    return items_by_product_id


class SqlOrderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_by_id(self, order_id: UUID) -> Order | None:
        orders = self._load_orders_where(OrderRow.id == order_id)
        return orders[0] if orders else None

    def find_by_idempotency_key(self, idempotency_key: UUID) -> Order | None:
        orders = self._load_orders_where(OrderRow.idempotency_key == idempotency_key)
        return orders[0] if orders else None

    def add(self, order: Order) -> None:
        self._insert_order(order)
        self._insert_order_items(order)
        self._insert_status_history(
            order.id,
            previous_status=None,
            new_status=order.status,
            detail="created",
            happened_at=order.created_at,
        )

    def _insert_order(self, order: Order) -> None:
        try:
            self._session.execute(
                insert(OrderRow).values(
                    id=order.id,
                    customer_id=order.customer_id,
                    idempotency_key=order.idempotency_key,
                    request_hash=order.request_hash,
                    shipping_address=order.shipping_address,
                    shipping_latitude=order.shipping_location.latitude,
                    shipping_longitude=order.shipping_location.longitude,
                    warehouse_id=order.warehouse_id,
                    status=order.status.value,
                    total_cents=order.total.cents,
                    payment_id=order.payment_id,
                    failure_code=order.failure_code,
                    expires_at=order.expires_at,
                    created_at=order.created_at,
                    updated_at=order.created_at,
                )
            )
        except IntegrityError as error:
            if error.orig.diag.constraint_name == "uq_orders_idempotency_key":
                raise DuplicateIdempotencyKey(str(order.idempotency_key)) from error
            raise

    def _insert_order_items(self, order: Order) -> None:
        self._session.execute(
            insert(OrderItemRow),
            [
                {
                    "order_id": order.id,
                    "product_id": item.product_id,
                    "quantity": item.quantity,
                    "unit_price_cents": item.unit_price.cents,
                }
                for item in order.items
            ],
        )

    def _insert_status_history(
        self,
        order_id: UUID,
        *,
        previous_status: OrderStatus | None,
        new_status: OrderStatus,
        detail: str | None,
        happened_at: datetime,
    ) -> None:
        self._session.execute(
            insert(OrderEventRow).values(
                order_id=order_id,
                from_status=previous_status.value if previous_status else None,
                to_status=new_status.value,
                detail=detail,
                created_at=happened_at,
            )
        )

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
        ensure_can_transition(expected_statuses, new_status)

        # Decide under a row lock, so concurrent changes of the same order are serialized. The
        # lock is held until the transaction ends, which also makes the update below safe.
        current_status = self._session.scalar(
            select(OrderRow.status).where(OrderRow.id == order_id).with_for_update()
        )
        if current_status is None or OrderStatus(current_status) not in expected_statuses:
            return False

        self._update_status_and_record_history(
            order_id,
            previous_status=current_status,
            new_status=new_status,
            changed_at=changed_at,
            payment_id=payment_id,
            failure_code=failure_code,
        )
        return True

    def _update_status_and_record_history(
        self,
        order_id: UUID,
        *,
        previous_status: str,
        new_status: OrderStatus,
        changed_at: datetime,
        payment_id: str | None,
        failure_code: str | None,
    ) -> None:
        """Update the order and add its history row, in a single round trip."""
        changed_columns: dict[str, object] = {"status": new_status.value, "updated_at": changed_at}
        if payment_id is not None:
            changed_columns["payment_id"] = payment_id
        if failure_code is not None:
            changed_columns["failure_code"] = failure_code
        history_detail = ", ".join(
            f"{name}={value}"
            for name, value in (("payment_id", payment_id), ("failure_code", failure_code))
            if value is not None
        )

        updated_order = (
            update(OrderRow)
            .where(OrderRow.id == order_id)
            .values(**changed_columns)
            .returning(OrderRow.id)
            .cte("updated_order")
        )
        self._session.execute(
            insert(OrderEventRow).from_select(
                ["order_id", "from_status", "to_status", "detail", "created_at"],
                select(
                    updated_order.c.id,
                    literal(previous_status, String),
                    literal(new_status.value, String),
                    literal(history_detail or None, Text),
                    literal(changed_at, DateTime(timezone=True)),
                ),
            )
        )

    def claim_authorized_orders_idle_since(
        self, *, idle_since: datetime, now: datetime, lease_duration: timedelta, max_orders: int
    ) -> list[Order]:
        candidate_order_ids = (
            select(OrderRow.id)
            .where(
                OrderRow.status == OrderStatus.AUTHORIZED.value,
                OrderRow.updated_at <= idle_since,
                _not_claimed_by_anyone_at(now),
            )
            .order_by(OrderRow.updated_at)
            .limit(max_orders)
            .with_for_update(skip_locked=True)
            .cte("candidate_order_ids")
        )
        return self._claim_orders(candidate_order_ids, lease_until=now + lease_duration)

    def claim_overdue_orders(
        self, *, now: datetime, lease_duration: timedelta, max_orders: int
    ) -> list[Order]:
        candidate_order_ids = (
            select(OrderRow.id)
            .where(
                OrderRow.status.in_([status.value for status in STATUSES_AWAITING_AUTHORIZATION]),
                OrderRow.expires_at <= now,
                _not_claimed_by_anyone_at(now),
            )
            .order_by(OrderRow.expires_at)
            .limit(max_orders)
            .with_for_update(skip_locked=True)
            .cte("candidate_order_ids")
        )
        return self._claim_orders(candidate_order_ids, lease_until=now + lease_duration)

    def _claim_orders(self, candidate_order_ids: CTE, *, lease_until: datetime) -> list[Order]:
        """Two statements for the whole batch: take the lease, then load the orders with items.

        FOR UPDATE SKIP LOCKED makes concurrent callers get different orders instead of waiting
        for each other.
        """
        claimed_order_ids = list(
            self._session.scalars(
                update(OrderRow)
                .where(OrderRow.id == candidate_order_ids.c.id)
                .values(claimed_until=lease_until)
                .returning(OrderRow.id)
            )
        )
        if not claimed_order_ids:
            return []
        return self._load_orders_where(OrderRow.id.in_(claimed_order_ids))

    def _load_orders_where(self, condition) -> list[Order]:
        """Load the orders with their items in ONE query (a left join), however many there are."""
        rows = self._session.execute(
            select(OrderRow, OrderItemRow)
            .outerjoin(OrderItemRow, OrderItemRow.order_id == OrderRow.id)
            .where(condition)
            .order_by(OrderRow.created_at, OrderRow.id, OrderItemRow.product_id)
        )
        item_rows_by_order_id: dict[UUID, tuple[OrderRow, list[OrderItemRow]]] = {}
        for order_row, item_row in rows:
            _, item_rows = item_rows_by_order_id.setdefault(order_row.id, (order_row, []))
            if item_row is not None:
                item_rows.append(item_row)
        return [
            _order_from_rows(order_row, item_rows)
            for order_row, item_rows in item_rows_by_order_id.values()
        ]


def _not_claimed_by_anyone_at(now: datetime):
    return or_(OrderRow.claimed_until.is_(None), OrderRow.claimed_until <= now)


def _order_from_rows(order_row: OrderRow, item_rows: list[OrderItemRow]) -> Order:
    return Order(
        id=order_row.id,
        customer_id=order_row.customer_id,
        idempotency_key=order_row.idempotency_key,
        request_hash=order_row.request_hash,
        shipping_address=order_row.shipping_address,
        shipping_location=Coordinates(order_row.shipping_latitude, order_row.shipping_longitude),
        warehouse_id=order_row.warehouse_id,
        items=tuple(
            OrderItem(item_row.product_id, item_row.quantity, Money(item_row.unit_price_cents))
            for item_row in item_rows
        ),
        total=Money(order_row.total_cents),
        status=OrderStatus(order_row.status),
        expires_at=order_row.expires_at,
        created_at=order_row.created_at,
        payment_id=order_row.payment_id,
        failure_code=order_row.failure_code,
    )
