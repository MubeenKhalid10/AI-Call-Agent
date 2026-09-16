"""Retrying the things that are safe to retry, and refusing to retry the rest.

Phase 9. The whole module exists to make one distinction impossible to blur::

    the operation definitely did not happen   ->  retrying is free
    the operation was definitely refused      ->  retrying repeats the refusal
    we do not know whether it happened        ->  retrying may do it twice

Most retry helpers collapse the third case into the first, because from the
caller's side a timeout and a connection reset look the same. For a *read* that
is fine. For "place a phone call to a stranger" it is the difference between one
call and two, and the second one is a person's phone ringing again for no
reason. So `Verdict.AMBIGUOUS` is a first-class outcome here, and a classifier
that returns it makes `call_with_retry` stop and raise `AmbiguousOutcomeError`
however many attempts are left — the caller has to decide what to do about not
knowing, and in this project that decision is written down in
`campaigns/dialer.py`: mark the attempt `UNRESOLVED`, which blocks the prospect
from being dialled again, and let `reliability/recovery.py` find out from the
carrier what actually happened.

**Backoff.** Exponential with a cap, plus jitter, because the failure this
protects against is usually shared — the network is down, or the provider is
rate-limiting — and every caller retrying on the same schedule turns a blip into
a thundering herd. The jitter is "equal jitter": half the computed delay plus a
random share of the other half, which keeps the growth curve while spreading the
arrivals. Full jitter (uniform from zero) was not used because a retry that
fires immediately after a rate-limit response is worse than useless.

**Timeouts belong to the policy, not to the operation.** An operation with no
timeout is not retryable in any useful sense: the failure it is most likely to
suffer is hanging, and a hung await never reaches the retry loop. So every
policy carries `timeout_secs`, and `call_with_retry` applies it per attempt.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from loguru import logger

T = TypeVar("T")


class Verdict(StrEnum):
    """What a failure means for whether the operation may be tried again."""

    RETRY = "retry"
    """Transient, and the operation certainly did not take effect.

    A connection that was refused, a 503, a database that was not reachable. The
    request never reached anything that could act on it, so doing it again is
    exactly as safe as doing it the first time.
    """

    FATAL = "fatal"
    """The operation reached the other side and was rejected on its merits.

    A malformed number, an unverified caller ID, bad credentials, a constraint
    violation. Retrying reproduces the same rejection and wastes the time.
    """

    AMBIGUOUS = "ambiguous"
    """The request may or may not have taken effect, and we cannot tell.

    A timeout, a connection dropped mid-request, a 5xx from a service that
    might have committed before it failed. Safe to retry only if the operation
    is idempotent — and if it were, the caller would be using an idempotency
    key rather than asking this question.
    """


class AmbiguousOutcomeError(RuntimeError):
    """An operation's outcome could not be determined, so it was not retried.

    Raised instead of the underlying error so that a caller cannot mistake "it
    failed" for "it did not happen". The original exception is the `__cause__`,
    and `operation` names what was being attempted.
    """

    def __init__(self, operation: str, cause: BaseException) -> None:
        """Record which operation was ambiguous and why."""
        super().__init__(
            f"{operation} did not report an outcome ({cause.__class__.__name__}: {cause}). "
            f"Whether it took effect is unknown, so it was not retried."
        )
        self.operation = operation
        self.cause = cause


@dataclass(frozen=True)
class RetryPolicy:
    """How many times, how long between, and how long each attempt may take.

    Attributes:
        attempts: Total attempts including the first. 1 means "no retries",
            which is the correct policy for anything that changes the world
            without an idempotency key.
        base_delay_secs: Delay after the first failure.
        max_delay_secs: Ceiling for the exponential growth.
        multiplier: Growth factor per attempt.
        jitter: Fraction of each delay that is randomised, 0 to 1. At 0.5 a
            computed delay of 1s is spent as a random value in [0.5s, 1.0s].
        timeout_secs: Per-attempt timeout. None means the operation is trusted
            to bound itself, which almost nothing is.
    """

    attempts: int = 3
    base_delay_secs: float = 0.25
    max_delay_secs: float = 5.0
    multiplier: float = 2.0
    jitter: float = 0.5
    timeout_secs: float | None = 10.0

    def __post_init__(self) -> None:
        """Reject a policy that cannot be honoured, at construction rather than at use."""
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter must be between 0 and 1")
        if self.base_delay_secs < 0 or self.max_delay_secs < 0:
            raise ValueError("delays must not be negative")

    @property
    def retries(self) -> int:
        """How many times a first failure may be tried again."""
        return self.attempts - 1

    def delay_for(self, attempt: int, *, rand: Callable[[], float] = random.random) -> float:
        """Seconds to wait before attempt number `attempt` (1 is the first retry).

        Args:
            attempt: 1-based index of the retry about to be made.
            rand: Source of randomness, injected so a test can pin the jitter.
        """
        if attempt < 1:
            return 0.0
        raw = min(self.base_delay_secs * (self.multiplier ** (attempt - 1)), self.max_delay_secs)
        if self.jitter <= 0:
            return raw
        return raw * (1.0 - self.jitter) + raw * self.jitter * rand()

    def describe(self) -> str:
        """One line for a log or a startup report."""
        if self.attempts == 1:
            return f"no retries, {self.timeout_secs or 0:g}s timeout"
        return (
            f"{self.attempts} attempts, {self.base_delay_secs:g}s backoff x{self.multiplier:g} "
            f"capped at {self.max_delay_secs:g}s, {self.jitter:g} jitter, "
            f"{self.timeout_secs or 0:g}s timeout"
        )


#: For reading from an external service. Retrying a read cannot do anything
#: twice, so a timeout is treated as retryable rather than as ambiguous.
READ_POLICY = RetryPolicy(attempts=3, base_delay_secs=0.25, max_delay_secs=4.0, timeout_secs=10.0)

#: For a database write. Short delays because the failure is usually a
#: connection blip rather than a busy server, and the caller is often mid-call.
DATABASE_POLICY = RetryPolicy(
    attempts=3, base_delay_secs=0.1, max_delay_secs=2.0, timeout_secs=10.0
)

#: For anything that changes the world and has no idempotency key. One attempt,
#: with a timeout so it cannot hang. `place_call` uses this.
NEVER_RETRY = RetryPolicy(attempts=1, timeout_secs=20.0)


Classifier = Callable[[BaseException], Verdict]


def read_classifier(exc: BaseException) -> Verdict:
    """Classify a failure of an operation that only reads.

    A timeout is `RETRY` here rather than `AMBIGUOUS`: a read that may or may
    not have happened is a read that did not happen, as far as anything outside
    the reader is concerned.
    """
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return Verdict.RETRY
    return _shared_classifier(exc)


def write_classifier(exc: BaseException) -> Verdict:
    """Classify a failure of an operation that changes something.

    A timeout, a dropped connection, or any failure the raiser marks
    `retryable` is `AMBIGUOUS` here: every one of them can mean the request was
    received and acted on before the answer was lost. "Retryable" is a
    statement about a read; on a write the same failure is a question about
    what happened.
    """
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return Verdict.AMBIGUOUS
    if isinstance(exc, ConnectionError):
        return Verdict.AMBIGUOUS
    if _marked_retryable(exc):
        return Verdict.AMBIGUOUS
    return _shared_classifier(exc)


def _shared_classifier(exc: BaseException) -> Verdict:
    """The part both classifiers agree on.

    Deliberately conservative: anything this does not recognise is `FATAL`, so
    an unfamiliar exception stops the loop instead of being hammered three
    times. A new retryable failure mode is opted in explicitly — either as a
    type here, or by the raiser setting `retryable = True` on its own exception
    class, which is how `ProviderUnavailableError` says a carrier blip is worth
    another go without this module needing to know what a carrier is.
    """
    if isinstance(exc, ConnectionError | OSError):
        return Verdict.RETRY
    if _marked_retryable(exc):
        return Verdict.RETRY
    return Verdict.FATAL


def _marked_retryable(exc: BaseException) -> bool:
    """Whether the exception's own class says trying again is reasonable."""
    return getattr(exc, "retryable", False) is True


async def call_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = READ_POLICY,
    classify: Classifier = read_classifier,
    name: str = "operation",
    rand: Callable[[], float] = random.random,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run `operation`, retrying it while `classify` says that is safe.

    Args:
        operation: A zero-argument coroutine function. Called once per attempt,
            so it must be a factory rather than an already-created coroutine —
            a coroutine object cannot be awaited twice.
        policy: How many attempts, how long between, how long each may take.
        classify: Turns a failure into a `Verdict`.
        name: What to call this in the log.
        rand: Randomness for the jitter, injected for tests.
        sleep: Sleep function, injected for tests.

    Returns:
        Whatever the operation returned.

    Raises:
        AmbiguousOutcomeError: The classifier said `AMBIGUOUS`. The operation
            may have taken effect; the original error is the `__cause__`.
        Exception: The last failure, when attempts run out or the verdict is
            `FATAL`. Raised unchanged, so callers keep their own error types.
    """
    last: BaseException | None = None

    for attempt in range(1, policy.attempts + 1):
        try:
            if policy.timeout_secs is None:
                return await operation()
            async with asyncio.timeout(policy.timeout_secs):
                return await operation()
        except asyncio.CancelledError:
            # The session is going away. Never retried and never reclassified:
            # cancellation is not a failure of the operation.
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised below, after classification
            last = exc
            verdict = classify(exc)

            if verdict is Verdict.AMBIGUOUS:
                logger.warning(
                    f"RETRY | {name} | attempt {attempt}/{policy.attempts} | AMBIGUOUS "
                    f"({exc.__class__.__name__}) — not retried, the outcome is unknown"
                )
                raise AmbiguousOutcomeError(name, exc) from exc

            if verdict is Verdict.FATAL:
                logger.debug(
                    f"RETRY | {name} | attempt {attempt}/{policy.attempts} | FATAL "
                    f"({exc.__class__.__name__}) — not retried"
                )
                raise

            if attempt >= policy.attempts:
                logger.warning(
                    f"RETRY | {name} | gave up after {attempt} attempt(s) "
                    f"({exc.__class__.__name__}: {exc})"
                )
                raise

            delay = policy.delay_for(attempt, rand=rand)
            logger.info(
                f"RETRY | {name} | attempt {attempt}/{policy.attempts} failed "
                f"({exc.__class__.__name__}) — retrying in {delay:.2f}s"
            )
            await sleep(delay)

    # Unreachable: the loop either returns or raises. Kept so a future edit that
    # breaks that invariant fails loudly rather than returning None.
    raise AssertionError(f"{name}: retry loop ended without a result") from last


async def guarded(
    operation: Callable[[], Awaitable[T]],
    *,
    default: T,
    name: str,
    timeout_secs: float | None = 10.0,
) -> T:
    """Run an operation that must never take its caller down, returning `default` on failure.

    For the writes that happen while somebody is on the phone — recording an
    outcome, storing a record — where the call carrying on matters more than
    the write succeeding, and the failure belongs in the log rather than in the
    caller's control flow.

    Every exception is caught, including an ambiguous one, because there is
    nothing this caller could do differently about it. `asyncio.CancelledError`
    is re-raised: the session ending is not a failure to swallow.
    """
    try:
        if timeout_secs is None:
            return await operation()
        async with asyncio.timeout(timeout_secs):
            return await operation()
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - the whole point of this helper
        logger.opt(exception=not isinstance(exc, TimeoutError)).warning(
            f"GUARDED | {name} failed ({exc.__class__.__name__}: {exc}); continuing"
        )
        return default


def describe_policies() -> str:
    """The active retry policies, for the startup report."""
    return (
        f"reads: {READ_POLICY.describe()} | "
        f"database: {DATABASE_POLICY.describe()} | "
        f"call placement: {NEVER_RETRY.describe()}"
    )


def is_ambiguous(exc: BaseException) -> bool:
    """Whether `exc` means an operation's outcome is unknown.

    One place to ask, so a caller does not have to remember that both the
    wrapper and a bare timeout can mean it.
    """
    return isinstance(exc, AmbiguousOutcomeError | TimeoutError | asyncio.TimeoutError)


def jittered(seconds: float, *, fraction: float = 0.2, rand: Callable[[], float] = random.random) -> float:
    """Spread a fixed interval so several workers do not fire together.

    Used for polling loops and pacing, where there is no failure to back off
    from but there is still a reason not to synchronise.
    """
    if seconds <= 0 or fraction <= 0:
        return max(seconds, 0.0)
    spread = seconds * fraction
    return max(0.0, seconds - spread / 2 + spread * rand())


__all__ = [
    "DATABASE_POLICY",
    "NEVER_RETRY",
    "READ_POLICY",
    "AmbiguousOutcomeError",
    "Classifier",
    "RetryPolicy",
    "Verdict",
    "call_with_retry",
    "describe_policies",
    "guarded",
    "is_ambiguous",
    "jittered",
    "read_classifier",
    "write_classifier",
]
