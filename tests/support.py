"""Test helpers: well-known cards and addresses, a fake clock and a handle on the running system."""

import contextlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from flask import Flask
from sqlalchemy import Engine, text
from werkzeug.serving import make_server

from app.config import Settings
from app.container import Container, build_container
from app.main import create_app


class Cards:
    """Card numbers whose behavior is defined in mocks/payments/scenarios.json."""

    OK = "4242424242424242"
    DECLINED = "4000000000000002"
    INSUFFICIENT_FUNDS = "4000000000009995"
    AUTHORIZE_SLOW = "4000000000000101"
    AUTHORIZE_UNAVAILABLE = "4000000000000200"
    AUTHORIZE_NEVER_ANSWERS = "4000000000000309"
    PROVIDER_DOWN = "4000000000000408"
    CAPTURE_SLOW = "4000000000000507"
    CAPTURE_REJECTED = "4000000000000606"
    CAPTURE_UNAVAILABLE = "4000000000000705"
    NOT_LUHN = "4000000000000001"


class Addresses:
    """Addresses known to mocks/geocoding/addresses.json."""

    BUENOS_AIRES = "Av. Corrientes 1234, Buenos Aires"
    CORDOBA = "Bv. San Juan 450, Córdoba"
    ROSARIO = "Bv. Oroño 1000, Rosario"
    MENDOZA = "Av. San Martín 800, Mendoza"
    UNKNOWN = "Calle Inexistente 999, Nowhere"
    GEOCODER_DOWN = "Calle Sin Servicio 1, Buenos Aires"


# Demo data (db/init/02-demo-data.sql): products 1 notebook, 2 mouse, 3 keyboard, 4 monitor,
# 5 webcam.
# Warehouses: 1 Buenos Aires, 2 Córdoba, 3 Rosario, 4 Mendoza.
NOTEBOOK, MOUSE, KEYBOARD, MONITOR, WEBCAM = 1, 2, 3, 4, 5
BUENOS_AIRES, CORDOBA, ROSARIO, MENDOZA = 1, 2, 3, 4


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        """The same time as seconds, for the circuit breakers."""
        return self.now.timestamp()


class RecordingTransport(httpx.BaseTransport):
    """A real HTTP transport that remembers what was called."""

    def __init__(self) -> None:
        self._inner = httpx.HTTPTransport()
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.calls.append((request.method, request.url.path))
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()

    def count(self, method: str, path_suffix: str) -> int:
        return sum(
            1
            for called_method, called_path in list(self.calls)
            if called_method == method and called_path.endswith(path_suffix)
        )


@contextlib.contextmanager
def serve(app) -> Iterator[str]:
    """Run a WSGI app on a real socket (so client timeouts are real), in threads, and yield its
    base URL."""
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def warm_up(engine: Engine) -> None:
    """Open all the connections of the pool, so that threads that start together find one ready.

    On a cold pool the first thread is done before the others have even connected, and a race that
    a busy server has all the time would never happen in a test.
    """
    connections = [engine.connect() for _ in range(engine.pool.size())]
    for connection in connections:
        connection.close()


@dataclass
class System:
    container: Container
    app: Flask
    client: httpx.Client  # the public API, called in-process
    engine: Engine
    clock: FakeClock
    geocoding: RecordingTransport
    payments: RecordingTransport
    payments_url: str
    payments_client: httpx.Client

    def place_order(
        self,
        *,
        key: UUID | None = None,
        customer_id: str = "customer-1",
        address: str = Addresses.ROSARIO,
        items: list[dict] | None = None,
        card: str = Cards.OK,
    ) -> httpx.Response:
        body = {
            "customer_id": customer_id,
            "shipping_address": address,
            "items": items or [{"product_id": NOTEBOOK, "quantity": 1}],
            "payment": {"card_number": card},
        }
        return self.client.post(
            "/orders", json=body, headers={"Idempotency-Key": str(key or uuid4())}
        )

    def reconcile(self):
        return self.container.reconcile_orders.run_once()

    def stock(self, warehouse_id: int, product_id: int) -> int:
        with self.engine.connect() as connection:
            result = connection.execute(
                text(
                    "SELECT quantity FROM stock "
                    "WHERE warehouse_id = :warehouse_id AND product_id = :product_id"
                ),
                {"warehouse_id": warehouse_id, "product_id": product_id},
            )
            return result.scalar_one()

    def order_status(self, order_id: str) -> str:
        with self.engine.connect() as connection:
            result = connection.execute(
                text("SELECT status FROM orders WHERE id = :order_id"),
                {"order_id": UUID(order_id)},
            )
            return result.scalar_one()

    def order_count(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT count(*) FROM orders")).scalar_one()

    def events(self, order_id: str) -> list[tuple[str | None, str]]:
        with self.engine.connect() as connection:
            result = connection.execute(
                text(
                    "SELECT from_status, to_status FROM order_events "
                    "WHERE order_id = :order_id ORDER BY id"
                ),
                {"order_id": UUID(order_id)},
            )
            return [(row.from_status, row.to_status) for row in result]

    def provider_payment(self, payment_id: str) -> dict:
        with httpx.Client(base_url=self.payments_url) as client:
            response = client.get(f"/payments/{payment_id}")
        assert response.status_code == 200, response.text
        return response.json()


@contextlib.contextmanager
def open_system(
    *,
    engine: Engine,
    database_url: str,
    geocoding_url: str,
    payments_url: str,
    clock: FakeClock,
    payments_timeout: float,
    **settings_overrides,
) -> Iterator[System]:
    """The whole application wired against the real database and the running mock providers."""
    # Most tests mix failing and healthy scenarios on purpose, so the breakers are switched off
    # (min_calls too high to ever be reached) unless a test asks for them.
    defaults = {
        "payments_max_attempts": 2,
        "payments_retry_backoff_seconds": 0,
        "geocoding_retry_backoff_seconds": 0,
        "order_ttl_seconds": 900,
        "reconciler_authorized_grace_seconds": 60,
        "breaker_min_calls": 1_000_000,
    }
    settings = Settings(database_url=database_url, **{**defaults, **settings_overrides})
    geocoding, payments = RecordingTransport(), RecordingTransport()
    geocoding_client = httpx.Client(
        base_url=geocoding_url, transport=geocoding, timeout=httpx.Timeout(2.0)
    )
    payments_client = httpx.Client(
        base_url=payments_url, transport=payments, timeout=httpx.Timeout(payments_timeout)
    )
    container = build_container(
        settings,
        geocoding_client=geocoding_client,
        payments_client=payments_client,
        clock=clock,
        monotonic_clock=clock.monotonic,
    )
    app = create_app(settings, container)
    warm_up(container.engine)

    try:
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://api") as client:
            yield System(
                container=container,
                app=app,
                client=client,
                engine=engine,
                clock=clock,
                geocoding=geocoding,
                payments=payments,
                payments_url=payments_url,
                payments_client=payments_client,
            )
    finally:
        geocoding_client.close()
        payments_client.close()
        container.engine.dispose()
