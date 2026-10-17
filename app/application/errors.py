class ApplicationError(Exception):
    """A business outcome the API reports to the caller as an error."""


class OutOfStock(ApplicationError):
    """No warehouse can fill the whole order."""


class ProductNotFound(ApplicationError):
    def __init__(self, product_ids: list[int]) -> None:
        super().__init__(f"unknown products: {product_ids}")
        self.product_ids = product_ids


class InvalidAddress(ApplicationError):
    """The geocoder does not recognise the shipping address."""


class GeocodingUnavailable(ApplicationError):
    """The geocoder could not be reached or answered with garbage."""


class IdempotencyKeyReused(ApplicationError):
    """The same Idempotency-Key was sent with a different request."""


class DuplicateIdempotencyKey(Exception):
    """Persistence signal: an order with this idempotency key already exists."""


# --- Payment gateway contract -------------------------------------------------------------
# These exist so the use cases can tell apart the three very different things that can happen
# when calling a payment provider. Releasing stock is only safe in the first two, and PaymentNotSent
# only counts for an order whose earlier attempts (if any) were not processed either.


class PaymentRejected(Exception):
    """The provider definitively refused the payment (declined card, invalid card, ...)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PaymentNotSent(Exception):
    """This request never reached the provider (connection refused, ...). Nothing happened."""


class PaymentOutcomeUnknown(Exception):
    """The request may or may not have been processed (read timeout, 5xx, ...)."""
