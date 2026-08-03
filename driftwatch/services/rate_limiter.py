"""Client-side token buckets for Drive and Sheets pacing (lane B).

WHY pacing rather than backoff: backoff is a *reaction* — you have already spent a
round trip, already been counted against the quota window, and you now sleep with
nothing to show for it. A token bucket spends the wall clock *instead of* the
failed request. SPEC §4.4 states the rule plainly: pacing beats retrying.

The numbers that matter:

* Sheets: Google's per-user read quota is **60 requests / minute**. The default of
  50 leaves headroom for the Test Connection wizard, a manual "Stage Now", and a
  concurrent cron all landing in the same minute.
* Drive: the per-user ceiling is ~325 000 quota units / minute. The default of
  200 000 leaves the same kind of headroom.

Both are per-connection settings (``gdrive.connection.sheets_reads_per_min`` /
``drive_units_per_min``) so an administrator can throttle a noisy connection
without touching code.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from .errors import CODE_RATE_LIMITED, GDriveQuotaError

_logger = logging.getLogger(__name__)

__all__ = [
    'TokenBucket',
    'BucketRegistry',
    'REGISTRY',
    'bucket_for',
    'DEFAULT_SHEETS_READS_PER_MIN',
    'DEFAULT_DRIVE_UNITS_PER_MIN',
    'API_DRIVE',
    'API_SHEETS',
]

DEFAULT_SHEETS_READS_PER_MIN = 50
DEFAULT_DRIVE_UNITS_PER_MIN = 200000

API_DRIVE = 'drive'
API_SHEETS = 'sheets'

#: Default deadline for a single :meth:`TokenBucket.acquire`. Longer than any
#: sane wait, shorter than the 600 s cron budget, so an exhausted bucket surfaces
#: as a quota error inside the run rather than as a silently truncated crawl.
DEFAULT_ACQUIRE_TIMEOUT = 120.0


class TokenBucket:
    """A thread-safe, monotonic-clock token bucket.

    Tokens refill continuously at ``refill_per_minute / 60`` per second up to
    ``capacity``. :meth:`acquire` blocks until ``cost`` tokens are available or the
    timeout expires.

    WHY a monotonic clock: ``time.time()`` moves backwards on NTP correction and
    on a VM resume. A bucket driven by wall-clock time can hand out a minute of
    quota in one instant after a clock step, which is exactly the burst the bucket
    exists to prevent.

    WHY continuous refill rather than a fixed window: Google's quota windows slide.
    A fixed window lets you spend the whole minute's budget in the last second of
    window N and the first second of window N+1 — a 2× burst that trips the very
    limit being modelled.
    """

    __slots__ = ('name', 'capacity', 'rate_per_second', '_tokens', '_last', '_lock',
                 '_clock', '_sleep', 'waits', 'total_wait_seconds', 'granted')

    def __init__(
        self,
        capacity: float,
        refill_per_minute: float,
        name: str = '',
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if refill_per_minute <= 0:
            raise ValueError('refill_per_minute must be positive, got %r' % (refill_per_minute,))
        self.name = name or 'bucket'
        self.capacity = float(max(capacity, 1.0))
        self.rate_per_second = float(refill_per_minute) / 60.0
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()
        self.waits = 0
        self.total_wait_seconds = 0.0
        self.granted = 0.0

    # -- internals -------------------------------------------------------- #

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
            self._last = now

    # -- public API ------------------------------------------------------- #

    @property
    def tokens(self) -> float:
        """Currently available tokens, after applying pending refill."""
        with self._lock:
            self._refill_locked()
            return self._tokens

    def try_acquire(self, cost: float = 1.0) -> bool:
        """Take ``cost`` tokens if they are available right now; never blocks."""
        cost = self._clamp(cost)
        with self._lock:
            self._refill_locked()
            if self._tokens >= cost:
                self._tokens -= cost
                self.granted += cost
                return True
            return False

    def acquire(self, cost: float = 1.0, timeout: Optional[float] = DEFAULT_ACQUIRE_TIMEOUT) -> float:
        """Block until ``cost`` tokens are available. Return the seconds waited.

        :raises GDriveQuotaError: if ``timeout`` elapses first. WHY raise rather
            than proceed: proceeding would mean issuing a call the operator has
            explicitly said the system must not issue at this rate, and the result
            would be a 429 anyway — but attributed to Google instead of to us.
        """
        cost = self._clamp(cost)
        deadline = None if timeout is None else self._clock() + float(timeout)
        waited = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= cost:
                    self._tokens -= cost
                    self.granted += cost
                    if waited:
                        self.waits += 1
                        self.total_wait_seconds += waited
                    return waited
                deficit = cost - self._tokens
                delay = deficit / self.rate_per_second
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise GDriveQuotaError(
                        'Rate limiter %r could not grant %.0f unit(s) within %.1fs; '
                        'lower the workload or raise the per-minute allowance on the '
                        'connection.' % (self.name, cost, float(timeout)),
                        code=CODE_RATE_LIMITED,
                        details={'bucket': self.name, 'cost': cost, 'timeout': timeout},
                    )
                delay = min(delay, remaining)
            # Cap a single nap so a clock adjustment or a capacity change is picked
            # up promptly instead of after a multi-minute sleep.
            delay = max(0.001, min(delay, 5.0))
            self._sleep(delay)
            waited += delay

    def _clamp(self, cost: float) -> float:
        """Bound ``cost`` to the bucket capacity.

        A request costing more than the whole bucket could never be granted and
        would block until the timeout, converting a pacing decision into an outage.
        Clamping with a warning keeps the run alive and makes the misconfiguration
        visible.
        """
        cost = float(cost)
        if cost <= 0:
            return 0.0
        if cost > self.capacity:
            _logger.warning(
                'Rate limiter %r asked for %.0f units but capacity is %.0f; clamping. '
                'Raise the per-minute allowance on the connection.',
                self.name, cost, self.capacity,
            )
            return self.capacity
        return cost

    def stats(self) -> dict:
        """Return counters for ``gdrive.sync.run`` quota accounting."""
        return {
            'name': self.name,
            'capacity': self.capacity,
            'per_minute': self.rate_per_second * 60.0,
            'granted': self.granted,
            'waits': self.waits,
            'total_wait_seconds': round(self.total_wait_seconds, 3),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '<TokenBucket %s %.0f/min>' % (self.name, self.rate_per_second * 60.0)


class BucketRegistry:
    """Process-wide registry of buckets keyed by ``(connection_id, api)``.

    WHY process-wide and **not** thread-local (unlike the service objects in
    :mod:`.google_client`): the quota being modelled is Google's, and Google counts
    every thread of every worker against the same per-user budget. A per-thread
    bucket would multiply the effective rate by the worker count and defeat the
    entire mechanism. The bucket is therefore shared and internally locked, which
    is the opposite of the rule for ``googleapiclient`` service objects — those are
    not thread-safe and must never be shared.
    """

    def __init__(self) -> None:
        self._buckets: Dict[Tuple[int, str], TokenBucket] = {}
        self._lock = threading.Lock()

    def bucket(
        self,
        connection_id: int,
        api: str,
        per_minute: float,
        capacity: Optional[float] = None,
    ) -> TokenBucket:
        """Return (creating if needed) the bucket for ``(connection_id, api)``.

        If an existing bucket's rate no longer matches ``per_minute`` — the
        administrator edited the connection — it is replaced rather than mutated,
        so the new rate takes effect on the next call without an Odoo restart.
        """
        key = (int(connection_id or 0), str(api))
        cap = float(capacity if capacity is not None else per_minute)
        with self._lock:
            existing = self._buckets.get(key)
            if existing is not None:
                if abs(existing.rate_per_second * 60.0 - float(per_minute)) < 1e-9:
                    return existing
                _logger.info(
                    'Rate limit for connection %s api %s changed %0.f/min -> %0.f/min; '
                    'resetting the token bucket.',
                    key[0], key[1], existing.rate_per_second * 60.0, per_minute,
                )
            bucket = TokenBucket(
                capacity=cap,
                refill_per_minute=per_minute,
                name='conn:%s/%s' % (key[0], key[1]),
            )
            self._buckets[key] = bucket
            return bucket

    def reset(self, connection_id: Optional[int] = None) -> None:
        """Drop cached buckets. Used by tests and by connection reconfiguration."""
        with self._lock:
            if connection_id is None:
                self._buckets.clear()
            else:
                for key in [k for k in self._buckets if k[0] == int(connection_id)]:
                    del self._buckets[key]

    def stats(self) -> list:
        """Snapshot of every live bucket, for run-level quota reporting."""
        with self._lock:
            return [b.stats() for b in self._buckets.values()]


#: The single registry every service in this package uses.
REGISTRY = BucketRegistry()


def bucket_for(connection_id: int, api: str, per_minute: Optional[float] = None) -> TokenBucket:
    """Convenience accessor over :data:`REGISTRY` with the documented defaults."""
    if per_minute is None:
        per_minute = (
            DEFAULT_SHEETS_READS_PER_MIN if api == API_SHEETS else DEFAULT_DRIVE_UNITS_PER_MIN
        )
    return REGISTRY.bucket(connection_id, api, per_minute)
