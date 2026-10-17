"""The composition root: builds the adapters and plugs them into the use cases.

It is plain constructor wiring, there is no dependency injection framework.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from app.adapters.geocoding.http_geocoder import HttpGeocoder
from app.adapters.payments.http_gateway import HttpPaymentGateway
from app.adapters.persistence.clock import DatabaseClock
from app.adapters.persistence.engine import create_database_engine
from app.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.adapters.resilience import CircuitBreaker
from app.application.create_order import CreateOrder
from app.application.order_payment_service import OrderPaymentService
from app.application.ports import Clock
from app.application.reconcile_orders import ReconcileOrders
from app.config import Settings

# How long to wait for a connection to be established, whatever the timeout of the whole call is.
_CONNECT_TIMEOUT_SECONDS = 2.0


@dataclass
class Container:
    create_order: CreateOrder
    reconcile_orders: ReconcileOrders
    engine: Engine
    circuit_breakers: dict[str, CircuitBreaker]
    _clients_to_close: list[httpx.Client] = field(default_factory=list)

    def database_is_reachable(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    def circuit_states(self) -> dict[str, str]:
        return {name: breaker.state.value for name, breaker in self.circuit_breakers.items()}

    def close(self) -> None:
        for client in self._clients_to_close:
            client.close()
        self.engine.dispose()


def build_container(
    settings: Settings,
    *,
    geocoding_client: httpx.Client | None = None,
    payments_client: httpx.Client | None = None,
    clock: Clock | None = None,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> Container:
    """Build everything. HTTP clients and the clock can be passed in (the tests do): the clients
    created here are closed by `Container.close`. Without a clock, the time is the database's."""
    engine = create_database_engine(settings)
    clock = clock or DatabaseClock(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    def new_unit_of_work() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    clients_to_close: list[httpx.Client] = []
    if geocoding_client is None:
        geocoding_client = _create_http_client(
            settings, settings.geocoding_url, settings.geocoding_timeout_seconds
        )
        clients_to_close.append(geocoding_client)
    if payments_client is None:
        payments_client = _create_http_client(
            settings, settings.payments_url, settings.payments_timeout_seconds
        )
        clients_to_close.append(payments_client)

    circuit_breakers = {
        name: _create_circuit_breaker(settings, name, monotonic_clock)
        for name in ("geocoding", "payments")
    }
    geocoder = HttpGeocoder(
        geocoding_client,
        max_attempts=settings.geocoding_max_attempts,
        retry_backoff_seconds=settings.geocoding_retry_backoff_seconds,
        breaker=circuit_breakers["geocoding"],
    )
    payment_gateway = HttpPaymentGateway(
        payments_client,
        max_attempts=settings.payments_max_attempts,
        retry_backoff_seconds=settings.payments_retry_backoff_seconds,
        breaker=circuit_breakers["payments"],
    )
    payment_service = OrderPaymentService(
        new_unit_of_work=new_unit_of_work, payment_gateway=payment_gateway, clock=clock
    )

    return Container(
        create_order=CreateOrder(
            new_unit_of_work=new_unit_of_work,
            geocoder=geocoder,
            payment_service=payment_service,
            clock=clock,
            order_time_to_live=timedelta(seconds=settings.order_ttl_seconds),
        ),
        reconcile_orders=ReconcileOrders(
            new_unit_of_work=new_unit_of_work,
            payment_service=payment_service,
            clock=clock,
            authorized_grace_period=timedelta(seconds=settings.reconciler_authorized_grace_seconds),
            batch_size=settings.reconciler_batch_size,
            lease_duration=timedelta(seconds=settings.reconciler_lease_seconds),
            max_concurrent_orders=settings.reconciler_concurrency,
        ),
        engine=engine,
        circuit_breakers=circuit_breakers,
        _clients_to_close=clients_to_close,
    )


def _create_http_client(settings: Settings, base_url: str, timeout_seconds: float) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        # The timeout covers connecting, writing, reading and waiting for a free connection, so no
        # attempt can take much longer than this.
        timeout=httpx.Timeout(timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_connections,
            keepalive_expiry=settings.http_keepalive_expiry_seconds,
        ),
    )


def _create_circuit_breaker(
    settings: Settings, name: str, monotonic_clock: Callable[[], float]
) -> CircuitBreaker:
    return CircuitBreaker(
        name,
        window=settings.breaker_window,
        min_calls=settings.breaker_min_calls,
        failure_rate=settings.breaker_failure_rate,
        recovery_seconds=settings.breaker_recovery_seconds,
        clock=monotonic_clock,
    )
