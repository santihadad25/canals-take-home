import httpx

from app.adapters.resilience import CircuitBreaker, wait_before_retry
from app.application.errors import (
    IdempotencyKeyReused,
    PaymentNotSent,
    PaymentOutcomeUnknown,
    PaymentRejected,
)
from app.application.ports import PaymentStatus
from app.domain.models import Money

CURRENCY = "USD"

# If one of these happens the request never reached the provider, so it cannot have seen it.
_ERRORS_BEFORE_THE_REQUEST_REACHED_THE_PROVIDER = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)

_PAYMENT_STATUS_BY_NAME = {status.value: status for status in PaymentStatus}


class HttpPaymentGateway:
    """Talks to a typical card payments REST API:

    POST /payments                     create (capture=false authorizes only)  [Idempotency-Key]
    POST /payments/{id}/capture                                                [Idempotency-Key]
    POST /payments/{id}/cancel                                                 [Idempotency-Key]
    GET  /payments/{id}

    Retrying is safe because every call that changes something carries an idempotency key. A
    circuit breaker stops calling the provider while it is failing: those calls are reported as
    "never sent", so they fail at once instead of waiting for a timeout, and it is known that they
    did nothing.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_attempts: int = 2,
        retry_backoff_seconds: float = 0.2,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._max_attempts = max(1, max_attempts)
        self._retry_backoff_seconds = retry_backoff_seconds
        self._breaker = breaker or CircuitBreaker("payments")

    def authorize(
        self, *, idempotency_key: str, amount: Money, description: str, card_number: str
    ) -> str:
        response = self._send_with_retries(
            "POST",
            "/payments",
            idempotency_key=idempotency_key,
            json_body={
                "amount_cents": amount.cents,
                "currency": CURRENCY,
                "card_number": card_number,
                "description": description,
                "capture": False,
            },
        )

        if response.status_code == 201:
            body = _json_body(response)
            payment_id = body.get("id")
            is_authorized = body.get("status") == PaymentStatus.AUTHORIZED.value
            if is_authorized and isinstance(payment_id, str):
                return payment_id
            raise PaymentOutcomeUnknown("unexpected authorize response")

        error_code = _error_code_of(response)
        if response.status_code == 422 and error_code == "idempotency_key_reused":
            raise IdempotencyKeyReused("the payment provider saw different data for this order")
        if response.status_code in (402, 422):
            raise PaymentRejected(error_code or "payment_rejected")
        # Anything else (a problem with our credentials, a 404, ...) is not something the customer
        # did. Keep the order open instead of blaming the card: it expires if nobody resolves it.
        raise PaymentOutcomeUnknown(f"unexpected authorize status {response.status_code}")

    def capture(self, *, payment_id: str, idempotency_key: str) -> PaymentStatus:
        response = self._send_with_retries(
            "POST", f"/payments/{payment_id}/capture", idempotency_key=idempotency_key
        )
        return _payment_status_from(
            response,
            status_by_conflict_code={
                "payment_already_captured": PaymentStatus.CAPTURED,
                "payment_canceled": PaymentStatus.CANCELED,
            },
        )

    def cancel(self, *, payment_id: str, idempotency_key: str) -> PaymentStatus:
        response = self._send_with_retries(
            "POST", f"/payments/{payment_id}/cancel", idempotency_key=idempotency_key
        )
        return _payment_status_from(
            response,
            status_by_conflict_code={"payment_already_captured": PaymentStatus.CAPTURED},
        )

    def get_status(self, payment_id: str) -> PaymentStatus:
        response = self._send_with_retries("GET", f"/payments/{payment_id}")
        return _payment_status_from(response, status_by_conflict_code={})

    def _send_with_retries(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        """Send the request, retrying a limited number of times with the same idempotency key.

        Returns the first answer that is not worth retrying. If every attempt failed, it raises
        PaymentNotSent when the provider provably never saw any of them, and PaymentOutcomeUnknown
        when it may have processed one.
        """
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        provider_may_have_processed_it = False
        circuit_was_open = False
        last_error: Exception | None = None

        for attempt_number in range(1, self._max_attempts + 1):
            if attempt_number > 1:
                wait_before_retry(self._retry_backoff_seconds, attempt_number - 1)
            if not self._breaker.allow_call():
                circuit_was_open = True
                break

            try:
                response = self._client.request(method, path, json=json_body, headers=headers)
            except _ERRORS_BEFORE_THE_REQUEST_REACHED_THE_PROVIDER as error:
                self._breaker.record_failure()
                last_error = error
                continue
            except httpx.HTTPError as error:  # a read timeout, a connection reset, ...
                self._breaker.record_failure()
                provider_may_have_processed_it = True
                last_error = error
                continue

            if response.status_code >= 500:
                self._breaker.record_failure()
                provider_may_have_processed_it = True
                continue

            self._breaker.record_success()  # it answered, whatever the answer says
            if _is_still_being_processed(response):
                provider_may_have_processed_it = True
                continue
            return response

        message = f"{method} {path} failed after {self._max_attempts} attempts"
        if circuit_was_open:
            message = f"{method} {path} not attempted: the payments circuit is open"
        if provider_may_have_processed_it:
            raise PaymentOutcomeUnknown(message) from last_error
        raise PaymentNotSent(message) from last_error


def _payment_status_from(
    response: httpx.Response, *, status_by_conflict_code: dict[str, PaymentStatus]
) -> PaymentStatus:
    """The status of the payment according to the provider's answer.

    A 409 conflict such as "already captured" or "already canceled" also tells us where the
    payment is, so it maps to a status through `status_by_conflict_code`.
    """
    if response.status_code == 200:
        payment_status = _PAYMENT_STATUS_BY_NAME.get(str(_json_body(response).get("status")))
        if payment_status is not None:
            return payment_status
    elif response.status_code == 409:
        error_code = _error_code_of(response)
        if error_code in status_by_conflict_code:
            return status_by_conflict_code[error_code]
    raise PaymentOutcomeUnknown(f"unexpected status {response.status_code}")


def _is_still_being_processed(response: httpx.Response) -> bool:
    """The provider answers 409 while an earlier request with the same key is still running."""
    return response.status_code == 409 and _error_code_of(response) == "request_in_progress"


def _json_body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code_of(response: httpx.Response) -> str | None:
    error = _json_body(response).get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    return error_code if isinstance(error_code, str) else None
