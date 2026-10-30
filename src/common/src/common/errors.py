"""Technical description of exceptions for logs and error details."""

import asyncio

import yaml

MAX_LENGTH = 300


def describe_error(exc: BaseException) -> str:
    """Return "Type: message", e.g. "ConnectError: [Errno -2] Name or service not known".

    The root cause is appended when it says more than the exception itself, e.g.
    "ConnectError: All connection attempts failed (ConnectionRefusedError: [Errno 111] ...)".
    YAML errors are reduced to the problem and the position, because the default
    text quotes the source line, which may contain secrets.
    """
    text = _text(exc)
    result = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    root = _root_cause(exc)
    if root is not None and _text(root) != text:
        result += f" ({type(root).__name__}: {_text(root)})"
    result = " ".join(result.split())
    return result if len(result) <= MAX_LENGTH else result[: MAX_LENGTH - 3] + "..."


def _root_cause(exc: BaseException) -> BaseException | None:
    """The deepest cause with a message. Cancellation is not a cause worth reporting."""
    found = None
    seen = {id(exc)}
    cause = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if _text(cause) and not isinstance(cause, asyncio.CancelledError):
            found = cause
        cause = cause.__cause__ or cause.__context__
    return found


def _text(exc: BaseException) -> str:
    if isinstance(exc, yaml.MarkedYAMLError):
        mark = exc.problem_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return f"{exc.problem or exc.context or 'invalid YAML'}{where}"
    return str(exc).strip()
