import os
from typing import Protocol
from uuid import UUID

from flask import Blueprint, Response, current_app, jsonify, request
from pydantic import ValidationError

from app.adapters.api.errors import InvalidRequest, build_error_response
from app.adapters.api.schemas import CreateOrderRequest, OrderResponse
from app.application.create_order import CreateOrder, CreateOrderCommand, OrderResult
from app.application.order_payment_service import FAILURE_CODE_PROVIDER_UNAVAILABLE
from app.domain.models import Order, OrderStatus, RequestedItem

orders_blueprint = Blueprint("orders", __name__)

# A client that gets one of these should retry the same request later.
_STATUSES_STILL_BEING_RESOLVED = (
    OrderStatus.PENDING,
    OrderStatus.AUTHORIZED,
    OrderStatus.PAYMENT_UNKNOWN,
)


class HealthProbe(Protocol):
    def database_is_reachable(self) -> bool: ...

    def circuit_states(self) -> dict[str, str]: ...


def _create_order_use_case() -> CreateOrder:
    return current_app.extensions["container"].create_order


def _health_probe() -> HealthProbe:
    return current_app.extensions["container"]


@orders_blueprint.post("/orders")
def create_order() -> Response:
    """Reserves stock in the closest warehouse that has everything, then authorizes and captures
    the payment.

    Send a fresh `Idempotency-Key` header per purchase attempt and reuse it when retrying: a retry
    never creates or charges twice. Answers: 201 created and paid, 200 a retry of an order that is
    already paid, 202 the payment is still being resolved (retry with the same key), 402 payment
    rejected, 409 out of stock or order expired, 422 invalid request, 400 bad Idempotency-Key, 502
    geocoding unavailable, 503 payment provider unavailable or service busy.
    """
    idempotency_key = _read_idempotency_key()
    order_request = _read_order_request()
    command = CreateOrderCommand(
        idempotency_key=idempotency_key,
        customer_id=order_request.customer_id,
        shipping_address=order_request.shipping_address,
        items=tuple(RequestedItem(item.product_id, item.quantity) for item in order_request.items),
        card_number=order_request.payment.card_number.get_secret_value(),
    )
    result = _create_order_use_case().execute(command)
    return _build_http_response(result)


@orders_blueprint.get("/health")
def health() -> Response:
    health_probe = _health_probe()
    if not health_probe.database_is_reachable():
        return build_error_response(503, "unhealthy", "database unreachable")
    return jsonify(
        {
            "status": "ok",
            "pid": os.getpid(),  # which worker process answered
            # The state of the circuit towards each service, as seen by this worker process.
            "circuits": health_probe.circuit_states(),
        }
    )


def _read_idempotency_key() -> UUID:
    header = request.headers.get("Idempotency-Key")
    if header is None:
        raise _bad_idempotency_key("Field required")
    try:
        return UUID(header)
    except ValueError:
        raise _bad_idempotency_key("Input should be a valid UUID") from None


def _bad_idempotency_key(problem: str) -> InvalidRequest:
    return InvalidRequest(
        400,
        "invalid_idempotency_key",
        "A valid Idempotency-Key header (a UUID) is required",
        details=[{"loc": ["header", "idempotency-key"], "message": problem}],
    )


def _read_order_request() -> CreateOrderRequest:
    body = request.get_json(force=True, silent=True)
    if body is None:
        details = [{"loc": ["body"], "message": "The body must be a JSON document"}]
        raise InvalidRequest(422, "invalid_request", "The request is not valid", details)
    try:
        return CreateOrderRequest.model_validate(body)
    except ValidationError as error:
        # Only where the problem is and why: the error also holds the offending input, which here
        # may be a card number.
        details = [
            {"loc": ["body", *[str(part) for part in problem["loc"]]], "message": problem["msg"]}
            for problem in error.errors()
        ]
        raise InvalidRequest(422, "invalid_request", "The request is not valid", details) from None


def _build_http_response(result: OrderResult) -> Response:
    order = result.order

    if order.status is OrderStatus.PAID:
        response = jsonify(OrderResponse.from_order(order).model_dump(mode="json"))
        response.status_code = 200 if result.already_existed else 201
        return response

    if order.status in _STATUSES_STILL_BEING_RESOLVED:
        response = jsonify(OrderResponse.from_order(order).model_dump(mode="json"))
        response.status_code = 202
        response.headers["Retry-After"] = "5"
        return response

    if order.status is OrderStatus.PAYMENT_FAILED:
        return _build_payment_failed_response(order)

    return _build_order_expired_response(order)


def _build_payment_failed_response(order: Order) -> Response:
    if order.failure_code == FAILURE_CODE_PROVIDER_UNAVAILABLE:
        return build_error_response(
            503,
            "payment_provider_unavailable",
            "The payment provider is unavailable. Nothing was charged; "
            "try again with a new Idempotency-Key.",
            order_id=str(order.id),
            order_status=order.status.value,
        )
    return build_error_response(
        402,
        "payment_failed",
        "The payment was rejected. Nothing was charged.",
        reason=order.failure_code,
        order_id=str(order.id),
        order_status=order.status.value,
    )


def _build_order_expired_response(order: Order) -> Response:
    return build_error_response(
        409,
        "order_expired",
        "The payment was not confirmed in time and the order expired. Nothing was charged; "
        "start a new order with a new Idempotency-Key.",
        order_id=str(order.id),
        order_status=order.status.value,
    )
