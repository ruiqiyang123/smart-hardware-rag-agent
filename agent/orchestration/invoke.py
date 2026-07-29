"""Bounded execution for read-only or idempotent orchestration calls."""

import math
import queue
import threading
from typing import Callable, NoReturn, Tuple, TypeVar, cast


T = TypeVar("T")


class OutputValidationError(Exception):
    """Signals a retryable structured-output validation failure."""


class ExecutionCapacityError(RuntimeError):
    """Signals that all bounded execution slots are still occupied."""


_EXECUTION_CAPACITY = 8
_EXECUTION_SLOTS = threading.BoundedSemaphore(_EXECUTION_CAPACITY)
_ACTIVE_THREADS: set[threading.Thread] = set()
_ACTIVE_THREADS_LOCK = threading.Lock()
_RETRYABLE_COMPLETED_ERRORS = (ValueError, TypeError, OutputValidationError)


def _active_execution_count() -> int:
    """Return the number of running workers; intended for diagnostics/tests."""

    with _ACTIVE_THREADS_LOCK:
        return len(_ACTIVE_THREADS)


def _active_execution_threads() -> Tuple[threading.Thread, ...]:
    """Return a stable snapshot of running workers for diagnostics/tests."""

    with _ACTIVE_THREADS_LOCK:
        return tuple(_ACTIVE_THREADS)


def _sanitized_error(error: Exception) -> Exception:
    if isinstance(error, OutputValidationError):
        return OutputValidationError("调用输出校验失败")
    if isinstance(error, TimeoutError):
        return TimeoutError("调用超时")
    if isinstance(error, ValueError):
        return ValueError("调用值校验失败")
    if isinstance(error, TypeError):
        return TypeError("调用类型校验失败")
    try:
        return type(error)("调用失败")
    except Exception:
        try:
            sanitized = BaseException.__new__(type(error))
            BaseException.__init__(sanitized, "调用失败")
            return cast(Exception, sanitized)
        except Exception:
            return RuntimeError("调用失败")


def _raise_sanitized(error: Exception) -> NoReturn:
    raise _sanitized_error(error) from None


def _release_worker(thread: threading.Thread) -> None:
    with _ACTIVE_THREADS_LOCK:
        _EXECUTION_SLOTS.release()
        _ACTIVE_THREADS.discard(thread)


def _invoke_once(
    function: Callable[[], T], timeout_seconds: float
) -> tuple[str, object]:
    if not _EXECUTION_SLOTS.acquire(blocking=False):
        raise ExecutionCapacityError("执行容量暂时不可用") from None

    results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
    worker: threading.Thread

    def run() -> None:
        try:
            try:
                results.put_nowait((True, function()))
            except Exception as error:
                results.put_nowait((False, error))
        finally:
            _release_worker(threading.current_thread())

    worker = threading.Thread(
        target=run,
        name="keyguard-bounded-invoke",
        daemon=True,
    )
    with _ACTIVE_THREADS_LOCK:
        _ACTIVE_THREADS.add(worker)
    start_error: Exception | None = None
    try:
        worker.start()
    except Exception as error:
        start_error = error
    if start_error is not None:
        _release_worker(worker)
        _raise_sanitized(start_error)

    try:
        succeeded, value = results.get(timeout=timeout_seconds)
    except queue.Empty:
        return "timeout", None
    return ("success" if succeeded else "error"), value


def _validate_policy(
    function: object, timeout_seconds: object, retries: object
) -> None:
    if not callable(function):
        raise ValueError("function 必须可调用")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds 必须是有限正数")
    if (
        isinstance(retries, bool)
        or not isinstance(retries, int)
        or retries < 0
    ):
        raise ValueError("retries 必须是非负整数")


def invoke_with_policy(
    function: Callable[[], T],
    timeout_seconds: float,
    retries: int,
) -> T:
    """Run a no-argument call with a timeout and finite, selective retries.

    Python threads cannot be forcefully terminated. A timed-out daemon worker
    may therefore keep running after this function returns and is never
    retried. The global capacity bound makes abandoned workers fail closed
    instead of growing without limit. Use this wrapper only for read-only,
    side-effect-free calls or operations protected by idempotency keys.
    """

    _validate_policy(function, timeout_seconds, retries)
    for attempt in range(retries + 1):
        outcome, value = _invoke_once(function, timeout_seconds)
        if outcome == "success":
            return cast(T, value)
        if outcome == "timeout":
            _raise_sanitized(TimeoutError("调用超时"))
        if not isinstance(value, Exception):
            _raise_sanitized(RuntimeError("调用失败"))
        if isinstance(value, _RETRYABLE_COMPLETED_ERRORS) and attempt < retries:
            continue
        _raise_sanitized(value)
    raise RuntimeError("调用失败")
