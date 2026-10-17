import logging
from collections.abc import Callable

from flask import Flask, Response, jsonify
from werkzeug.exceptions import HTTPException

from app.adapters.persistence.errors import (
    DATABASE_ERROR_TYPES,
    describe_database_error,
    is_transient_database_error,
)
from app.application.errors import (
    GeocodingUnavailable,
    IdempotencyKeyReused,
    InvalidAddress,
    OutOfStock,
    ProductNotFound,
)
from app.domain.errors import InvalidOrder

logger = logging.getLogger(__name__)

# Each business error and the HTTP status and error code it is reported with.
_HTTP_STATUS_AND_ERROR_CODE_BY_APPLICATION_ERROR: dict[type[Exception], tuple[int, str]] = {
    OutOfStock: (409, "out_of_stock"),
    ProductNotFound: (422, "product_not_found"),
    InvalidAddress: (422, "invalid_address"),
    IdempotencyKeyReused: (422, "idempotency_key_reused"),
    InvalidOrder: (422, "invalid_order"),
    GeocodingUnavailable: (502, "geocoding_unavailable"),
}


class InvalidRequest(Exception):
    """The request cannot be processed because of how it was written."""

    def __init__(
        self, status_code: int, error_code: str, message: str, details: list[dict]
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.details = details


def build_error_response(
    status_code: int, error_code: str, message: str, **extra_fields: object
) -> Response:
    error = {"code": error_code, "message": message}
    error.update({name: value for name, value in extra_fields.items() if value is not None})
    response = jsonify({"error": error})
    response.status_code = status_code
    return response


def install_error_handlers(app: Flask) -> None:
    @app.errorhandler(InvalidRequest)
    def handle_invalid_request(error: InvalidRequest) -> Response:
        return build_error_response(
            error.status_code, error.error_code, str(error), details=error.details
        )

    for error_type, (
        status_code,
        error_code,
    ) in _HTTP_STATUS_AND_ERROR_CODE_BY_APPLICATION_ERROR.items():
        app.register_error_handler(
            error_type, _handler_for_application_error(status_code, error_code)
        )

    # Overload and transient database trouble are 503s, not 500s (see persistence/errors.py).
    for error_type in DATABASE_ERROR_TYPES:
        app.register_error_handler(error_type, _handle_database_error)

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> Response:
        # Unknown paths, wrong methods, ...: the same JSON shape as every other error.
        return build_error_response(
            error.code or 500, (error.name or "error").lower().replace(" ", "_"), error.description
        )

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> Response:
        logger.error("unexpected error", exc_info=error)
        return build_error_response(500, "internal_error", "Something went wrong")


def _handler_for_application_error(
    status_code: int, error_code: str
) -> Callable[[Exception], Response]:
    def handle_application_error(error: Exception) -> Response:
        return build_error_response(status_code, error_code, str(error))

    return handle_application_error


def _handle_database_error(error: Exception) -> Response:
    if is_transient_database_error(error):
        logger.warning("transient database trouble: %s", describe_database_error(error))
        return _service_busy_response()
    logger.error("database error", exc_info=error)
    return build_error_response(500, "internal_error", "Something went wrong")


def _service_busy_response() -> Response:
    response = build_error_response(
        503,
        "service_busy",
        "Temporarily unable to process the request. Retry with the same Idempotency-Key.",
    )
    response.headers["Retry-After"] = "1"
    return response
