"""Outbound calling compliance and safety controls. Phase 19.

Three modules, in the order a call meets them:

* `policy` — `CompliancePolicy`, the operator-configured rules for a call
  (window, attempt ceiling, retry delays, disclosures), built from the
  environment, a campaign's `configuration["compliance"]`, and the
  jurisdiction of the number (`COMPLIANCE_JURISDICTIONS`); `PolicyResolver`
  does the layering.
* `dnc` — `DncEntry` / `DncSource`, one row per number that must never be
  dialled, with when, why and who.
* `gate` — `ComplianceGate`, the one check before every outbound call: the
  list, the person, the campaign's rules under the policy, the window; and
  the audit row each decision leaves.

`config.py` imports `policy` and `dnc` (both pure) at module level, and
`src/reliability/health.py` imports `config.py` — so `gate`, which imports
`src/reliability/`, is exported from here **lazily** (`__getattr__`), and
importing this package from `config.py` pulls in nothing that imports
`config.py` back. `src/campaigns/` imports all three. The conversation
layer never imports this package: the disclosures it must speak arrive on
the `CallBrief`, as words.

**What this package is not.** Legal advice, or a claim that a deployment
is compliant with anything. It applies the rules the operator configured
and records that it did. `COMPLIANCE.md` says which controls exist and
which policies the operator must decide.
"""

from __future__ import annotations

from typing import Any

from .dnc import DncEntry, DncSource, parse_source
from .policy import (
    CONFIG_KEY,
    DEFAULT_AI_DISCLOSURE,
    DEFAULT_RECORDING_DISCLOSURE,
    MAX_ATTEMPTS_CEILING,
    OVERLAY_KEYS,
    CompliancePolicy,
    Disclosure,
    PolicyResolver,
    RetryDelays,
    parse_jurisdictions,
)

_GATE_NAMES = ("PROCESS_ROLE", "ComplianceDecision", "ComplianceGate", "Verdict")


def __getattr__(name: str) -> Any:
    """`ComplianceGate` and friends, imported on first use (see the module docstring)."""
    if name in _GATE_NAMES:
        from . import gate

        return getattr(gate, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CONFIG_KEY",
    "DEFAULT_AI_DISCLOSURE",
    "DEFAULT_RECORDING_DISCLOSURE",
    "MAX_ATTEMPTS_CEILING",
    "OVERLAY_KEYS",
    "PROCESS_ROLE",
    "ComplianceDecision",
    "ComplianceGate",
    "CompliancePolicy",
    "Disclosure",
    "DncEntry",
    "DncSource",
    "PolicyResolver",
    "RetryDelays",
    "Verdict",
    "parse_jurisdictions",
    "parse_source",
]
