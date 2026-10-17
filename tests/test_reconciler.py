"""The reconciler: it finishes the orders that were left halfway."""

import logging
from uuid import uuid4

import pytest

from tests.support import MONITOR, ROSARIO, Cards

TTL = 900  # seconds, see the `system` fixture


def monitor(quantity: int = 1) -> list[dict]:
    return [{"product_id": MONITOR, "quantity": quantity}]


@pytest.mark.parametrize("card", [Cards.AUTHORIZE_NEVER_ANSWERS, Cards.PROVIDER_DOWN])
def test_an_order_nobody_can_resolve_expires_and_gives_the_stock_back(system, card):
    key = uuid4()
    first = system.place_order(key=key, card=card, items=monitor())
    order_id = first.json()["id"]
    assert first.status_code == 202
    assert system.stock(ROSARIO, MONITOR) == 1

    # Before its deadline the reconciler leaves it alone: the client may still come back.
    report = system.reconcile()
    assert report.orders_expired == 0
    assert system.order_status(order_id) == "PAYMENT_UNKNOWN"
    assert system.stock(ROSARIO, MONITOR) == 1

    system.clock.advance(TTL + 1)
    report = system.reconcile()

    assert report.orders_expired == 1
    assert system.order_status(order_id) == "EXPIRED"
    assert system.stock(ROSARIO, MONITOR) == 2
    assert system.payments.count("POST", "/capture") == 0  # no payment id, so nothing to capture

    calls_before = list(system.payments.calls)
    retry = system.place_order(key=key, card=card, items=monitor())
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "order_expired"
    assert system.payments.calls == calls_before
    assert system.stock(ROSARIO, MONITOR) == 2


def test_a_second_run_of_the_reconciler_does_not_release_the_stock_twice(system):
    first = system.place_order(card=Cards.PROVIDER_DOWN, items=monitor())
    system.clock.advance(TTL + 1)

    system.reconcile()
    second = system.reconcile()

    assert second.orders_expired == 0
    assert system.stock(ROSARIO, MONITOR) == 2
    assert system.order_status(first.json()["id"]) == "EXPIRED"


def test_capture_timeout_leaves_the_order_authorized_and_the_reconciler_finishes_it(system):
    first = system.place_order(card=Cards.CAPTURE_SLOW, items=monitor())

    assert first.status_code == 202
    order = first.json()
    assert order["status"] == "AUTHORIZED"
    assert order["payment_id"]
    assert system.stock(ROSARIO, MONITOR) == 1

    # Too soon: the request that created the order may still be working on it.
    assert system.reconcile().authorized_orders_processed == 0
    assert system.order_status(order["id"]) == "AUTHORIZED"

    system.clock.advance(61)
    report = system.reconcile()

    assert report.authorized_orders_processed == 1
    assert system.order_status(order["id"]) == "PAID"
    assert system.provider_payment(order["payment_id"])["status"] == "captured"
    assert system.stock(ROSARIO, MONITOR) == 1  # sold


def test_a_rejected_capture_fails_the_order_and_releases_the_stock(system):
    response = system.place_order(card=Cards.CAPTURE_REJECTED, items=monitor())

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "payment_failed"
    assert error["reason"] == "authorization_canceled"
    assert system.order_status(error["order_id"]) == "PAYMENT_FAILED"
    assert system.stock(ROSARIO, MONITOR) == 2


def test_an_authorized_order_never_expires_and_its_capture_keeps_being_retried(system, caplog):
    first = system.place_order(card=Cards.CAPTURE_UNAVAILABLE, items=monitor())
    order = first.json()
    assert first.status_code == 202 and order["status"] == "AUTHORIZED"
    caplog.set_level(logging.WARNING)

    attempts = []
    for _ in range(3):
        system.clock.advance(TTL)  # 15 minutes at a time: far past the deadline
        system.reconcile()
        attempts.append(system.payments.count("POST", "/capture"))
        assert system.order_status(order["id"]) == "AUTHORIZED"  # still alive...
        assert system.stock(ROSARIO, MONITOR) == 1  # ...with its stock

    assert attempts[0] < attempts[1] < attempts[2]  # the capture is tried on every run
    assert system.payments.count("POST", "/cancel") == 0  # and it is never canceled
    assert "has been AUTHORIZED past the time it should have been paid" in caplog.text
