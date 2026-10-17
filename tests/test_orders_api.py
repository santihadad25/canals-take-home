"""POST /orders end to end: real API, real Postgres, real HTTP to the mock providers."""

import contextlib
import logging
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.adapters.persistence.repositories import SqlWarehouseRepository
from app.domain.models import Coordinates, Warehouse
from tests.support import (
    BUENOS_AIRES,
    CORDOBA,
    MENDOZA,
    MONITOR,
    MOUSE,
    NOTEBOOK,
    ROSARIO,
    WEBCAM,
    Addresses,
    Cards,
)


def items(*pairs: tuple[int, int]) -> list[dict]:
    return [{"product_id": product, "quantity": quantity} for product, quantity in pairs]


@contextlib.contextmanager
def provider_unreachable(system):
    """Point the payment client at a closed port: every request fails before it is sent."""
    client = system.payments_client
    original_url = client.base_url
    client.base_url = "http://127.0.0.1:1"
    try:
        yield
    finally:
        client.base_url = original_url


def test_places_and_pays_an_order(system):
    response = system.place_order(items=items((NOTEBOOK, 1), (MOUSE, 2)), address=Addresses.ROSARIO)

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["status"] == "PAID"
    assert order["warehouse_id"] == ROSARIO
    assert order["total_cents"] == 129_900 + 2 * 2_500
    assert order["items"] == [
        {"product_id": NOTEBOOK, "quantity": 1, "unit_price_cents": 129_900},
        {"product_id": MOUSE, "quantity": 2, "unit_price_cents": 2_500},
    ]
    assert system.stock(ROSARIO, NOTEBOOK) == 2
    assert system.stock(ROSARIO, MOUSE) == 18

    payment = system.provider_payment(order["payment_id"])
    assert payment["status"] == "captured"
    assert payment["amount_cents"] == 134_900
    assert payment["description"] == f"Order {order['id']}"
    assert system.events(order["id"]) == [
        (None, "PENDING"),
        ("PENDING", "AUTHORIZED"),
        ("AUTHORIZED", "PAID"),
    ]


@pytest.mark.parametrize(
    "wanted, address, expected_warehouse",
    [
        # Every warehouse that has it is a candidate; the closest one wins.
        (items((NOTEBOOK, 1)), Addresses.ROSARIO, ROSARIO),
        (items((NOTEBOOK, 1)), Addresses.BUENOS_AIRES, BUENOS_AIRES),
        (items((WEBCAM, 1)), Addresses.MENDOZA, MENDOZA),
        (items((WEBCAM, 1)), Addresses.CORDOBA, CORDOBA),
        # Only Córdoba and Mendoza have webcams: Buenos Aires is closer to Córdoba.
        (items((WEBCAM, 1)), Addresses.BUENOS_AIRES, CORDOBA),
        # Rosario is the closest but has no webcam, so it is not a candidate at all.
        (items((NOTEBOOK, 1), (WEBCAM, 1)), Addresses.ROSARIO, CORDOBA),
        # A warehouse short on a single item is out, even if it has the rest.
        (items((NOTEBOOK, 4)), Addresses.ROSARIO, BUENOS_AIRES),
    ],
)
def test_picks_the_closest_warehouse_that_has_everything(
    system, wanted, address, expected_warehouse
):
    response = system.place_order(items=wanted, address=address)
    assert response.status_code == 201, response.text
    assert response.json()["warehouse_id"] == expected_warehouse


def test_falls_back_to_the_next_closest_warehouse_when_the_stock_moved(system, monkeypatch):
    # The pre-check does not lock anything, so stock can change before the reservation. Simulate
    # exactly that: Rosario is empty but the pre-check still lists it.
    with system.engine.begin() as connection:
        connection.execute(
            text("UPDATE stock SET quantity = 0 WHERE warehouse_id = 3 AND product_id = 4")
        )

    def stale_precheck(self, requested):
        return [
            Warehouse(3, "Rosario", Coordinates(-32.9442, -60.6505)),
            Warehouse(1, "Buenos Aires", Coordinates(-34.6118, -58.4173)),
        ]

    monkeypatch.setattr(SqlWarehouseRepository, "find_warehouses_with_stock_for", stale_precheck)

    response = system.place_order(items=items((MONITOR, 1)), address=Addresses.ROSARIO)

    assert response.status_code == 201, response.text
    assert response.json()["warehouse_id"] == BUENOS_AIRES
    assert system.stock(ROSARIO, MONITOR) == 0
    assert system.stock(BUENOS_AIRES, MONITOR) == 4


def test_conflict_when_the_stock_moved_and_nothing_else_has_it(system, monkeypatch):
    with system.engine.begin() as connection:
        connection.execute(
            text("UPDATE stock SET quantity = 0 WHERE warehouse_id = 3 AND product_id = 4")
        )

    def stale_precheck(self, requested):
        return [Warehouse(3, "Rosario", Coordinates(-32.9442, -60.6505))]

    monkeypatch.setattr(SqlWarehouseRepository, "find_warehouses_with_stock_for", stale_precheck)

    response = system.place_order(items=items((MONITOR, 1)))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "out_of_stock"
    assert system.order_count() == 0  # the order insert was rolled back with the reservation
    assert system.payments.calls == []


@pytest.mark.parametrize("wanted", [items((MOUSE, 1_000)), items((NOTEBOOK, 1), (MOUSE, 1_000))])
def test_out_of_stock_fails_before_calling_anything_external(system, wanted):
    response = system.place_order(items=wanted)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "out_of_stock"
    assert system.geocoding.calls == []
    assert system.payments.calls == []
    assert system.order_count() == 0


def test_unknown_product(system):
    response = system.place_order(items=items((NOTEBOOK, 1), (999, 1)))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "product_not_found"
    assert "999" in response.json()["error"]["message"]
    assert system.payments.calls == []
    assert system.stock(ROSARIO, NOTEBOOK) == 3


def test_an_address_the_geocoder_does_not_know_is_rejected_before_reserving_anything(system):
    response = system.place_order(address=Addresses.UNKNOWN)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_address"
    assert system.order_count() == 0
    assert system.stock(ROSARIO, NOTEBOOK) == 3
    assert system.payments.calls == []


def test_an_address_is_validated_even_when_a_single_warehouse_qualifies(system):
    # Only Córdoba and Mendoza have webcams, but the address still has to be a real one.
    response = system.place_order(items=items((WEBCAM, 1)), address=Addresses.UNKNOWN)
    assert response.status_code == 422


def test_geocoding_outage_is_a_retryable_502_that_changes_nothing(system):
    response = system.place_order(address=Addresses.GEOCODER_DOWN)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "geocoding_unavailable"
    assert system.order_count() == 0
    assert system.stock(ROSARIO, NOTEBOOK) == 3


@pytest.mark.parametrize(
    "card, reason",
    [
        (Cards.DECLINED, "card_declined"),
        (Cards.INSUFFICIENT_FUNDS, "insufficient_funds"),
        (Cards.NOT_LUHN, "invalid_card_number"),  # the provider validates the card, not us
    ],
)
def test_a_rejected_card_fails_the_order_and_gives_the_stock_back(system, card, reason):
    response = system.place_order(card=card, items=items((NOTEBOOK, 2)))

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "payment_failed"
    assert error["reason"] == reason
    assert error["order_status"] == "PAYMENT_FAILED"
    assert system.stock(ROSARIO, NOTEBOOK) == 3  # reserved, then released
    assert system.order_status(error["order_id"]) == "PAYMENT_FAILED"
    assert system.events(error["order_id"]) == [
        (None, "PENDING"),
        ("PENDING", "PAYMENT_FAILED"),
    ]
    assert system.payments.count("POST", "/capture") == 0  # money never moved


def test_retrying_a_failed_order_gives_the_same_answer_without_calling_the_provider(system):
    key = uuid4()
    first = system.place_order(key=key, card=Cards.DECLINED)
    calls_before = list(system.payments.calls)

    again = system.place_order(key=key, card=Cards.DECLINED)

    assert again.status_code == first.status_code == 402
    assert again.json()["error"]["order_id"] == first.json()["error"]["order_id"]
    assert system.payments.calls == calls_before
    assert system.order_count() == 1
    assert system.stock(ROSARIO, NOTEBOOK) == 3


def test_retrying_a_paid_order_returns_it_without_charging_again(system):
    key = uuid4()
    first = system.place_order(key=key, items=items((NOTEBOOK, 1)))
    again = system.place_order(key=key, items=items((NOTEBOOK, 1)))

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json() == first.json()
    assert system.payments.count("POST", "/payments") == 1
    assert system.payments.count("POST", "/capture") == 1
    assert system.order_count() == 1
    assert system.stock(ROSARIO, NOTEBOOK) == 2


def test_an_idempotency_key_cannot_be_reused_for_a_different_request(system):
    key = uuid4()
    system.place_order(key=key, items=items((NOTEBOOK, 1)))

    other = system.place_order(key=key, items=items((NOTEBOOK, 2)))

    assert other.status_code == 422
    assert other.json()["error"]["code"] == "idempotency_key_reused"
    assert system.order_count() == 1


def test_the_same_request_with_a_new_key_is_a_new_order(system):
    system.place_order(items=items((NOTEBOOK, 1)))
    system.place_order(items=items((NOTEBOOK, 1)))
    assert system.order_count() == 2
    assert system.stock(ROSARIO, NOTEBOOK) == 1


@pytest.mark.parametrize("header", [None, "not-a-uuid", "12345"])
def test_the_idempotency_key_header_is_required_and_must_be_a_uuid(system, header):
    body = {
        "customer_id": "c",
        "shipping_address": Addresses.ROSARIO,
        "items": items((NOTEBOOK, 1)),
        "payment": {"card_number": Cards.OK},
    }
    headers = {} if header is None else {"Idempotency-Key": header}
    response = system.client.post("/orders", json=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert system.order_count() == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.update(items=[]),
        lambda body: body.update(items=items((NOTEBOOK, 1), (NOTEBOOK, 2))),
        lambda body: body.update(items=items((NOTEBOOK, 0))),
        lambda body: body.update(items=items((NOTEBOOK, -3))),
        lambda body: body.update(items=items((0, 1))),
        lambda body: body.update(customer_id="  "),
        lambda body: body.update(shipping_address=""),
        lambda body: body.pop("payment"),
        lambda body: body.update(payment={"card_number": ""}),
        lambda body: body.update(amount_cents=1),  # the server computes the amount
        lambda body: body["payment"].update(amount_cents=1),
    ],
)
def test_invalid_requests_are_rejected_without_echoing_the_card(system, mutation):
    body = {
        "customer_id": "customer-1",
        "shipping_address": Addresses.ROSARIO,
        "items": items((NOTEBOOK, 1)),
        "payment": {"card_number": Cards.OK},
    }
    mutation(body)

    response = system.client.post("/orders", json=body, headers={"Idempotency-Key": str(uuid4())})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert Cards.OK not in response.text
    assert system.order_count() == 0


def test_the_customer_id_is_an_opaque_reference_that_is_not_checked(system):
    response = system.place_order(customer_id="a-customer-from-another-system")

    assert response.status_code == 201
    assert response.json()["customer_id"] == "a-customer-from-another-system"
    with system.engine.connect() as connection:
        customers_table = connection.scalar(text("SELECT to_regclass('public.customers')"))
        stored = connection.scalar(text("SELECT customer_id FROM orders"))
    assert customers_table is None  # nothing to reference, deliberately
    assert stored == "a-customer-from-another-system"


def test_the_card_number_is_never_stored_or_logged(system, caplog):
    caplog.set_level(logging.DEBUG)
    system.place_order(card=Cards.OK)
    system.place_order(card=Cards.DECLINED)

    with system.engine.connect() as connection:
        for table in ("orders", "order_items", "order_events"):
            rows = connection.execute(text(f"SELECT t::text FROM {table} t")).scalars()
            for row in rows:
                assert Cards.OK not in row and Cards.DECLINED not in row
    assert Cards.OK not in caplog.text
    assert Cards.DECLINED not in caplog.text


def test_health(system):
    response = system.client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["pid"], int)  # which worker process answered


def test_a_provider_unreachable_from_the_start_fails_the_order_and_releases_the_stock(system):
    with provider_unreachable(system):
        response = system.place_order(items=items((MONITOR, 1)))

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "payment_provider_unavailable"
    assert system.order_status(error["order_id"]) == "PAYMENT_FAILED"
    assert system.stock(ROSARIO, MONITOR) == 2  # nothing could have been authorized


def test_a_retry_that_cannot_reach_the_provider_leaves_an_uncertain_order_uncertain(system):
    key = uuid4()
    first = system.place_order(key=key, card=Cards.AUTHORIZE_SLOW, items=items((MONITOR, 1)))
    assert first.status_code == 202
    order_id = first.json()["id"]
    assert system.order_status(order_id) == "PAYMENT_UNKNOWN"

    # The first attempt may well have been authorized (its answer was only late), so a retry that
    # fails before being sent proves nothing: the order must not be failed, nor its stock released.
    with provider_unreachable(system):
        retry = system.place_order(key=key, card=Cards.AUTHORIZE_SLOW, items=items((MONITOR, 1)))

    assert retry.status_code == 202
    assert retry.json()["status"] == "PAYMENT_UNKNOWN"
    assert system.order_status(order_id) == "PAYMENT_UNKNOWN"
    assert system.stock(ROSARIO, MONITOR) == 1
    assert system.events(order_id) == [(None, "PENDING"), ("PENDING", "PAYMENT_UNKNOWN")]

    # Once the provider is back, the same key recovers the original authorization.
    recovered = system.place_order(key=key, card=Cards.AUTHORIZE_SLOW, items=items((MONITOR, 1)))
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "PAID"
    assert system.stock(ROSARIO, MONITOR) == 1  # sold
