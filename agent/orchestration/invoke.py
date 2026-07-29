"""Bounded execution for idempotent orchestration calls."""

import math
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Callable, NoReturn, TypeVar


T = TypeVar("T")


class OutputValidationError(Exception):
    """Signals a retryable structured-output validation failure."""


_RETRYABLE_ERRORS = (TimeoutError, ValueError, TypeError, OutputValidationError)


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
        error.args = ("调用失败",)
        return error


def _raise_sanitized(error: Exception) -> NoReturn:
    sanitized = _sanitized_error(error)
    error.args = ("上游调用失败",)
    if sanitized is error:
        raise error.with_traceback(None)
    raise sanitized from error


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

    Python threads cannot be forcefully terminated. A timed-out function may
    therefore keep running after this function returns. Use this wrapper only
    for side-effect-free calls or operations protected by idempotency keys.
    """

    _validate_policy(function, timeout_seconds, retries)
    for attempt in range(retries + 1):
        executor = ThreadPoolExecutor(max_workers=1)
        future = None
        try:
            future = executor.submit(function)
            return future.result(timeout=timeout_seconds)
        except _RETRYABLE_ERRORS as error:
            if attempt == retries:
                _raise_sanitized(error)
        except Exception as error:
            _raise_sanitized(error)
        finally:
            if future is not None:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError("调用失败")
