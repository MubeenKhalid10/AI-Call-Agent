"""SignalWire, which is Twilio's API with a different company behind it.

SignalWire's *Compatibility API* is a deliberate reimplementation of Twilio's:
the same REST paths under `/api/laml`, the same `Twiml` parameter carrying the
same `<Connect><Stream>` markup, the same call status words, the same error
numbering, and — the part that matters most here — the same Media Streams
websocket protocol, down to `event`, `streamSid`, `callSid` and
`customParameters`, defaulting to the same 8kHz μ-law.

So this provider is `TwilioProvider` pointed somewhere else. What it overrides:

* **`api_base`** — your SignalWire *space*, plus `/api/laml`. There is no single
  shared host the way there is for Twilio; every account gets its own
  subdomain, which is why `SIGNALWIRE_SPACE_URL` is required and has no default.
* **The account identifier** — SignalWire calls it a Project ID and it is a
  UUID rather than an `AC…` SID. It goes in the same place in the URL and the
  same place in HTTP basic auth, so nothing but the name changes.
* **`brand` / `credential_hint` / `error_docs`** — so a failure says
  "SignalWire" and names the SignalWire environment variables, rather than
  sending you to a Twilio account you do not have.

Nothing else. `place_call`, `fetch_call`, `hang_up`, the TwiML, the status
mapping, the error translation and the serializer are all inherited, and the
bot does not know the difference: Pipecat detects a SignalWire media stream as
`twilio` because that is genuinely what it is speaking.

**Why this exists.** Twilio does not offer trial accounts in every country —
Pakistan among them — which makes the reference implementation untestable for
the person building this. SignalWire's trial needs no card, hands you a phone
number, and lets you call one verified number, which is exactly the shape of a
development test. It is also the cheapest possible demonstration that the
provider abstraction is real: a whole second carrier in one small file.
"""

from __future__ import annotations

import aiohttp

from .base import TelephonyError
from .twilio import TwilioProvider


class SignalWireProvider(TwilioProvider):
    """Places outbound calls through SignalWire's Twilio-compatible API."""

    name = "signalwire"
    # Not `("signalwire",)`. This names the *wire protocol* Pipecat detects, and
    # a SignalWire media stream is indistinguishable from a Twilio one — which
    # is the whole point of the compatibility API.
    transports = ("twilio",)

    brand = "SignalWire"
    credential_hint = "SIGNALWIRE_PROJECT_ID and SIGNALWIRE_API_TOKEN"
    error_docs = "https://signalwire.com/docs/compatibility-api (error code {code})"

    # Phase 14. SignalWire signs a delivery with Twilio's algorithm — its own
    # SDK's `RequestValidator` delegates to Twilio's for form-encoded bodies —
    # but keys it with a *signing key* from the dashboard's API credentials
    # page, not with the API token, and puts it in its own header. The Twilio
    # header is accepted second because compatibility deployments have been
    # seen sending both.
    signature_headers = ("x-signalwire-signature", "x-twilio-signature")
    webhook_secret_hint = "SIGNALWIRE_SIGNING_KEY"

    def __init__(
        self,
        project_id: str,
        api_token: str,
        space_url: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout_secs: float = 20.0,
        signing_key: str | None = None,
    ) -> None:
        """Create the provider.

        Args:
            project_id: SignalWire Project ID — a UUID, used everywhere Twilio
                uses the account SID.
            api_token: SignalWire API token. Never logged.
            space_url: Your space, e.g. `example.signalwire.com` or the full
                `https://example.signalwire.com`. Both are accepted because both
                are what the dashboard shows you at different moments.
            session: An existing HTTP session to use. When omitted, one is
                created on first use and closed by `close()`.
            timeout_secs: Ceiling on one HTTP request. See `TwilioProvider`.
            signing_key: Phase 14. The webhook signing key from the API
                credentials page. `None` means deliveries cannot be verified
                and are refused; the API token is deliberately *not* used in
                its place, because it is not what SignalWire signs with.
        """
        super().__init__(
            account_sid=project_id,
            auth_token=api_token,
            api_base=space_api_base(space_url),
            session=session,
            timeout_secs=timeout_secs,
            # An empty string, not None: `TwilioProvider` reads None as "use
            # the auth token", which is right for Twilio and wrong here.
            webhook_secret=signing_key or "",
        )
        self._space = space_url

    def describe(self) -> str:
        """One line for the startup log. Shows the space, never the token."""
        return f"signalwire (space {self._space}, project …{self._account_sid[-4:]})"


def space_api_base(space_url: str) -> str:
    """Turn a SignalWire space into the API root its Compatibility API lives at.

    Everything the dashboard might hand you is accepted, because the value gets
    copied by hand and a wrong one fails as an authentication error that says
    nothing about the URL::

        example.signalwire.com                  -> https://example.signalwire.com/api/laml
        https://example.signalwire.com          -> https://example.signalwire.com/api/laml
        https://example.signalwire.com/         -> https://example.signalwire.com/api/laml
        https://example.signalwire.com/api/laml -> https://example.signalwire.com/api/laml

    The result deliberately stops before `/2010-04-01`: `TwilioProvider` appends
    the version for its own requests, and Pipecat's serializer appends it for
    the hang-up, so both need the host and path *without* it.

    Raises:
        TelephonyError: `space_url` is empty or has no hostname.
    """
    candidate = (space_url or "").strip().rstrip("/")
    if not candidate:
        raise TelephonyError(
            "SIGNALWIRE_SPACE_URL is not set. It is the address of your SignalWire space, "
            "shown at the top of the dashboard — something like example.signalwire.com."
        )

    if "//" not in candidate:
        candidate = f"https://{candidate}"

    if candidate.endswith("/api/laml"):
        return candidate

    # Any other path is almost certainly a mis-paste — a console deep link, say —
    # so keep the host and drop the rest. Appending `/api/laml` to it instead
    # would build a URL that 404s, and a 404 from an API root reads as a
    # credentials problem rather than as the typo it is.
    scheme, _, rest = candidate.partition("//")
    host = rest.split("/", 1)[0]
    if not host:
        raise TelephonyError(
            f"SIGNALWIRE_SPACE_URL is {space_url!r}, which has no hostname in it. "
            f"Use something like example.signalwire.com."
        )
    return f"{scheme}//{host}/api/laml"
