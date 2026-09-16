#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the telephony layer that need no phone, no carrier and no keys.

Run it from the `server/` directory::

    uv run python tests/test_telephony.py

**What this can and cannot prove.** A phone call has three parts, and only two
of them can be tested without dialling one:

* *Placing the call* — the request sent to the carrier, the TwiML in it, and
  what happens when the carrier says no. Fully covered here, against a stub HTTP
  session that records what it was asked and replies with real Twilio response
  bodies.
* *Interpreting the call* — mapping the carrier's status words onto outcomes,
  and reading the media stream's handshake back into a `CallSession`. Fully
  covered here.
* *The audio* — that a real call's μ-law reaches the STT and the reply reaches
  the caller. Not covered here and not coverable here; it needs a carrier
  streaming real audio. `docs` in README.md describes the manual call that
  checks it, and the eval suite covers the pipeline behind it.

So a pass here means every decision this project makes about telephony is
correct, and says nothing about whether Twilio is up. That is the right split:
these run in under a second and never charge anyone for a call.

It is a plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

# Set before importing config: nothing here reaches a vendor, but `Config`
# validates that the keys exist before it will build. Real values already in the
# environment are left alone.
for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"  # No database is opened here.

from src.config import Config, ConfigError, TelephonyConfig  # noqa: E402
from src.telephony import (  # noqa: E402
    PARAM_DIRECTION,
    PARAM_FROM,
    PARAM_TO,
    CallRequest,
    CallSession,
    CallSetupError,
    CallStatus,
    ProviderUnavailableError,
    TelephonyError,
    build_stream_twiml,
    make_provider,
    stream_url,
)
from src.telephony.signalwire import SignalWireProvider, space_api_base  # noqa: E402
from src.telephony.transport import _unauthenticated_serializer  # noqa: E402
from src.telephony.twilio import TwilioProvider  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def check_raises(label: str, exception_type, call, *, contains: str = "") -> None:
    """Assert that `call` raises, and that its message mentions `contains`."""
    try:
        call()
    except exception_type as exc:
        text = str(exc)
        check(label, contains in text, f"message was {text[:120]!r}" if contains else "")
    except Exception as exc:  # noqa: BLE001 - the wrong exception type is the failure
        check(label, False, f"raised {exc.__class__.__name__}: {exc}")
    else:
        check(label, False, "did not raise")


# --- A stub for Twilio's REST API -------------------------------------------


class StubResponse:
    """One canned HTTP response, shaped like `aiohttp`'s."""

    def __init__(self, status: int, body) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class StubSession:
    """An `aiohttp.ClientSession` stand-in that records requests and replays replies.

    The provider under test is the real provider — only the socket is replaced,
    so the URL it builds, the form fields it sends and the way it reads a
    response are all exercised exactly as they would be against Twilio.
    """

    def __init__(self, responses: list[StubResponse]) -> None:
        self._responses = list(responses)
        # (method, url, {"data": form fields, "params": query string, "timeout": ...}).
        # Phase 9 widened this from the form fields alone: the query parameters
        # are what `find_recent_calls` is built out of, and the timeout is what
        # stops a placement hanging, so both have to be assertable.
        self.requests: list[tuple[str, str, dict]] = []
        self.closed = False

    def request(self, method, url, auth=None, data=None, params=None, timeout=None):
        # Phase 14: the body may be a list of pairs, because a status callback
        # asks for its events as a repeated field. `data` keeps the last value
        # per name, as before; `pairs` keeps every field in order.
        pairs = list(data.items()) if hasattr(data, "items") else list(data or [])
        self.requests.append(
            (
                method,
                url,
                {"data": dict(pairs), "pairs": pairs, "params": dict(params or {}), "timeout": timeout},
            )
        )
        self.auth = auth
        if not self._responses:
            raise AssertionError(f"stub had no response left for {method} {url}")
        return self._responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        """`session.get`, for the health checks. Same recording, same replay."""
        self.requests.append((
            "GET", url, {"data": {}, "params": {}, "timeout": timeout, "headers": dict(headers or {})}
        ))
        if not self._responses:
            raise AssertionError(f"stub had no response left for GET {url}")
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


def _sent(record: tuple[str, str, dict]) -> tuple[str, str, dict]:
    """Unpack a recorded request into (method, url, form fields).

    Phase 9 widened what `StubSession` records — query parameters and the
    timeout matter now — so the form fields moved under a key. This keeps the
    checks that only care about the body reading the way they did.
    """
    method, url, sent = record
    return method, url, sent["data"]


class CallDataStub(dict):
    """Pipecat's `CallData`, as far as the serializer builders touch it.

    They read it both ways — `call_data["stream_id"]` and `call_data.get(...)` —
    which a plain dict already does, so this only exists to be explicit about
    what is being stood in for.
    """


def call_resource(**overrides) -> dict:
    """A Twilio Call resource, in the shape the real API returns one."""
    body = {
        "sid": "CA00000000000000000000000000000001",
        "status": "queued",
        "to": "+923001234567",
        "from": "+15550001111",
        "duration": None,
        "error_code": None,
        "error_message": None,
    }
    body.update(overrides)
    return body


def make_twilio(responses: list[StubResponse]) -> tuple[TwilioProvider, StubSession]:
    """A real `TwilioProvider` wired to a stub session."""
    session = StubSession(responses)
    provider = TwilioProvider("ACtestsid00000000000000000000abcd", "secret-token", session=session)
    return provider, session


def sample_request(**overrides) -> CallRequest:
    """A representative outbound call."""
    fields = {
        "to_number": "+923001234567",
        "from_number": "+15550001111",
        "stream_url": "wss://abc123.ngrok.app/ws",
        "answer_timeout_secs": 25,
        "parameters": {
            PARAM_DIRECTION: "outbound",
            PARAM_FROM: "+15550001111",
            PARAM_TO: "+923001234567",
        },
    }
    fields.update(overrides)
    return CallRequest(**fields)


# --- Checks -----------------------------------------------------------------


def check_stream_urls() -> None:
    """The one value that has to match between the tunnel, the bot and the carrier."""
    print("\n=== stream URL derivation ===")
    for given, expected in (
        ("https://abc123.ngrok.app", "wss://abc123.ngrok.app/ws"),
        ("https://abc123.ngrok.app/", "wss://abc123.ngrok.app/ws"),
        ("http://abc123.ngrok.app", "ws://abc123.ngrok.app/ws"),
        ("abc123.ngrok.app", "wss://abc123.ngrok.app/ws"),
        ("wss://abc123.ngrok.app/ws", "wss://abc123.ngrok.app/ws"),
        ("https://abc123.ngrok.app/ws", "wss://abc123.ngrok.app/ws"),
        ("  https://abc123.ngrok.app  ", "wss://abc123.ngrok.app/ws"),
    ):
        actual = stream_url(given)
        check(f"{given.strip()!r:<40} -> {expected}", actual == expected, actual)

    check("custom path is honoured", stream_url("https://h", "/calls") == "wss://h/calls")
    check_raises(
        "an empty public URL explains itself",
        TelephonyError,
        lambda: stream_url(""),
        contains="TELEPHONY_PUBLIC_URL",
    )


def check_twiml() -> None:
    """The markup that connects a call to the bot, and the parameters riding on it."""
    print("\n=== TwiML ===")
    twiml = build_stream_twiml(sample_request())

    # `<Connect>` is what makes the stream two-way. `<Start>` only forks a copy
    # of the caller's audio to us, which produces a bot that hears everything
    # and cannot be heard — and looks fine in the logs.
    check("uses <Connect>, not <Start>", "<Connect>" in twiml and "<Start>" not in twiml)
    check("streams to the given URL", 'url="wss://abc123.ngrok.app/ws"' in twiml)
    check("under Twilio's 4000-character limit", len(twiml) < 4000, f"{len(twiml)} chars")

    # These three parameters are the only thing an outbound call can tell the
    # bot about itself; `session.py` reads them back at the other end.
    for name in (PARAM_DIRECTION, PARAM_FROM, PARAM_TO):
        check(f"carries the {name} parameter", f'name="{name}"' in twiml)

    hostile = build_stream_twiml(
        sample_request(parameters={"note": 'a & b <c> "d"'}, stream_url="wss://h/ws?a=1&b=2")
    )
    check("escapes parameter values", "&amp;" in hostile and "<c>" not in hostile)
    check("escapes the URL's ampersand", 'url="wss://h/ws?a=1&amp;b=2"' in hostile)


def check_call_status() -> None:
    """Outcomes have to be distinguishable, or every failure looks the same."""
    print("\n=== call outcomes ===")
    for status, final, reached in (
        (CallStatus.QUEUED, False, False),
        (CallStatus.RINGING, False, False),
        (CallStatus.ANSWERED, False, True),
        (CallStatus.COMPLETED, True, True),
        (CallStatus.BUSY, True, False),
        (CallStatus.NO_ANSWER, True, False),
        (CallStatus.FAILED, True, False),
        (CallStatus.CANCELED, True, False),
        # An unrecognised status must not be mistaken for an ending: a polling
        # loop that treats it as final stops watching a call that is still up.
        (CallStatus.UNKNOWN, False, False),
    ):
        ok = status.is_final is final and status.reached_person is reached
        check(f"{status.value:<10} final={final} reached={reached}", ok)
        check(f"{status.value:<10} explains itself", bool(status.explain()))


async def check_place_call() -> None:
    """What actually goes over the wire when a call is placed."""
    print("\n=== placing a call ===")
    provider, session = make_twilio([StubResponse(201, call_resource())])
    snapshot = await provider.place_call(sample_request())

    method, url, data = _sent(session.requests[0])
    check("POSTs to the Calls collection", method == "POST" and url.endswith("/Calls.json"), url)
    check("uses the account in the path", "/Accounts/ACtestsid00000000000000000000abcd/" in url)
    check("sends To", data.get("To") == "+923001234567", str(data.get("To")))
    check("sends From", data.get("From") == "+15550001111", str(data.get("From")))
    check("sends inline TwiML, not a webhook URL", "Twiml" in data and "Url" not in data)
    check("sends the answer timeout", data.get("Timeout") == "25", str(data.get("Timeout")))
    check("never sends the auth token as a field", "secret-token" not in json.dumps(data))

    check("returns the call id", snapshot.call_id.startswith("CA"), snapshot.call_id)
    check("maps 'queued'", snapshot.status is CallStatus.QUEUED)
    check("keeps the raw payload", snapshot.raw.get("sid") == snapshot.call_id)
    check("describes itself without secrets", "secret-token" not in snapshot.describe())

    # The provider must not close a session it was handed.
    await provider.close()
    check("leaves a caller-owned session open", session.closed is False)


async def check_status_mapping() -> None:
    """Every Twilio status word, mapped onto an outcome."""
    print("\n=== reading a call's status ===")
    for twilio_status, expected, duration in (
        ("queued", CallStatus.QUEUED, None),
        ("initiated", CallStatus.QUEUED, None),
        ("ringing", CallStatus.RINGING, None),
        ("in-progress", CallStatus.ANSWERED, None),
        ("completed", CallStatus.COMPLETED, "37"),
        ("busy", CallStatus.BUSY, "0"),
        ("no-answer", CallStatus.NO_ANSWER, "0"),
        ("failed", CallStatus.FAILED, None),
        ("canceled", CallStatus.CANCELED, None),
        ("something-new", CallStatus.UNKNOWN, None),
    ):
        provider, session = make_twilio(
            [StubResponse(200, call_resource(status=twilio_status, duration=duration))]
        )
        snapshot = await provider.fetch_call("CA1")
        method, url, _ = _sent(session.requests[0])
        ok = snapshot.status is expected and method == "GET" and url.endswith("/Calls/CA1.json")
        check(f"{twilio_status:<14} -> {expected.value}", ok, snapshot.status.value)

    provider, _ = make_twilio([StubResponse(200, call_resource(status="completed", duration="37"))])
    snapshot = await provider.fetch_call("CA1")
    check("reads the billed duration", snapshot.duration_secs == 37.0, str(snapshot.duration_secs))

    provider, _ = make_twilio(
        [
            StubResponse(
                200,
                call_resource(status="failed", error_code=13224, error_message="Dial: number"),
            )
        ]
    )
    snapshot = await provider.fetch_call("CA1")
    check("keeps the carrier's error", snapshot.error_code == "13224", str(snapshot.error_code))
    check("shows the error when describing", "13224" in snapshot.describe())


async def check_errors() -> None:
    """A refused call has to say which setting to change."""
    print("\n=== when the carrier says no ===")

    cases = (
        (400, 21215, "Geo Permission", "geographic permissions", "a blocked destination country"),
        (400, 21211, "not a valid phone number", "E.164", "a malformed To number"),
        (400, 21210, "not verified", "TELEPHONY_FROM_NUMBER", "an unowned From number"),
        (401, 20003, "Authenticate", "TWILIO_ACCOUNT_SID", "bad credentials"),
        (400, 21205, "Url is not a valid URL", "TELEPHONY_PUBLIC_URL", "an unreachable stream URL"),
    )
    for status, code, message, expected_hint, label in cases:
        provider, _ = make_twilio([StubResponse(status, {"code": code, "message": message})])
        try:
            await provider.place_call(sample_request())
        except CallSetupError as exc:
            text = str(exc)
            check(
                f"{label:<32} names the fix",
                expected_hint in text,
                text.replace("\n", " ")[:110],
            )
            check(f"{label:<32} keeps Twilio's words", message in text)
        else:
            check(f"{label:<32} raises", False, "no exception")

    # An unknown code still has to be actionable: the code and a place to look
    # it up beats a bare HTTP status.
    provider, _ = make_twilio([StubResponse(400, {"code": 99999, "message": "Something new"})])
    try:
        await provider.place_call(sample_request())
    except CallSetupError as exc:
        check("an unknown code points at the docs", "99999" in str(exc) and "docs" in str(exc))
    else:
        check("an unknown code points at the docs", False, "no exception")

    # A 5xx is a different kind of problem: nothing was wrong with the request,
    # so retrying makes sense and the error type says so.
    provider, _ = make_twilio([StubResponse(503, {"message": "Service unavailable"})])
    try:
        await provider.place_call(sample_request())
    except ProviderUnavailableError:
        check("a 5xx is reported as retryable", True)
    except CallSetupError:
        check("a 5xx is reported as retryable", False, "raised CallSetupError instead")

    # Hanging up a call that already ended is the normal race, not a failure.
    provider, _ = make_twilio([StubResponse(404, {"code": 20404, "message": "Not found"})])
    try:
        await provider.hang_up("CA1")
        check("hanging up an ended call is not an error", True)
    except TelephonyError as exc:
        check("hanging up an ended call is not an error", False, str(exc)[:80])

    provider, _ = make_twilio([StubResponse(404, {"code": 20001, "message": "Bad account"})])
    try:
        await provider.hang_up("CA1")
        check("a real 404 still raises", False, "swallowed")
    except CallSetupError:
        check("a real 404 still raises", True)


async def check_transfer() -> None:
    """Moving a live call to a person (Phase 7). No real call is touched."""
    print("\n=== transferring ===")
    from src.telephony import TransferError, build_transfer_twiml, is_e164

    twiml = build_transfer_twiml("+923001234567")
    check("dials the destination", '<Dial timeout="30">+923001234567</Dial>' in twiml, twiml)
    check("with a spoken fallback and a hang-up if nobody answers", "<Say>" in twiml and "<Hangup />" in twiml)
    check("a caller id is passed when given", 'callerId="+15550001111"' in build_transfer_twiml("+923001234567", caller_id="+15550001111"))
    check("a bad destination is refused before the carrier sees it", _raises(lambda: build_transfer_twiml("0300 1234567"), TelephonyError, "E.164"))
    check("is_e164 accepts real shapes", is_e164("+923001234567") and is_e164("+14155552671"))
    check("and refuses the rest", not is_e164("03001234567") and not is_e164("+0123") and not is_e164("") and not is_e164(None))

    provider, session = make_twilio([StubResponse(200, call_resource(status="in-progress"))])
    await provider.transfer_call("CA1", "+923001234567", caller_id="+15550001111")
    method, url, data = _sent(session.requests[0])
    check("POSTs new instructions to the call resource", method == "POST" and url.endswith("/Calls/CA1.json"), url)
    check("as inline TwiML with a Dial", "<Dial" in data.get("Twiml", "") and "+923001234567" in data["Twiml"])
    check("and nothing else — the call is not hung up", "Status" not in data)

    provider, _ = make_twilio([StubResponse(404, {"code": 20404, "message": "The requested resource was not found"})])
    check("a call that has ended is a TransferError saying so", await _araises(provider.transfer_call("CA1", "+923001234567"), TransferError, "already ended"))
    provider, _ = make_twilio([StubResponse(400, {"code": 21220, "message": "Unable to update record: Call is not in-progress"})])
    check("a call no longer in progress likewise", await _araises(provider.transfer_call("CA1", "+923001234567"), TransferError, "already ended"))
    provider, _ = make_twilio([StubResponse(400, {"code": 21215, "message": "Account not authorized to call +92"})])
    check("a refused destination is a TransferError with the fix", await _araises(provider.transfer_call("CA1", "+923001234567"), TransferError, "geographic permissions"))
    provider, session = make_twilio([])
    check("a bad number never reaches the carrier", await _araises(provider.transfer_call("CA1", "not a number"), TransferError, "E.164") and not session.requests)


def _raises(call, exception_type, contains: str = "") -> bool:
    try:
        call()
    except exception_type as exc:
        return contains in str(exc)
    except Exception:  # noqa: BLE001
        return False
    return False


async def _araises(coroutine, exception_type, contains: str = "") -> bool:
    try:
        await coroutine
    except exception_type as exc:
        return contains in str(exc)
    except Exception:  # noqa: BLE001
        return False
    return False


async def check_hang_up() -> None:
    """Ending a call from outside the bot."""
    print("\n=== hanging up ===")
    provider, session = make_twilio([StubResponse(200, call_resource(status="completed"))])
    await provider.hang_up("CA1")
    method, url, data = _sent(session.requests[0])
    check("POSTs to the call resource", method == "POST" and url.endswith("/Calls/CA1.json"), url)
    check("asks for status completed", data.get("Status") == "completed", str(data))


def check_call_session() -> None:
    """Reading the media stream's handshake back into something the bot can log."""
    print("\n=== the bot's view of a call ===")

    # What Pipecat hands the bot for an outbound Twilio call: the ids from the
    # carrier, the numbers from the parameters we attached to the TwiML.
    outbound = SimpleNamespace(
        transport_type="twilio",
        call_data=SimpleNamespace(
            call_id="CA123",
            stream_id="MZ456",
            from_number=None,
            to_number=None,
            body={
                PARAM_DIRECTION: "outbound",
                PARAM_FROM: "+15550001111",
                PARAM_TO: "+923001234567",
            },
        ),
    )
    session = CallSession.from_runner_args(outbound)
    check("recognises a phone call", session is not None)
    assert session is not None
    check("reads the call id", session.call_id == "CA123")
    check("knows the call is outbound", session.is_outbound)
    check("recovers the numbers from the parameters", session.to_number == "+923001234567")
    check("identifies itself in one line", "CA123" in session.describe())

    # A carrier that reports from/to itself (Telnyx, Exotel) needs no parameters.
    inbound = SimpleNamespace(
        transport_type="telnyx",
        call_data=SimpleNamespace(
            call_id="v3:abc",
            stream_id="s1",
            from_number="+923009999999",
            to_number="+15550001111",
            body={},
        ),
    )
    session = CallSession.from_runner_args(inbound)
    assert session is not None
    check("uses the carrier's own numbers", session.from_number == "+923009999999")
    check("defaults to inbound", not session.is_outbound)

    # Anything that is not a phone call must produce None, because that is the
    # single check `bot.py` makes before every telephony-only behaviour.
    for transport in ("webrtc", "eval", "websocket", None):
        args = SimpleNamespace(transport_type=transport, call_data=None)
        check(
            f"{str(transport):<10} is not a phone call", CallSession.from_runner_args(args) is None
        )

    # A carrier that sends a handshake with nothing in it must still produce a
    # usable session rather than an exception during call setup.
    bare = SimpleNamespace(transport_type="plivo", call_data=None)
    session = CallSession.from_runner_args(bare)
    check("survives a handshake with no data", session is not None and bool(session.describe()))


def check_config() -> None:
    """Telephony settings are optional at startup and demanded at the point of use."""
    print("\n=== configuration ===")
    saved = {
        name: os.environ.pop(name, None)
        for name in (
            "TELEPHONY_PROVIDER",
            "TELEPHONY_FROM_NUMBER",
            "TELEPHONY_PUBLIC_URL",
            "TELEPHONY_STREAM_PATH",
            "TELEPHONY_ANSWER_TIMEOUT_SECS",
            "TELEPHONY_DISCONNECT_GRACE_SECS",
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
        )
    }
    try:
        # The case that matters most: no telephony configuration at all must
        # still start a bot. Anything else stops every developer without a
        # Twilio account from running the browser agent.
        config = Config.from_env()
        check("a bot with no carrier still starts", config.telephony.provider == "twilio")
        check("and reports itself as unconfigured", not config.telephony.is_configured)
        check("and says so in the startup line", "not configured" in config.describe())

        check_raises(
            "dialling without credentials names them",
            ConfigError,
            config.telephony.require_outbound,
            contains="TWILIO_ACCOUNT_SID",
        )
        check_raises(
            "dialling without a public URL explains the tunnel",
            ConfigError,
            config.telephony.require_outbound,
            contains="ngrok",
        )
        check_raises(
            "building a provider without credentials fails early",
            ConfigError,
            lambda: make_provider(config.telephony),
            contains="TWILIO_AUTH_TOKEN",
        )

        # Half-configured is the state a person is actually in while setting
        # this up, and the message has to name only what is still missing.
        os.environ["TWILIO_ACCOUNT_SID"] = "ACxxxx"
        os.environ["TWILIO_AUTH_TOKEN"] = "tok"
        os.environ["TELEPHONY_FROM_NUMBER"] = "+15550001111"
        telephony = TelephonyConfig.from_env()
        check("credentials alone are not enough", not telephony.is_configured)
        try:
            telephony.require_outbound()
            check("names only what is missing", False, "did not raise")
        except ConfigError as exc:
            text = str(exc)
            check(
                "names only what is missing",
                "TELEPHONY_PUBLIC_URL" in text and "TWILIO_ACCOUNT_SID" not in text,
                text.replace("\n", " ")[:110],
            )

        os.environ["TELEPHONY_PUBLIC_URL"] = "https://abc123.ngrok.app"
        telephony = TelephonyConfig.from_env()
        check("fully configured", telephony.is_configured)
        check("describes the route", "abc123.ngrok.app/ws" in telephony.describe())
        check("never shows a credential", "tok" not in telephony.describe())
        check("a dropped call is not held open", telephony.disconnect_grace_secs == 0.0)
        provider = make_provider(telephony)
        check("builds the configured provider", provider.name == "twilio")
        check("its description hides the token", "tok" not in provider.describe())

        os.environ["TELEPHONY_PROVIDER"] = "carrier-pigeon"
        check_raises(
            "an unknown provider fails at startup, not at dial time",
            ConfigError,
            TelephonyConfig.from_env,
            contains="carrier-pigeon",
        )
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def check_bot_wiring() -> None:
    """The bot must offer a transport for every carrier, and still offer the old ones."""
    print("\n=== bot wiring ===")
    import inspect

    import bot as bot_module  # imported here so a failure above reports first

    source = inspect.getsource(bot_module.bot)
    check("still serves the browser", '"webrtc"' in source)
    check("still serves the eval harness", '"eval"' in source)
    check("serves every carrier Pipecat can detect", "TELEPHONY_TRANSPORTS" in source)

    params = bot_module._telephony_params()
    check("telephony audio is two-way", params.audio_in_enabled and params.audio_out_enabled)
    # The serializer is filled in later — by the provider when one is configured,
    # otherwise by Pipecat. Setting it here would pre-empt both.
    check("leaves the serializer to be chosen per call", params.serializer is None)


def check_signalwire() -> None:
    """The second carrier: Twilio's API, somewhere else, under other credentials."""
    print("\n=== signalwire ===")
    provider = SignalWireProvider("project-abcd", "PT-secret", "example.signalwire.com")

    check("is its own provider name", provider.name == "signalwire")
    # Not "signalwire": this names the wire protocol Pipecat detects, and a
    # SignalWire media stream *is* a Twilio one.
    check("speaks Twilio's media protocol", provider.transports == ("twilio",))
    check("shows the space, never the token", "example.signalwire.com" in provider.describe())
    check("hides the token", "PT-secret" not in provider.describe())

    url = provider._url("Calls.json")
    check(
        "posts to the space's Compatibility API",
        url
        == "https://example.signalwire.com/api/laml/2010-04-01/Accounts/project-abcd/Calls.json",
        url,
    )

    for given in (
        "example.signalwire.com",
        "https://example.signalwire.com",
        "https://example.signalwire.com/",
        "https://example.signalwire.com/api/laml",
    ):
        base = space_api_base(given)
        check(
            f"space {given!r:<45} -> /api/laml", base == "https://example.signalwire.com/api/laml"
        )

    check_raises(
        "an empty space URL says where to find it",
        TelephonyError,
        lambda: space_api_base(""),
        contains="SIGNALWIRE_SPACE_URL",
    )

    # Errors have to name SignalWire and the SignalWire variables. Sending
    # somebody to a Twilio console they do not have an account on is worse than
    # saying nothing.
    message = provider._explain(401, {"code": 20003, "message": "Authenticate"})
    check("credential errors name SignalWire", "SignalWire" in message and "Twilio" not in message)
    check("and name the right variables", "SIGNALWIRE_PROJECT_ID" in message, message[:90])


def check_serializers() -> None:
    """Who builds the serializer, and why it cannot be left to the framework."""
    print("\n=== serializers ===")
    from pipecat.serializers.twilio import TwilioFrameSerializer

    call_data = {"stream_id": "MZ1", "call_id": "CA1"}

    # This is the check that justifies `src/telephony/transport.py` existing.
    # Pipecat's dev runner builds the serializer with
    # `os.getenv("TWILIO_ACCOUNT_SID", "")`, and the serializer rejects an empty
    # one — so a bot configured for any *other* carrier would not survive
    # answering a call. If this ever stops raising, the override can go.
    try:
        TwilioFrameSerializer(stream_sid="MZ1", call_sid="CA1", account_sid="", auth_token="")
        check("Pipecat's default serializer needs Twilio credentials", False, "did not raise")
    except ValueError:
        check("Pipecat's default serializer needs Twilio credentials", True)

    twilio = TwilioProvider("ACtest", "tok")
    serializer = twilio.make_serializer(call_data)
    check("twilio builds a Twilio serializer", isinstance(serializer, TwilioFrameSerializer))
    check(
        "and hangs up at Twilio",
        serializer._base_url == "https://api.twilio.com",
        str(serializer._base_url),
    )

    signalwire = SignalWireProvider("project-abcd", "PT-secret", "example.signalwire.com")
    serializer = signalwire.make_serializer(call_data)
    check(
        "signalwire reuses the same serializer",
        isinstance(serializer, TwilioFrameSerializer),
    )
    # The whole point: identical wire protocol, different company to hang up at.
    check(
        "but hangs up at SignalWire",
        serializer._base_url == "https://example.signalwire.com/api/laml",
        str(serializer._base_url),
    )

    # A bot with nothing configured still has to answer, or the free way to test
    # the phone path (`tests/fake_carrier.py`, and anybody's first run) does not
    # work. Turning auto hang-up off is what makes a credential-free serializer
    # constructible at all.
    print("\n=== answering with no credentials ===")
    for transport_type, extra in (
        ("twilio", {}),
        ("telnyx", {"outbound_encoding": "PCMU"}),
        ("plivo", {}),
        ("exotel", {}),
    ):
        data = {"stream_id": "s1", "call_id": "c1", **extra}
        serializer = _unauthenticated_serializer(transport_type, CallDataStub(data))
        check(f"{transport_type:<8} answers with no credentials", serializer is not None)

    check(
        "an unrecognised carrier is declined, not guessed",
        _unauthenticated_serializer(
            "carrier-pigeon", CallDataStub({"stream_id": "s", "call_id": "c"})
        )
        is None,
    )


async def main() -> int:
    """Run every check and report."""
    check_stream_urls()
    check_twiml()
    check_call_status()
    await check_place_call()
    await check_status_mapping()
    await check_errors()
    await check_hang_up()
    await check_transfer()
    check_call_session()
    check_config()
    check_signalwire()
    check_serializers()
    check_bot_wiring()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
