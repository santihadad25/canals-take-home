"""Building blocks to survive a dependency that is slow, flaky or down."""

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"  # calls go through
    OPEN = "open"  # calls are refused without being attempted
    HALF_OPEN = "half_open"  # one probe call decides whether to close again


class CircuitBreaker:
    """Stops calling a dependency that keeps failing, so that requests fail fast instead of
    piling up behind timeouts.

    * CLOSED: every call goes through. The last `window` outcomes are remembered. When at least
      `min_calls` of them are known and the share of failures reaches `failure_rate`, the breaker
      opens. Measuring a rate over a window (not consecutive failures) means that a few unlucky
      requests cannot trip it while the dependency is healthy.
    * OPEN: calls are refused for `recovery_seconds`.
    * HALF_OPEN: after that, a single probe call is let through. If it succeeds the breaker closes,
      if it fails the breaker opens again for another `recovery_seconds`.

    Callers ask `allow_call()` and then report `record_success()` or `record_failure()` for the
    calls they made. Only trouble in reaching the dependency (timeouts, connection errors, 5xx) is
    a failure: an answer such as "card declined" proves the dependency works.

    The state lives in the process: with several workers each one learns on its own, which is fine
    because each notices an outage within a few calls. The threads of a process share one breaker,
    so every method takes a lock.
    """

    def __init__(
        self,
        name: str,
        *,
        window: int = 40,
        min_calls: int = 20,
        failure_rate: float = 0.5,
        recovery_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._min_calls = min_calls
        self._failure_rate = failure_rate
        self._recovery_seconds = recovery_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._recent_outcomes: deque[bool] = deque(maxlen=window)  # True means the call worked
        self._state = BreakerState.CLOSED
        self._opened_at = 0.0
        self._probe_started_at: float | None = None

    @property
    def state(self) -> BreakerState:
        with self._lock:
            recovery_time_has_passed = self._clock() - self._opened_at >= self._recovery_seconds
            if self._state is BreakerState.OPEN and recovery_time_has_passed:
                return BreakerState.HALF_OPEN
            return self._state

    def allow_call(self) -> bool:
        """May a call be made right now? While HALF_OPEN only one caller at a time gets a yes."""
        with self._lock:
            if self._state is BreakerState.CLOSED:
                return True

            now = self._clock()
            if self._state is BreakerState.OPEN:
                if now - self._opened_at < self._recovery_seconds:
                    return False
                self._state = BreakerState.HALF_OPEN
                self._probe_started_at = None
                logger.info("circuit %s: half-open, letting one probe call through", self.name)

            # A probe whose caller vanished must not block the breaker forever.
            probe_is_available = (
                self._probe_started_at is None
                or now - self._probe_started_at >= self._recovery_seconds
            )
            if probe_is_available:
                self._probe_started_at = now
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._recent_outcomes.clear()
                self._state = BreakerState.CLOSED
                logger.info("circuit %s: closed again, the dependency answers", self.name)
            elif self._state is BreakerState.CLOSED:
                self._recent_outcomes.append(True)

    def record_failure(self) -> None:
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._open_circuit("the probe call failed")
            elif self._state is BreakerState.CLOSED:
                self._recent_outcomes.append(False)
                failed_calls = self._recent_outcomes.count(False)
                known_calls = len(self._recent_outcomes)
                enough_calls_to_judge = known_calls >= self._min_calls
                if enough_calls_to_judge and failed_calls / known_calls >= self._failure_rate:
                    self._open_circuit(f"{failed_calls} of the last {known_calls} calls failed")
            # While OPEN this is a call that was already in flight when the breaker opened: it
            # tells us nothing new.

    def _open_circuit(self, reason: str) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._probe_started_at = None
        logger.warning(
            "circuit %s: OPEN, calls fail fast for %.0fs (%s)",
            self.name,
            self._recovery_seconds,
            reason,
        )


def wait_before_retry(base_delay_seconds: float, retry_number: int) -> None:
    """Wait before retry number `retry_number` (1 is the first retry).

    The wait grows exponentially and has some jitter, so that many workers retrying at the same
    time do not hit the dependency in lockstep.
    """
    if base_delay_seconds > 0:
        delay = base_delay_seconds * 2 ** (retry_number - 1) * random.uniform(0.5, 1.5)
        time.sleep(delay)
