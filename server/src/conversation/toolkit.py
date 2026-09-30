"""The boundary every tool call crosses: schema, validation, guard, audit log.

Phase 6 advertised the tools as Pipecat *direct functions* — plain async
functions whose name, typed signature and docstring become the schema, so there
is no hand-written JSON to drift. That is kept. What Phase 7 adds is the layer
the requirement calls "validation / authorization" between the model's request
and the handler, and it is one function: `strict_tool`.

`strict_tool` takes a direct function and returns a `FunctionSchema` carrying
the *same* schema Pipecat would have derived, plus a handler that runs before
the function does:

1. **The arguments are checked against the schema.** Required arguments must be
   present; each value is coerced to its declared type where that is safe (the
   string `"3"` for an integer) and refused where it is not; arguments the
   schema does not know are dropped and logged. Anything refused becomes a
   structured `invalid_arguments` failure with a message naming the problem —
   which is what lets a small model correct itself, where Pipecat's own
   behaviour on a bad call is a `TypeError` and the generic sentence "the
   function failed and returned no result".
2. **The function is guarded.** An exception inside it becomes an
   `internal_error` failure the model can act on, and a traceback in the log.
   A tool that returns without reporting anything is reported as a failure too,
   because a call that never settles blocks the model until Pipecat's timeout.
3. **Every call is logged, once, in one shape.** Tool name, session and call
   ids, prospect and attempt ids, the arguments, success or the error code, the
   elapsed time, and a one-line summary of the result. Argument values whose
   key looks like a credential are redacted before logging; nothing else in a
   tool's arguments is secret, and a reviewer needs to see what the model asked
   for.

The direct-function shape is preserved for the tests, which invoke the handler
exactly as Pipecat does — `handler(params)` with `params.arguments` set — so
what they exercise is this boundary and not a copy of it.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger
from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import (
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.services.llm_service import FunctionCallParams

from src.spoken_text import TOOL_REPLY_METADATA, SpokenTextScrubber

from .results import INTERNAL_ERROR, INVALID_ARGUMENTS, ActionRecord, ToolResult

# Argument names that must never appear in a log line with their value.
# Defensive: no tool here takes one, but the log format outlives the tool list.
_SECRET_KEY = re.compile(r"(token|secret|password|api[_-]?key|authorization)", re.IGNORECASE)

_MAX_LOGGED_VALUE = 80


@dataclass
class AuditContext:
    """Who and what a tool call belongs to, for the log and the call record.

    One per conversation. The ids are whatever the session has — a browser
    session has only a session id, a campaign call has all four — and absent
    ones are simply left out of the log line.

    Attributes:
        actions: Every tool call this session made, in order, for the outcome
            record. Appended by `strict_tool`'s handler.
    """

    session_id: str | None = None
    call_id: str | None = None
    prospect_id: int | None = None
    call_attempt_id: int | None = None
    actions: list[ActionRecord] = field(default_factory=list)

    def describe(self) -> str:
        """The id fragment of a log line."""
        parts = []
        if self.session_id:
            parts.append(f"session={self.session_id}")
        if self.call_id:
            parts.append(f"call={self.call_id}")
        if self.prospect_id is not None:
            parts.append(f"prospect={self.prospect_id}")
        if self.call_attempt_id is not None:
            parts.append(f"attempt={self.call_attempt_id}")
        return " ".join(parts) if parts else "session=anonymous"


ToolFunction = Callable[..., Awaitable[ToolResult | None]]
"""A direct function that either returns a `ToolResult` or reports one itself.

Returning is the normal shape. A tool that must do something *after* its result
has been delivered — `end_call` pushes the end frame behind the goodbye it just
enabled — calls `params.result_callback(result.to_dict())` itself and returns
None; the guard notices the callback was used and does not report twice.
"""


#: How long a released tool waits for the response's end frame to reach the
#: spoken-text filter before falling back to the second request. The end
#: frame is pushed by the LLM service right after it hands the tool calls off,
#: so this is milliseconds in practice; the ceiling is for a pipeline that is
#: wedged, where the old behaviour is the safe one.
RESPONSE_END_WAIT_SECS = 2.0

#: The argument a releasing tool carries its reply in. Phase 43. The model
#: on Cerebras (qwen-3.8) answers a recording call with the call alone — no
#: text in the same response — so the Phase 32 release never applied and every
#: recording turn paid a second LLM request (measured 2026-09-23: caller heard
#: the reply after 4.08 s on tool turns against 2.14 s on plain ones). With the
#: reply in the call, the wrapper speaks it and the turn is released.
SAY_ARGUMENT = "say"


def strict_tool(
    function: ToolFunction,
    *,
    audit: AuditContext,
    advertise: Callable[[], list[FunctionSchema]] | None = None,
    release: Callable[[ToolResult], bool] | None = None,
    speech: Callable[[], Any] | None = None,
) -> FunctionSchema:
    """Wrap a direct function in the validation, guard and audit boundary.

    Args:
        function: An async function whose first parameter is `params`, in the
            shape Pipecat's direct functions take. Its signature and docstring
            become the schema, exactly as they would unwrapped.
        audit: Where to log against and record into.
        advertise: Phase 31. Returns the tools the *next* request should
            describe to the model. Called after the tool has run — the stage
            may have moved — and set on the request's context before the
            result is delivered, so the request that answers the result sees
            the set for the new stage. None leaves the context's tools alone.
        release: Phase 32. Given the tool's result, says whether the reply
            the model has *already* spoken in the same response is the whole
            reply — a recording tool whose result changes nothing the caller
            needs to hear. When it says so, and the model did speak in that
            response, and it was the response's only tool call, the result is
            delivered with ``run_llm=False`` and no second LLM request is
            made. In every other case — the model called without speaking,
            spoke but the result needs a follow-up (a no that must be closed),
            several tool calls in one response, no tally, a failure — the
            result is delivered as before and the second request runs. None
            never releases: the tool's result is needed before speaking.
        speech: Phase 32. Returns the session's `SpeechTally` (or None), the
            record of what the model has said in the current response.

    Returns:
        A `FunctionSchema` to list in `LLMContext(tools=[...])`. Pipecat
        registers the handler it carries by itself.
    """
    wrapper = DirectFunctionWrapper(function)
    schema = wrapper.to_function_schema()
    name = schema.name
    properties = schema.properties
    required = list(schema.required)
    # `**kwargs`-style parameters cannot be described to a model and would make
    # the argument check meaningless, so they are refused at definition time.
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            raise TypeError(f"tool {name} must not take *args or **kwargs")

    async def handler(params: FunctionCallParams) -> None:
        started = time.monotonic()
        raw = params.arguments
        arguments, problems, ignored = validate_arguments(raw, properties, required)
        if ignored:
            logger.warning(f"TOOL | {name} | {audit.describe()} | ignored unknown argument(s): {', '.join(ignored)}")

        if problems:
            result = ToolResult.fail(
                INVALID_ARGUMENTS,
                "; ".join(problems),
                guidance=INVALID_ARGUMENTS_GUIDANCE,
                data={"expected": _expected(properties, required)},
            )
            _log(name, audit, raw, result, started)
            await params.result_callback(result.to_dict())
            return

        reported: list[Any] = []

        async def capture(result: Any, *args: Any, **kwargs: Any) -> None:
            reported.append(result)
            await params.result_callback(result, *args, **kwargs)

        guarded = dataclasses.replace(params, arguments=arguments, result_callback=capture)

        try:
            returned = await function(guarded, **arguments)
        except Exception as exc:  # noqa: BLE001 - the whole point is to report it
            _refresh_advertised(params, advertise, name)
            logger.exception(f"TOOL | {name} | {audit.describe()} | raised")
            result = ToolResult.fail(
                INTERNAL_ERROR,
                f"{name} failed: {exc.__class__.__name__}",
                guidance=INTERNAL_ERROR_GUIDANCE,
            )
            _log(name, audit, arguments, result, started)
            if not reported:
                await params.result_callback(result.to_dict())
            return

        _refresh_advertised(params, advertise, name)

        if isinstance(returned, ToolResult):
            result = returned
            _log(name, audit, arguments, result, started)
            verdict = await _turn_verdict(name, result, release, speech)
            if verdict is _Verdict.SPOKEN:
                logger.info(f"TOOL | {name} | reply already spoken in the same response; no second LLM request")
                await params.result_callback(result.to_dict(), properties=FunctionCallResultProperties(run_llm=False))
                return
            reply = _speakable_reply(arguments.get(SAY_ARGUMENT)) if verdict is _Verdict.SILENT else ""
            llm = getattr(params, "llm", None)
            if reply and llm is not None:
                # Phase 43: the reply travelled in the call. The result goes
                # first, so the context reads call, result, reply; the reply
                # then takes the ordinary path — spoken-text filter, TTS,
                # transcript — as if the model had streamed it.
                await params.result_callback(result.to_dict(), properties=FunctionCallResultProperties(run_llm=False))
                await _speak_tool_reply(llm, name, reply)
                logger.info(f"TOOL | {name} | reply carried in the call ({len(reply)} chars); no second LLM request")
                return
            if verdict is _Verdict.SILENT:
                logger.debug(f"TOOL | {name} | the model called without speaking and carried no reply; the second request runs")
            await params.result_callback(result.to_dict())
            return

        if reported:
            # The tool delivered its own result. Log what it delivered.
            delivered = reported[-1]
            result = _as_result(delivered)
            _log(name, audit, arguments, result, started)
            return

        result = ToolResult.fail(
            INTERNAL_ERROR,
            f"{name} returned without a result",
            guidance=INTERNAL_ERROR_GUIDANCE,
        )
        _log(name, audit, arguments, result, started)
        await params.result_callback(result.to_dict())

    handler.__name__ = f"{name}_handler"
    handler.__qualname__ = handler.__name__
    return FunctionSchema(
        name=name,
        description=schema.description,
        properties=properties,
        required=required,
        handler=handler,
    )


class _Verdict(Enum):
    """What the tally says about the response a releasing tool was called in."""

    SECOND_REQUEST = "second request"  # as before: Pipecat runs the LLM again
    SPOKEN = "spoken"  # the model spoke in the same response: that reply is the turn
    SILENT = "silent"  # releasable, one call, but the model said nothing


async def _turn_verdict(
    name: str,
    result: ToolResult,
    release: Callable[[ToolResult], bool] | None,
    speech: Callable[[], Any] | None,
) -> _Verdict:
    """Whether this result may end the turn without a second LLM request. Phase 32.

    ``SECOND_REQUEST`` means "as before": Pipecat runs the LLM again with the
    result in context. ``SPOKEN`` only when every condition holds — see
    `strict_tool`'s ``release``. ``SILENT`` is the Phase 43 case: every
    condition but the speech holds, so a reply carried in the call may be
    spoken instead of running the LLM again. Never raises: the result is what
    matters, and the second request is the safe default.
    """
    if release is None or speech is None:
        return _Verdict.SECOND_REQUEST
    try:
        if not release(result):
            return _Verdict.SECOND_REQUEST
        tally = speech()
        if tally is None:
            return _Verdict.SECOND_REQUEST
        response = tally.current_response
        if response == 0:
            return _Verdict.SECOND_REQUEST
        if not await tally.wait_ended(response, RESPONSE_END_WAIT_SECS):
            logger.debug(f"TOOL | {name} | response {response} has not ended; the second request runs")
            return _Verdict.SECOND_REQUEST
        calls = tally.calls_in(response)
        if calls != 1:
            logger.debug(f"TOOL | {name} | {calls} tool call(s) in response {response}; the second request runs")
            return _Verdict.SECOND_REQUEST
        if not tally.spoke_in(response):
            return _Verdict.SILENT
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception(f"TOOL | {name} | could not decide whether the turn is complete; the second request runs")
        return _Verdict.SECOND_REQUEST
    return _Verdict.SPOKEN


async def _turn_properties(
    name: str,
    result: ToolResult,
    release: Callable[[ToolResult], bool] | None,
    speech: Callable[[], Any] | None,
) -> FunctionCallResultProperties | None:
    """The Phase 32 answer alone: ``run_llm=False`` when the model already spoke, else None."""
    verdict = await _turn_verdict(name, result, release, speech)
    if verdict is _Verdict.SILENT:
        logger.debug(f"TOOL | {name} | the model called without speaking; the second request runs")
    return FunctionCallResultProperties(run_llm=False) if verdict is _Verdict.SPOKEN else None


def _speakable_reply(text: Any) -> str:
    """The ``say`` argument as it will be spoken, or "" when nothing of it can be.

    A reply that the spoken-text filter would reduce to nothing — markup, a
    tool's own words, whitespace — must not release the turn, or the caller
    would hear silence where the second request used to answer.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    scrubber = SpokenTextScrubber()
    spoken = scrubber.feed(text) + scrubber.finish()
    return text.strip() if spoken.strip() else ""


async def _speak_tool_reply(llm: Any, name: str, reply: str) -> None:
    """Push the reply carried in a call as if the model had streamed it. Phase 43.

    Start, text, end — the same three frames a response is made of — pushed
    from the LLM service so they take the ordinary path: the spoken-text
    filter (which scrubs it and adds any read-back owed), the TTS, the
    transport, and the assistant aggregator, which records it in the context
    after the tool result it follows. The LLM service stamps ``skip_tts`` on
    them exactly as it does on its own frames, so a text-mode eval stays
    silent. Each frame carries `TOOL_REPLY_METADATA` so a latency tracker can
    tell it from a request of its own.
    """
    for frame in (LLMFullResponseStartFrame(), LLMTextFrame(text=reply), LLMFullResponseEndFrame()):
        frame.metadata[TOOL_REPLY_METADATA] = name
        await llm.push_frame(frame)


def _refresh_advertised(
    params: FunctionCallParams, advertise: Callable[[], list[FunctionSchema]] | None, name: str
) -> None:
    """Put the tools the next request should describe on the request's context.

    Never raises: the tool's result is what matters, and a bookkeeping failure
    here must not turn a recorded fact into an error the model has to handle.
    """
    if advertise is None:
        return
    context = getattr(params, "context", None)
    if context is None or not hasattr(context, "set_tools"):
        return
    try:
        context.set_tools(advertise())
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception(f"TOOL | {name} | could not refresh the advertised tools")


def validate_arguments(
    raw: Any, properties: Mapping[str, Any], required: list[str]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Check the model's arguments against a schema.

    Args:
        raw: Whatever the model sent. Usually a dict; occasionally not.
        properties: The schema's `properties`, as Pipecat derives them.
        required: The schema's `required` list.

    Returns:
        `(arguments, problems, ignored)`: the cleaned arguments to call with,
        the problems found (empty means valid), and the unknown names dropped.
        An argument sent as `null` or an empty string for a *required*
        parameter is a missing argument; for an optional one it is simply left
        to the default.
    """
    if not isinstance(raw, Mapping):
        return {}, [f"arguments must be an object, not {type(raw).__name__}"], []

    cleaned: dict[str, Any] = {}
    problems: list[str] = []
    ignored: list[str] = []

    for key, value in raw.items():
        if key not in properties:
            ignored.append(str(key))
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue  # Treated as absent; required-ness is checked below.
        coerced, problem = _coerce(key, value, properties[key])
        if problem:
            problems.append(problem)
        else:
            cleaned[key] = coerced

    for key in required:
        if key not in cleaned and not any(p.startswith(f"{key} ") for p in problems):
            problems.append(f"{key} is required")

    return cleaned, problems, ignored


def _coerce(key: str, value: Any, spec: Mapping[str, Any]) -> tuple[Any, str | None]:
    """Coerce one value to its declared type, or explain why it cannot be."""
    kind = spec.get("type")
    if kind == "string":
        if isinstance(value, str):
            return value.strip(), None
        if isinstance(value, (int, float, bool)):
            return str(value), None
        return None, f"{key} must be text"
    if kind == "integer":
        if isinstance(value, bool):
            return None, f"{key} must be a whole number"
        if isinstance(value, int):
            return value, None
        if isinstance(value, float) and value.is_integer():
            return int(value), None
        if isinstance(value, str):
            try:
                return int(value.strip()), None
            except ValueError:
                return None, f"{key} must be a whole number, not {value!r}"
        return None, f"{key} must be a whole number"
    if kind == "number":
        if isinstance(value, bool):
            return None, f"{key} must be a number"
        if isinstance(value, (int, float)):
            return float(value), None
        if isinstance(value, str):
            try:
                return float(value.strip()), None
            except ValueError:
                return None, f"{key} must be a number, not {value!r}"
        return None, f"{key} must be a number"
    if kind == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
            return value.strip().lower() in ("true", "yes"), None
        return None, f"{key} must be true or false"
    if kind == "array":
        if isinstance(value, (list, tuple)):
            return list(value), None
        return None, f"{key} must be a list"
    if kind == "object":
        if isinstance(value, Mapping):
            return dict(value), None
        return None, f"{key} must be an object"
    return value, None


def _expected(properties: Mapping[str, Any], required: list[str]) -> dict[str, str]:
    """The schema in one line per argument, for the model to correct itself from."""
    return {
        name: f"{spec.get('type', 'any')}{' (required)' if name in required else ''}"
        for name, spec in properties.items()
    }


def _as_result(delivered: Any) -> ToolResult:
    """Read a result a tool delivered itself back into a `ToolResult`, for the log."""
    if isinstance(delivered, ToolResult):
        return delivered
    if isinstance(delivered, Mapping):
        success = bool(delivered.get("success", True))
        return ToolResult(
            success=success,
            data=dict(delivered.get("data") or {}) if success else None,
            error_code=delivered.get("error_code") if not success else None,
            message=delivered.get("message"),
            guidance=str(delivered.get("guidance") or ""),
        )
    return ToolResult.ok({"result": str(delivered)[:_MAX_LOGGED_VALUE]})


def _log(name: str, audit: AuditContext, arguments: Any, result: ToolResult, started: float) -> None:
    """One line per tool call, and one entry in the call's action record."""
    elapsed_ms = (time.monotonic() - started) * 1000
    summary = _summarize(result.data) if result.success else (result.message or "")
    verdict = "ok" if result.success else f"FAIL {result.error_code}"
    line = (
        f"TOOL | {name} | {audit.describe()} | args={_render_arguments(arguments)} | "
        f"{verdict} in {elapsed_ms:.0f}ms"
    )
    if summary:
        line += f" | {summary}"
    (logger.info if result.success else logger.warning)(line)
    audit.actions.append(
        ActionRecord(
            tool=name,
            success=result.success,
            error_code=result.error_code,
            summary=summary[:200],
            extra={"elapsed_ms": round(elapsed_ms)},
        )
    )


def _render_arguments(arguments: Any) -> str:
    """Arguments as compact JSON, with anything credential-shaped redacted."""
    if not isinstance(arguments, Mapping):
        return repr(arguments)[:_MAX_LOGGED_VALUE]
    shown = {
        str(key): ("***" if _SECRET_KEY.search(str(key)) else _clip(value))
        for key, value in arguments.items()
    }
    try:
        return json.dumps(shown, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(shown)[:_MAX_LOGGED_VALUE * 4]


def _summarize(data: Mapping[str, Any] | None) -> str:
    """The result's data as `key=value` pairs, lists and dicts by size."""
    if not data:
        return ""
    parts = []
    for key, value in data.items():
        if isinstance(value, (list, tuple, set)):
            parts.append(f"{key}[{len(value)}]")
        elif isinstance(value, Mapping):
            parts.append(f"{key}{{{len(value)}}}")
        else:
            parts.append(f"{key}={_clip(value)}")
    return " ".join(parts)


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_LOGGED_VALUE:
        return value[: _MAX_LOGGED_VALUE - 1] + "…"
    return value


INVALID_ARGUMENTS_GUIDANCE = (
    "That tool call was rejected because its arguments were not in the expected form; the message"
    " says what was wrong and `data.expected` lists the arguments. Nothing has happened. Fix the"
    " arguments and call it again, or if you do not have the information, ask the person for it."
    " Do not tell them anything was done."
)

INTERNAL_ERROR_GUIDANCE = (
    "That tool failed and nothing has happened. Do not tell them it worked. Say plainly that you"
    " could not do that just now and offer to have a colleague follow up, then carry on."
)
