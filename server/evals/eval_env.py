"""Run the bot for an eval with the SAMPLE configuration the scenarios were written against.

    uv run python -m evals.eval_env bot.py -t eval --port 7861

The sales scenarios, their judge criteria and the sample knowledge base in
`evals/kb` all describe one company — Meridian Fleet Systems, the values in
`.env.example`. A deployment's `.env` describes whoever the deployment sells
for, and `load_dotenv(override=True)` means it wins over the shell. So an
eval bot started plainly would introduce the deployment's company while
answering questions from Meridian's handbook, and a judge told the handbook's
facts would fail correct answers. Observed 2026-09-10.

This wrapper runs the same `bot.py` with the sample `SALES_*` and
`DEV_PROSPECT_*` values applied *after* `.env` is loaded — the eval's fixture,
not a change to the deployment — by patching `dotenv.load_dotenv` for this
process. Nothing else is touched: keys, providers, the database and the
calendar are the deployment's own. `evals/sales/suite.yaml` spawns its bots
through it.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

import dotenv

SERVER = Path(__file__).resolve().parent.parent
EXAMPLE = SERVER / ".env.example"
# The sample prospect is commented out in `.env.example` (a deployment must
# not greet strangers by a fixture's name); the eval wants it.
SAMPLE_PREFIXES = ("SALES_", "DEV_PROSPECT_")


def sample_settings() -> dict[str, str]:
    """The `SALES_*` and `DEV_PROSPECT_*` values of `.env.example`, commented or not."""
    settings: dict[str, str] = {}
    for raw in EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("# "):
            line = line[2:].strip()
        if "=" not in line or not line.startswith(SAMPLE_PREFIXES):
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith(SAMPLE_PREFIXES) and name.isidentifier():
            settings[name] = value.strip()
    return settings


def shell_settings() -> dict[str, str]:
    """Sample variables already set in the shell — they beat `.env.example`.

    The wrapper is imported before the bot loads `.env`, so what is in the
    environment here came from the shell. The one that matters is
    `DEV_PROSPECT_EMAIL`: Cal.com refuses an attendee whose domain cannot
    receive mail (`example.com` gets HTTP 400 `email_domain_cannot_receive_mail`,
    observed 2026-09-10), so a booking eval needs a real address of the
    deployment's own, and that does not belong in a template.
    """
    return {name: value for name, value in os.environ.items() if name.startswith(SAMPLE_PREFIXES)}


OVERRIDES = {**sample_settings(), **shell_settings()}
_real_load = dotenv.load_dotenv


def _load_then_override(*args, **kwargs):
    result = _real_load(*args, **kwargs)
    os.environ.update(OVERRIDES)
    return result


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m evals.eval_env <bot.py> [bot arguments...]")
    script = Path(sys.argv[1])
    if not script.is_absolute():
        script = SERVER / script
    dotenv.load_dotenv = _load_then_override
    os.environ.update(OVERRIDES)
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(SERVER))
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
