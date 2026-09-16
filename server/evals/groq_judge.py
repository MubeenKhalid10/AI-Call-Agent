"""A Groq-backed judge for the eval harness.

The harness ships two judges out of the box: a local Ollama model, and OpenAI.
This project has neither — Ollama is not installed and there is no OpenAI key —
but it does have a Groq key, and Groq is both free and fast enough that a judged
scenario costs nothing and finishes quickly.

The harness supports exactly this: `judge.eval.factory` takes a dotted path to a
callable that receives the `judge.eval` config and returns any Pipecat LLM
service. Scenarios reference it as::

    judge:
      eval:
        factory: evals.groq_judge.make_judge

For that dotted path to resolve, the harness must run with `server/` on
`sys.path` — which is why `evals/README.md` invokes it as
`uv run python -m pipecat.evals` rather than through the `pipecat` console
script, whose `sys.path[0]` is the script's own directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from pipecat.services.groq.llm import GroqLLMService

# The harness runs in its own process, separate from the bot, so it has not
# loaded the project's `.env` for us. Read the one value the judge needs from
# the file rather than loading the file into the environment: the harness
# spawns the bots, and a bot spawned after the first judged run would inherit
# everything `load_dotenv` put here — including the deployment's `SALES_*`,
# which `evals/eval_env.py` then keeps over the sample configuration because
# a value already in the environment is taken to be the operator's choice.
# Observed 2026-09-11: with `-r 2`, attempt 1 introduced Meridian and attempt
# 2 introduced the deployment's company.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Judging is a short classification over a couple of sentences, so the same
# model the agent itself uses is more than enough — and it is the one already
# verified to work on this account.
DEFAULT_JUDGE_MODEL = "qwen/qwen3.8-27b"

# The bot and the judge share one Groq organisation, and the free tier's limit
# is per model per minute. A judge on the agent's own model spends the agent's
# minute: on 2026-09-11 a tool turn was refused (input tokens per minute) while
# the judge was reading the turn before it, and one judge call was itself
# rate-limited. `EVAL_JUDGE_MODEL` in the shell moves the judge to a model with
# its own budget without touching the scenarios; a scenario's `model` still wins.
JUDGE_MODEL_VAR = "EVAL_JUDGE_MODEL"

# The harness caps a verdict at 200 tokens, and a reasoning model that spends
# them thinking returns nothing. `EVAL_JUDGE_MAX_TOKENS` raises the floor for
# such a model (gpt-oss needs about 900); unset, the harness's cap stands.
JUDGE_MAX_TOKENS_VAR = "EVAL_JUDGE_MAX_TOKENS"


class _GroqJudge(GroqLLMService):
    """A Groq service whose inference honours a minimum token budget for the verdict."""

    def __init__(self, *args, min_tokens: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._min_tokens = min_tokens

    async def run_inference(self, *args, max_tokens=None, **kwargs):
        if self._min_tokens and (max_tokens is None or max_tokens < self._min_tokens):
            max_tokens = self._min_tokens
        return await super().run_inference(*args, max_tokens=max_tokens, **kwargs)


def make_judge(config: dict) -> GroqLLMService:
    """Build the judge LLM from a scenario's `judge.eval` config.

    Args:
        config: The scenario's `judge.eval` mapping. `model` overrides the
            default; `extra` is forwarded verbatim as request parameters.

    Returns:
        A Groq LLM service the harness can run inference against.
    """
    api_key = (os.getenv("GROQ_API_KEY") or dotenv_values(ENV_FILE).get("GROQ_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set, so the eval judge cannot start. "
            "Scenarios with no `eval:` criteria do not need a judge and will still run."
        )

    model = config.get("model") or os.getenv(JUDGE_MODEL_VAR, "").strip() or DEFAULT_JUDGE_MODEL
    min_tokens = int(os.getenv(JUDGE_MAX_TOKENS_VAR, "0") or 0)
    return _GroqJudge(
        api_key=api_key,
        min_tokens=min_tokens,
        settings=GroqLLMService.Settings(
            model=model,
            extra=config.get("extra") or {},
        ),
    )
