"""Mock card payments provider. It looks like a typical payments API:

POST /payments                   create a payment; "capture": false only authorizes (holds funds)
POST /payments/{id}/capture      capture an authorized payment
POST /payments/{id}/cancel       release an authorized payment
GET  /payments/{id}
POST /payments/{id}/refunds      refund (part of) a captured payment

Every POST needs an Idempotency-Key header: repeating a request with the same key and the same
body replays the original response instead of doing the work again.

What happens for a given card (declined, slow, unavailable, ...) is configured in scenarios.json.
The mock keeps its state in memory: restart it to start from scratch. It serves requests in
threads, so the state is only touched while holding a lock; the slow answers sleep outside of it.
"""

import json
import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, request


def luhn_valid(number: str) -> bool:
    if not number.isdigit() or not 12 <= len(number) <= 19:
        return False
    total = 0
    for position, char in enumerate(reversed(number)):
        digit = int(char)
        if position % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _respond(status_code: int, body: dict, headers: dict | None = None) -> Response:
    response = jsonify(body)
    response.status_code = status_code
    response.headers.update(headers or {})
    return response


def _error(status_code: int, code: str, message: str) -> Response:
    return _respond(status_code, {"error": {"code": code, "message": message}})


@dataclass
class StoredResponse:
    fingerprint: str
    status_code: int
    body: dict


class Provider:
    def __init__(self, scenarios: dict, authorization_ttl: timedelta) -> None:
        self.scenarios = scenarios
        self.authorization_ttl = authorization_ttl
        self.payments: dict[str, dict] = {}
        self.responses: dict[str, StoredResponse] = {}
        self.attempts: Counter[tuple[str, str]] = Counter()
        self.lock = threading.Lock()  # protects everything above

    def scenario(self, card_number: str, operation: str) -> dict:
        default = self.scenarios.get("default", {}).get(operation, {"outcome": "approve"})
        override = self.scenarios.get("cards", {}).get(card_number, {}).get(operation, {})
        return {**default, **override}

    def expire_if_due(self, payment: dict) -> None:
        """An authorization nobody captures lapses on its own, like on the card networks."""
        if payment["status"] == "authorized" and datetime.now(UTC) >= payment["_expires_at"]:
            payment["status"] = "canceled"

    @staticmethod
    def public(payment: dict) -> dict:
        return {key: value for key, value in payment.items() if not key.startswith("_")}


def create_app(
    scenarios_path: str | None = None, authorization_ttl_seconds: int | None = None
) -> Flask:
    path = Path(scenarios_path or os.environ.get("SCENARIOS_FILE", "scenarios.json"))
    ttl_seconds = (
        authorization_ttl_seconds
        if authorization_ttl_seconds is not None
        else int(os.environ.get("AUTHORIZATION_TTL_SECONDS", 7 * 24 * 3600))
    )
    provider = Provider(
        json.loads(path.read_text(encoding="utf-8")), timedelta(seconds=ttl_seconds)
    )
    app = Flask(__name__)
    app.json.sort_keys = False

    # --- helpers shared by the mutating endpoints -------------------------------------------

    def fingerprint(method: str, route: str, body: dict) -> str:
        return json.dumps([method, route, body], sort_keys=True)

    def read_body() -> dict | None:
        raw = request.get_data()
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def slow_down(scenario: dict, attempt: int) -> None:
        # The work was already done; only the answer is late (the caller times out).
        if scenario["outcome"] == "slow" and attempt <= scenario.get("affected_attempts", 1):
            time.sleep(scenario.get("delay_seconds", 10))

    def unavailable(scenario: dict, attempt: int) -> Response | None:
        # A 5xx before anything was processed: nothing is stored.
        if scenario["outcome"] == "unavailable" and attempt <= scenario.get("affected_attempts", 1):
            return _error(503, "service_unavailable", "try again later")
        return None

    def replay(key: str, print_: str) -> Response | None:
        stored = provider.responses.get(key)
        if stored is None:
            return None
        if stored.fingerprint != print_:
            return _error(
                422, "idempotency_key_reused", "this key was used with different parameters"
            )
        return _respond(stored.status_code, stored.body, {"Idempotent-Replayed": "true"})

    def remember(key: str, print_: str, status_code: int, body: dict) -> Response:
        provider.responses[key] = StoredResponse(print_, status_code, body)
        return _respond(status_code, body)

    def conflict(code: str, message: str) -> tuple[int, dict]:
        return 409, {"error": {"code": code, "message": message}}

    # --- endpoints ------------------------------------------------------------------------

    @app.post("/payments")
    def create_payment() -> Response:
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return _error(400, "idempotency_key_required", "send an Idempotency-Key header")
        body = read_body()
        if body is None:
            return _error(400, "invalid_json", "the body must be a JSON object")
        amount, currency = body.get("amount_cents"), body.get("currency")
        card, description = body.get("card_number"), body.get("description", "")
        capture = body.get("capture", True)
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount <= 0
            or not isinstance(currency, str)
            or not isinstance(card, str)
            or not isinstance(description, str)
            or not isinstance(capture, bool)
        ):
            return _error(
                422, "invalid_request", "amount_cents, currency and card_number are required"
            )

        print_ = fingerprint("POST", "/payments", body)
        with provider.lock:
            provider.attempts[("authorize", idempotency_key)] += 1
            attempt = provider.attempts[("authorize", idempotency_key)]
            scenario = provider.scenario(card, "authorize")

            if (response := unavailable(scenario, attempt)) is not None:
                return response
            if (response := replay(idempotency_key, print_)) is None:
                if not luhn_valid(card):
                    return _error(422, "invalid_card_number", "the card number is not valid")
                response = _authorize_or_decline(
                    idempotency_key, print_, scenario, amount, currency, description, card, capture
                )
        slow_down(scenario, attempt)
        return response

    def _authorize_or_decline(
        idempotency_key: str,
        print_: str,
        scenario: dict,
        amount: int,
        currency: str,
        description: str,
        card: str,
        capture: bool,
    ) -> Response:
        if scenario["outcome"] == "decline":
            code = scenario.get("code", "card_declined")
            return remember(
                idempotency_key,
                print_,
                402,
                {"error": {"code": code, "message": "the card was declined"}},
            )
        now = datetime.now(UTC)
        payment = {
            "id": f"pay_{uuid.uuid4().hex[:20]}",
            "status": "captured" if capture else "authorized",
            "amount_cents": amount,
            "currency": currency,
            "description": description,
            "card_last4": card[-4:],
            "refunded_cents": 0,
            "created_at": now.isoformat(),
            "_card": card,
            "_expires_at": now + provider.authorization_ttl,
        }
        payment["expires_at"] = payment["_expires_at"].isoformat()
        provider.payments[payment["id"]] = payment
        return remember(idempotency_key, print_, 201, provider.public(payment))

    @app.post("/payments/<payment_id>/capture")
    def capture_payment(payment_id: str) -> Response:
        return change_status(payment_id, request.headers.get("Idempotency-Key"), "capture")

    @app.post("/payments/<payment_id>/cancel")
    def cancel_payment(payment_id: str) -> Response:
        return change_status(payment_id, request.headers.get("Idempotency-Key"), "cancel")

    def change_status(payment_id: str, key: str | None, operation: str) -> Response:
        if not key:
            return _error(400, "idempotency_key_required", "send an Idempotency-Key header")

        with provider.lock:
            payment = provider.payments.get(payment_id)
            if payment is None:
                return _error(404, "payment_not_found", "no such payment")

            print_ = fingerprint("POST", f"/payments/{payment_id}/{operation}", {})
            provider.attempts[(operation, key)] += 1
            attempt = provider.attempts[(operation, key)]
            scenario = provider.scenario(payment["_card"], operation)

            if (response := unavailable(scenario, attempt)) is not None:
                return response
            if (response := replay(key, print_)) is None:
                provider.expire_if_due(payment)
                status = payment["status"]
                if operation == "capture":
                    if status == "captured":
                        code, body = conflict("payment_already_captured", "already captured")
                    elif status == "canceled" or scenario["outcome"] == "reject":
                        payment["status"] = "canceled"  # a rejected capture means the hold is gone
                        code, body = conflict(
                            "payment_canceled", "the authorization is no longer valid"
                        )
                    else:
                        payment["status"] = "captured"
                        code, body = 200, provider.public(payment)
                else:
                    if status == "captured":
                        code, body = conflict("payment_already_captured", "already captured")
                    else:
                        payment["status"] = "canceled"
                        code, body = 200, provider.public(payment)
                response = remember(key, print_, code, body)
        slow_down(scenario, attempt)
        return response

    @app.get("/payments/<payment_id>")
    def get_payment(payment_id: str) -> Response:
        with provider.lock:
            payment = provider.payments.get(payment_id)
            if payment is None:
                return _error(404, "payment_not_found", "no such payment")
            provider.expire_if_due(payment)
            return _respond(200, provider.public(payment))

    @app.post("/payments/<payment_id>/refunds")
    def refund_payment(payment_id: str) -> Response:
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return _error(400, "idempotency_key_required", "send an Idempotency-Key header")
        body = read_body()
        if body is None:
            return _error(400, "invalid_json", "the body must be a JSON object")

        with provider.lock:
            payment = provider.payments.get(payment_id)
            if payment is None:
                return _error(404, "payment_not_found", "no such payment")

            print_ = fingerprint("POST", f"/payments/{payment_id}/refunds", body)
            if (response := replay(idempotency_key, print_)) is not None:
                return response
            if payment["status"] != "captured":
                return _error(409, "payment_not_captured", "only captured payments can be refunded")
            remaining = payment["amount_cents"] - payment["refunded_cents"]
            amount = body.get("amount_cents", remaining)
            if (
                not isinstance(amount, int)
                or isinstance(amount, bool)
                or not 0 < amount <= remaining
            ):
                return _error(422, "amount_too_large", f"at most {remaining} cents can be refunded")
            payment["refunded_cents"] += amount
            refund = {
                "id": f"re_{uuid.uuid4().hex[:20]}",
                "payment_id": payment_id,
                "amount_cents": amount,
                "status": "succeeded",
            }
            return remember(idempotency_key, print_, 201, refund)

    @app.get("/health")
    def health() -> Response:
        return _respond(200, {"status": "ok"})

    return app


app = create_app()
