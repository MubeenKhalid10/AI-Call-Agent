"""Open and close a TLS websocket to the bot's carrier route. Deployment check only.

    uv run python ../deploy/oracle/check_wss.py <YOUR_HOSTNAME>       # from server/

Proves what a carrier needs before any audio flows: DNS, port 443, the
certificate, Caddy's websocket upgrade and the bot's /ws route. It sends
nothing, so the bot logs one call that never started; that is this check.
"""

import asyncio
import sys

import websockets


async def main(host: str) -> int:
    url = f"wss://{host.removeprefix('https://').removeprefix('wss://').strip('/').removesuffix('/ws')}/ws"
    try:
        async with websockets.connect(url, open_timeout=15):
            print(f"WSS handshake OK  {url}")
            return 0
    except Exception as exc:  # noqa: BLE001 - reported to the person running the check
        print(f"WSS handshake FAILED  {url}\n  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: check_wss.py <YOUR_HOSTNAME>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
