import httpx

from app.adapters.resilience import CircuitBreaker, wait_before_retry
from app.application.errors import GeocodingUnavailable, InvalidAddress
from app.domain.errors import DomainError
from app.domain.models import Coordinates


class HttpGeocoder:
    """Geocoding over HTTP: GET /geocode?address=... answers {"lat": ..., "lon": ...} or a 404.

    Looking an address up changes nothing, so transient trouble (a timeout, a connection error, a
    5xx) is retried. When the geocoder keeps failing, a circuit breaker makes the calls fail at
    once, so an outage costs a request a few milliseconds instead of a timeout.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_attempts: int = 2,
        retry_backoff_seconds: float = 0.1,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._max_attempts = max(1, max_attempts)
        self._retry_backoff_seconds = retry_backoff_seconds
        self._breaker = breaker or CircuitBreaker("geocoding")

    def geocode(self, address: str) -> Coordinates:
        last_problem = "no attempt was made"

        for attempt_number in range(1, self._max_attempts + 1):
            if attempt_number > 1:
                wait_before_retry(self._retry_backoff_seconds, attempt_number - 1)
            if not self._breaker.allow_call():
                raise GeocodingUnavailable("the geocoder keeps failing: not calling it for now")

            try:
                response = self._client.get("/geocode", params={"address": address})
            except httpx.HTTPError as error:
                self._breaker.record_failure()
                last_problem = f"unreachable ({type(error).__name__})"
                continue
            if response.status_code >= 500:
                self._breaker.record_failure()
                last_problem = f"answered {response.status_code}"
                continue
            self._breaker.record_success()  # it answered, and "not found" is an answer too

            if response.status_code == 404:
                raise InvalidAddress(f"address not found: {address!r}")
            if response.status_code != 200:
                raise GeocodingUnavailable(f"geocoder answered {response.status_code}")
            return self._coordinates_from(response)

        raise GeocodingUnavailable(f"geocoder {last_problem}")

    @staticmethod
    def _coordinates_from(response: httpx.Response) -> Coordinates:
        try:
            body = response.json()
            return Coordinates(float(body["lat"]), float(body["lon"]))
        except (ValueError, KeyError, TypeError, DomainError) as error:
            raise GeocodingUnavailable("geocoder returned an unusable response") from error
