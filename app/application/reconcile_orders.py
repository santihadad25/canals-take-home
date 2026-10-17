import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from app.application.order_payment_service import OrderPaymentService
from app.application.ports import Clock, UnitOfWorkFactory
from app.domain.models import Order

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    authorized_orders_processed: int = 0
    orders_expired: int = 0
    failures: int = 0


class ReconcileOrders:
    """Finishes the orders that were left halfway, without anyone having to come back.

    * AUTHORIZED orders that nobody finished: ask the provider about the payment (by payment id,
      no card needed) and capture it. They never expire: the capture is retried on every run until
      it goes through, or until the provider says the authorization is gone.
    * Orders that never got authorized and are past their deadline: expire them and give their
      stock back.

    It is safe to run several instances at once. Each run first *claims* a batch of orders with a
    short lease, so the instances get different orders and do not repeat each other's calls to
    the provider. If an instance dies, its lease runs out and the orders are picked up again.
    Within a run, the orders of the batch are processed concurrently (in threads), because each one
    mostly waits for the payment provider.
    """

    def __init__(
        self,
        *,
        new_unit_of_work: UnitOfWorkFactory,
        payment_service: OrderPaymentService,
        clock: Clock,
        authorized_grace_period: timedelta,
        batch_size: int,
        lease_duration: timedelta,
        max_concurrent_orders: int,
    ) -> None:
        self._new_unit_of_work = new_unit_of_work
        self._payment_service = payment_service
        self._clock = clock
        self._authorized_grace_period = authorized_grace_period
        self._batch_size = batch_size
        self._lease_duration = lease_duration
        self._max_concurrent_orders = max(1, max_concurrent_orders)

    def run_once(self) -> ReconcileReport:
        overdue_orders, authorized_orders = self._claim_orders_to_process()

        with ThreadPoolExecutor(max_workers=self._max_concurrent_orders) as executor:
            # Expiring orders only touches the database and gives stock back, so it goes first.
            # All the orders are submitted together, so a slow one does not hold back the others.
            expirations = [
                executor.submit(
                    self._process, order, self._payment_service.expire_unauthorized_order
                )
                for order in overdue_orders
            ]
            completions = [
                executor.submit(self._process, order, self._payment_service.finish_authorized_order)
                for order in authorized_orders
            ]
            expiration_results = [future.result() for future in expirations]
            completion_results = [future.result() for future in completions]

        return ReconcileReport(
            authorized_orders_processed=completion_results.count(True),
            orders_expired=expiration_results.count(True),
            failures=expiration_results.count(False) + completion_results.count(False),
        )

    def _claim_orders_to_process(self) -> tuple[list[Order], list[Order]]:
        now = self._clock()
        with self._new_unit_of_work() as unit_of_work:
            overdue_orders = unit_of_work.orders.claim_overdue_orders(
                now=now, lease_duration=self._lease_duration, max_orders=self._batch_size
            )
            authorized_orders = unit_of_work.orders.claim_authorized_orders_idle_since(
                idle_since=now - self._authorized_grace_period,
                now=now,
                lease_duration=self._lease_duration,
                max_orders=self._batch_size,
            )
            unit_of_work.commit()  # a claim only counts once it is committed
        return overdue_orders, authorized_orders

    @staticmethod
    def _process(order: Order, action: Callable[[Order], Order]) -> bool:
        """Apply the action to the order. Returns whether it worked.

        One order that fails must not stop the others: its lease runs out and it is retried.
        """
        try:
            action(order)
        except Exception:
            logger.exception("reconciling order %s failed", order.id)
            return False
        return True
