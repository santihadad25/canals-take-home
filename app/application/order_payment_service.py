"""The payment steps of an order's life: authorize, capture, and getting out of doubt.

Rules that keep money and stock consistent (see the README for the full picture):

* Money only moves on capture, and a capture only happens for an AUTHORIZED order, which by then
  already holds its stock. A charged order therefore always has its stock.
* Stock is released only together with a final "not paid" status, and only when nobody can capture
  anymore: there is no payment id, or the provider says the authorization is gone.
* An AUTHORIZED order never expires. Its funds are held and the sale is accepted, so its capture is
  retried until it goes through (or the provider says the authorization is gone, which it does by
  itself after some days). The deadline only applies to orders that never got authorized.
* Every status change is a compare-and-set, so concurrent actors (the request that is still
  running, a client retry, the reconciler) cannot overwrite each other. Only the winner of a status
  change releases the stock, so it is never released twice.
"""

import logging
from collections.abc import Collection
from dataclasses import replace

from app.application.errors import PaymentNotSent, PaymentOutcomeUnknown, PaymentRejected
from app.application.ports import Clock, PaymentGateway, PaymentStatus, UnitOfWorkFactory
from app.domain.models import STATUSES_AWAITING_AUTHORIZATION, Order, OrderStatus

logger = logging.getLogger(__name__)

# Failure codes that are decided by us. The other failure codes come from the payment provider.
FAILURE_CODE_PROVIDER_UNAVAILABLE = "payment_provider_unavailable"
FAILURE_CODE_AUTHORIZATION_CANCELED = "authorization_canceled"


class OrderPaymentService:
    def __init__(
        self, *, new_unit_of_work: UnitOfWorkFactory, payment_gateway: PaymentGateway, clock: Clock
    ) -> None:
        self._new_unit_of_work = new_unit_of_work
        self._payment_gateway = payment_gateway
        self._clock = clock

    def authorize(self, order: Order, card_number: str) -> Order:
        """Ask the provider to hold the funds. Returns the order as it is stored afterwards.

        PENDING or PAYMENT_UNKNOWN becomes AUTHORIZED, PAYMENT_FAILED or PAYMENT_UNKNOWN.
        """
        try:
            payment_id = self._payment_gateway.authorize(
                idempotency_key=_idempotency_key(order, "authorize"),
                amount=order.total,
                description=f"Order {order.id}",
                card_number=card_number,
            )
        except PaymentRejected as rejection:
            return self._change_status(
                order,
                expected_statuses=STATUSES_AWAITING_AUTHORIZATION,
                new_status=OrderStatus.PAYMENT_FAILED,
                failure_code=rejection.code,
            )
        except PaymentNotSent:
            # This request never reached the provider. Only a PENDING order can be failed for it:
            # a PAYMENT_UNKNOWN one may hold an authorization from an earlier attempt, so it stays
            # as it is until a retry resolves it or the deadline expires it.
            return self._change_status(
                order,
                expected_statuses={OrderStatus.PENDING},
                new_status=OrderStatus.PAYMENT_FAILED,
                failure_code=FAILURE_CODE_PROVIDER_UNAVAILABLE,
            )
        except PaymentOutcomeUnknown:
            # Maybe authorized, maybe not. Keep the stock: a client retry (same idempotency key)
            # resumes from here, and the reconciler expires the order if nobody comes back.
            return self._change_status(
                order,
                expected_statuses={OrderStatus.PENDING},
                new_status=OrderStatus.PAYMENT_UNKNOWN,
            )

        return self._record_authorization(order, payment_id)

    def capture(self, order: Order) -> Order:
        """Charge an AUTHORIZED order.

        It becomes PAID, or PAYMENT_FAILED if the authorization is gone. If the outcome is
        unclear, the order stays AUTHORIZED and the reconciler captures it later, using only the
        payment id.
        """
        assert order.payment_id is not None
        try:
            payment_status = self._payment_gateway.capture(
                payment_id=order.payment_id, idempotency_key=_idempotency_key(order, "capture")
            )
        except (PaymentNotSent, PaymentOutcomeUnknown):
            return order
        return self._apply_provider_status(order, payment_status)

    def finish_authorized_order(self, order: Order) -> Order:
        """Finish an AUTHORIZED order that nobody finished by capturing its payment.

        It does not expire, so this is retried on every reconciler run until the payment is
        captured, or the provider says the authorization is gone.
        """
        assert order.payment_id is not None
        try:
            payment_status = self._payment_gateway.get_status(order.payment_id)
        except (PaymentNotSent, PaymentOutcomeUnknown):
            self._warn_if_payment_is_taking_too_long(
                order, "the payment provider could not be asked"
            )
            return order

        if payment_status is not PaymentStatus.AUTHORIZED:
            return self._apply_provider_status(order, payment_status)

        order_after_capture = self.capture(order)
        if order_after_capture.status is OrderStatus.AUTHORIZED:
            self._warn_if_payment_is_taking_too_long(order, "the capture did not go through")
        return order_after_capture

    def expire_unauthorized_order(self, order: Order) -> Order:
        """PENDING or PAYMENT_UNKNOWN becomes EXPIRED, and the stock goes back to the warehouse.

        It is safe without asking the provider: with no payment id nobody can capture, and any
        authorization that exists lapses by itself. If a request gets a payment id after this, its
        own status change fails and it cancels that authorization.
        """
        return self._change_status(
            order,
            expected_statuses=STATUSES_AWAITING_AUTHORIZATION,
            new_status=OrderStatus.EXPIRED,
        )

    def _record_authorization(self, order: Order, payment_id: str) -> Order:
        """Save that the provider authorized the payment, and keep that authorization only if the
        order still wants it."""
        stored_order = self._change_status(
            order,
            expected_statuses=STATUSES_AWAITING_AUTHORIZATION,
            new_status=OrderStatus.AUTHORIZED,
            payment_id=payment_id,
        )
        authorization_was_recorded = stored_order.payment_id == payment_id and (
            stored_order.status in (OrderStatus.AUTHORIZED, OrderStatus.PAID)
        )
        if not authorization_was_recorded:
            # We lost a race (for example the order expired while the call was in flight). The
            # hold we just got must not outlive an order that no longer wants it.
            self._cancel_unwanted_authorization(order, payment_id)
        return stored_order

    def _warn_if_payment_is_taking_too_long(self, order: Order, reason: str) -> None:
        """An AUTHORIZED order that cannot be captured keeps its stock. That is the one case where
        stock stays reserved for long, so once the order is past the time it should have been
        paid, say so."""
        if self._clock() >= order.expires_at:
            logger.warning(
                "order %s has been AUTHORIZED past the time it should have been paid (%s): its "
                "stock stays held and the capture is retried",
                order.id,
                reason,
            )

    def _apply_provider_status(self, order: Order, payment_status: PaymentStatus) -> Order:
        """Bring an AUTHORIZED order in line with what the provider says about its payment."""
        if payment_status is PaymentStatus.CAPTURED:
            stored_order = self._change_status(
                order,
                expected_statuses={OrderStatus.AUTHORIZED},
                new_status=OrderStatus.PAID,
            )
            if stored_order.status is not OrderStatus.PAID:
                logger.critical(
                    "payment %s was captured but order %s is %s: manual refund required",
                    order.payment_id,
                    order.id,
                    stored_order.status.value,
                )
            return stored_order

        if payment_status is PaymentStatus.CANCELED:
            return self._change_status(
                order,
                expected_statuses={OrderStatus.AUTHORIZED},
                new_status=OrderStatus.PAYMENT_FAILED,
                failure_code=FAILURE_CODE_AUTHORIZATION_CANCELED,
            )

        return order  # still only authorized: nothing to change

    def _cancel_unwanted_authorization(self, order: Order, payment_id: str) -> None:
        try:
            self._payment_gateway.cancel(
                payment_id=payment_id, idempotency_key=_idempotency_key(order, "cancel")
            )
        except (PaymentNotSent, PaymentOutcomeUnknown):
            logger.warning(
                "could not cancel authorization %s of order %s; it will lapse by itself",
                payment_id,
                order.id,
            )

    def _change_status(
        self,
        order: Order,
        *,
        expected_statuses: Collection[OrderStatus],
        new_status: OrderStatus,
        payment_id: str | None = None,
        failure_code: str | None = None,
    ) -> Order:
        """Change the status if the order still has one of the expected ones.

        In the same transaction, the stock goes back to the warehouse if the new status asks for
        it. Returns the order as it is stored: updated if we changed it, or as somebody else left
        it if we lost the race.
        """
        changed_at = self._clock()  # read before the transaction: it may query the database
        with self._new_unit_of_work() as unit_of_work:
            status_was_changed = unit_of_work.orders.try_change_status(
                order.id,
                expected_statuses=expected_statuses,
                new_status=new_status,
                changed_at=changed_at,
                payment_id=payment_id,
                failure_code=failure_code,
            )
            if status_was_changed and new_status.releases_stock:
                unit_of_work.warehouses.release_stock(order.warehouse_id, order.items)
            unit_of_work.commit()

            if not status_was_changed:
                order_as_stored_by_someone_else = unit_of_work.orders.find_by_id(order.id)
                assert order_as_stored_by_someone_else is not None
                logger.info(
                    "order %s: could not change it to %s, it is %s",
                    order.id,
                    new_status.value,
                    order_as_stored_by_someone_else.status.value,
                )
                return order_as_stored_by_someone_else

        logger.info("order %s -> %s", order.id, new_status.value)
        return replace(
            order,
            status=new_status,
            payment_id=payment_id or order.payment_id,
            failure_code=failure_code or order.failure_code,
        )


def _idempotency_key(order: Order, operation: str) -> str:
    """The key sent to the payment provider. Derived from the order id, so every retry of the same
    operation (by the request, a client retry or the reconciler) repeats the same key."""
    return f"{order.id}:{operation}"
