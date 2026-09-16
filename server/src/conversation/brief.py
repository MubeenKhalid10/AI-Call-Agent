"""Who the agent is calling, and on whose behalf — the facts, and only the facts.

Three plain data objects, none of which knows anything about databases,
carriers or pipelines:

* `ProspectBrief` — the person. Every field is optional and an absent field
  means *we do not know*, never "there is nothing there".
* `CampaignBrief` — what we are calling about: our own identity, the offer, the
  value points, and the next step we are asking for.
* `CallBrief` — the two together, plus the ids that tie this conversation back
  to a call attempt in the campaign database.

**The rule this module exists to enforce is in `ProspectBrief.render`.** Unknown
fields are not silently omitted from the prompt — they are listed, by name,
under a heading that says the agent does not know them. That is deliberate and
it is the difference between an agent that stays quiet about a missing job title
and one that invents "I saw you're the operations lead". A model reading a
prompt with `company` simply missing has no way to tell "not supplied" from
"not applicable", and fills the gap; a model reading "Not known: their company,
their job title" has been told, in the same breath, both that it does not know
and that saying so is expected.

The renderer is also why this is a separate module from the conversation state:
what the agent may say about the prospect is a property of the *data*, and
should not change because the call moved from discovery to objection handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Campaign `configuration` keys this layer reads. A campaign's configuration is
# a free-form JSONB column from Phase 5; these are the entries that mean
# something to the conversation. Anything else in there is left alone.
CONFIG_OFFER = "offer"
CONFIG_VALUE_POINTS = "value_points"
CONFIG_MEETING_ASK = "meeting_ask"
CONFIG_COMPANY = "company_name"
# Phase 28: the basic facts about the company — what it is and does, and the
# services it offers — as campaign context the agent always has, whether or
# not a knowledge base retrieval finds anything.
CONFIG_COMPANY_DESCRIPTION = "company_description"
CONFIG_SERVICES = "services"
CONFIG_AGENT_NAME = "agent_name"
CONFIG_QUALIFICATION = "qualification_criteria"
CONFIG_NOTES = "notes"


@dataclass(frozen=True)
class ProspectBrief:
    """What is known about the person on the other end of the call.

    Every field defaults to `None` or empty, and that is a load-bearing default
    rather than a convenience: an anonymous brief is the correct brief for an
    inbound call, a browser session, or an outbound call whose prospect id could
    not be resolved, and it renders into a prompt that tells the agent it knows
    nothing about who it is speaking to.

    Attributes:
        prospect_id: The campaign database's id, when this call came from a
            campaign. Carried so the do-not-call action can name the row.
        notes: Prior notes about this person, from earlier calls or the import.
            Rendered verbatim; nothing here summarises or embellishes them.
    """

    prospect_id: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    job_title: str | None = None
    industry: str | None = None
    location: str | None = None
    phone: str | None = None
    email: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_anonymous(self) -> bool:
        """Whether we know nothing at all about who we are calling."""
        return not any((self.first_name, self.last_name, self.company, self.job_title))

    @property
    def display_name(self) -> str | None:
        """The name to greet them by, or None if we do not have one.

        First name only. A cold call that opens with a full name sounds like a
        list being read, which is exactly what the prospect suspects and exactly
        what the opening has ten seconds to disprove.
        """
        return self.first_name or None

    def render(self) -> str:
        """The prospect block for the system instruction.

        Known fields are listed as facts. Unknown ones are listed *by name* as
        not known, followed by the rule about them — see the module docstring
        for why the absent fields are spelled out rather than omitted.
        """
        known: list[str] = []
        unknown: list[str] = []

        for label, value in (
            ("First name", self.first_name),
            ("Last name", self.last_name),
            ("Company", self.company),
            ("Job title", self.job_title),
            ("Industry", self.industry),
            ("Location", self.location),
        ):
            if value:
                known.append(f"- {label}: {value}")
            else:
                unknown.append(label.lower())

        lines = ["WHO YOU ARE CALLING"]
        if known:
            lines.extend(known)
        else:
            lines.append("- Nothing at all. You have a phone number and no other details.")

        if self.notes:
            lines.append("- Earlier notes about this person:")
            lines.extend(f"    - {note}" for note in self.notes)
        else:
            unknown.append("any earlier notes or history with them")

        if unknown:
            lines.append("")
            lines.append(f"NOT KNOWN: {', '.join(unknown)}.")
        lines.append(
            "You have no other information about this person. Never claim or imply anything"
            " not listed above: not that they downloaded something, visited a site, filled in a"
            " form, spoke to a colleague, or were referred. If you need an unknown detail, ask."
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class CampaignBrief:
    """What we are calling about, and who we say we are.

    Populated from a campaign's `configuration` JSON when the call came from a
    campaign, and from environment defaults otherwise, so a browser session
    behaves like a real call rather than like a different product.

    Attributes:
        company_name: The business the agent is calling on behalf of. Empty is
            allowed and is handled honestly rather than papered over: the
            opening simply does not name a company, because inventing one would
            be the single worst thing this agent could say.
        value_points: Short claims the agent may make about what we do. The
            agent is told these are the *only* claims it may make, which is
            what stops it inventing features.
        meeting_ask: The next step to ask for, in the words a person would use.
        company_description: Phase 28. One or two plain sentences on what the
            company is and does — the answer to "what do you actually do?",
            which a prospect asks in the first minute and which must not
            depend on a vector search finding the right passage.
        services: Phase 28. The services the company offers, as short names.
            The detail behind them stays in the knowledge base; this is the
            list the agent can name without looking anything up.
    """

    agent_name: str = ""
    company_name: str = ""
    company_description: str = ""
    services: list[str] = field(default_factory=list)
    campaign_name: str | None = None
    offer: str = ""
    value_points: list[str] = field(default_factory=list)
    qualification_criteria: list[str] = field(default_factory=list)
    meeting_ask: str = ""
    notes: list[str] = field(default_factory=list)
    #: Phase 19. Sentences the opening must contain, in order — an AI
    #: disclosure, a recording disclosure — as the compliance policy for
    #: this call resolved them. Words, not policy: the conversation layer
    #: does not know why they are required, only that they are.
    disclosures: list[str] = field(default_factory=list)

    @classmethod
    def from_configuration(
        cls, configuration: dict[str, Any] | None, *, defaults: CampaignBrief
    ) -> CampaignBrief:
        """Overlay a campaign's `configuration` onto the environment defaults.

        Per-field, not all-or-nothing: a campaign that sets only `offer` keeps
        the configured agent name and company from `.env`, because those are
        properties of the business and would be identical in every campaign row.

        Unrecognised keys are ignored, and a `configuration` that is not a dict
        (or is absent) leaves the defaults untouched — a campaign created before
        this phase existed has an empty `{}` there and must still be callable.
        """
        if not isinstance(configuration, dict):
            return defaults

        # Phase 28: the company facts travel with the company name. A campaign
        # that names a *different* company than the environment's default must
        # not inherit that default's description or services — they describe
        # another business — so it gets only what its own configuration says.
        # A campaign that names the same company, or none, inherits them like
        # every other field.
        configured_company = _text(configuration.get(CONFIG_COMPANY))
        same_company = (
            not configured_company
            or configured_company.casefold() == defaults.company_name.casefold()
        )
        inherited_description = defaults.company_description if same_company else ""
        inherited_services = list(defaults.services) if same_company else []

        return cls(
            agent_name=_text(configuration.get(CONFIG_AGENT_NAME)) or defaults.agent_name,
            company_name=configured_company or defaults.company_name,
            company_description=(
                _text(configuration.get(CONFIG_COMPANY_DESCRIPTION)) or inherited_description
            ),
            services=_lines(configuration.get(CONFIG_SERVICES)) or inherited_services,
            campaign_name=defaults.campaign_name,
            offer=_text(configuration.get(CONFIG_OFFER)) or defaults.offer,
            value_points=_lines(configuration.get(CONFIG_VALUE_POINTS)) or defaults.value_points,
            qualification_criteria=(
                _lines(configuration.get(CONFIG_QUALIFICATION)) or defaults.qualification_criteria
            ),
            meeting_ask=_text(configuration.get(CONFIG_MEETING_ASK)) or defaults.meeting_ask,
            notes=_lines(configuration.get(CONFIG_NOTES)) or defaults.notes,
            # Never from the campaign JSON directly: the disclosures come
            # from the resolved compliance policy, which the briefing puts
            # on the defaults it hands in.
            disclosures=list(defaults.disclosures),
        )

    def render(self) -> str:
        """The campaign block for the system instruction.

        Phase 28 made the top of it the always-available campaign context: who
        the agent is (name, AI assistant, company), what the company is and
        does, the services, and the purpose of the call — followed by three
        rules on how to use it. It is deliberately compact (a few lines, no
        detail) because it is sent on every request; the detail stays in the
        knowledge base and arrives per turn through retrieval.
        """
        lines = ["WHY YOU ARE CALLING"]
        if self.company_name:
            who = f"You are {self.agent_name}, an AI assistant" if self.agent_name else "You are an AI assistant"
            lines.append(f"- {who} calling on behalf of {self.company_name}.")
            if self.company_description:
                lines.append(f"- About {self.company_name}: {self.company_description}")
            if self.services:
                lines.append(f"- Services: {'; '.join(self.services)}.")
        else:
            # Not a silent omission: the agent is told the gap exists, so it
            # introduces itself without a company rather than filling one in.
            lines.append(
                "- You have NOT been told which company you are calling for."
                " Introduce yourself by name only and never invent a company name."
            )
        if self.offer:
            lines.append(f"- What you are calling about: {self.offer}")
        purpose = "- The purpose of this call: find out whether they might need what we do"
        if self.qualification_criteria:
            purpose += " (what makes somebody a fit is listed below)"
        # `rstrip` because a meeting ask written as a sentence ends in a full
        # stop already, and "specialist.." reads badly.
        purpose += (
            f" and, if so, ask for {self.meeting_ask.rstrip('.')}."
            if self.meeting_ask
            else " and, if so, agree a next step."
        )
        lines.append(purpose)
        if self.value_points:
            lines.append("- The only claims you may make about what we do:")
            lines.extend(f"    - {point}" for point in self.value_points)
        else:
            lines.append(
                "- You have no approved claims about the product. Do not describe features,"
                " results or pricing; ask questions and offer to have a specialist explain."
            )
        if self.qualification_criteria:
            lines.append("- What makes somebody a fit:")
            lines.extend(f"    - {point}" for point in self.qualification_criteria)
        if self.meeting_ask:
            lines.append(f"- The next step to ask for: {self.meeting_ask}")
        if self.notes:
            lines.append("- Campaign notes:")
            lines.extend(f"    - {note}" for note in self.notes)
        if self.company_name:
            # Phase 28: how to use the facts above. Three rules, in the same
            # breath as the facts: the basics need no lookup; anything beyond
            # them comes from the knowledge base or is honestly unknown; an
            # unrelated question gets a brief honest answer and a steer back.
            lines.append(
                f"- Basic questions - who you are, what {self.company_name} does, what it offers -"
                " you answer from the facts above in your own words; nothing else is needed for them."
            )
            lines.append(
                "- Any company fact beyond these comes from the knowledge base. If it is not there"
                " either, do not invent it: say naturally you are not certain and offer to have the"
                " team confirm or follow up."
            )
            lines.append(
                "- Something general or off-topic: answer it naturally and briefly, then steer back to"
                " why you called. Invent no company facts, but do not answer ordinary chat with"
                " \"I don't have that information\"."
            )
        if self.disclosures:
            lines.append(
                "- REQUIRED DISCLOSURES: your very first sentence on this call must include,"
                " in these words or very close to them:"
            )
            lines.extend(f'    - "{sentence}"' for sentence in self.disclosures)
            lines.append(
                "  Say them before anything else, whatever the person says first, and never"
                " deny or soften them later."
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class CallBrief:
    """Everything the conversation layer knows before the first word is spoken.

    Attributes:
        campaign_id / call_attempt_id: The campaign database rows this call
            belongs to, when it came from one. They are carried, not used, by
            the conversation itself — the sink uses them to write the outcome
            back, which is what keeps the conversation layer free of any
            knowledge of those tables.
        source: Where the identity came from — `"campaign"`, `"environment"` or
            `"none"`. Logged at the start of the call, because "the agent
            greeted somebody by the wrong name" and "the agent had no idea who
            it was calling" are diagnosed in completely different places.
    """

    prospect: ProspectBrief = field(default_factory=ProspectBrief)
    campaign: CampaignBrief = field(default_factory=CampaignBrief)
    campaign_id: int | None = None
    call_attempt_id: int | None = None
    source: str = "none"

    @property
    def prospect_id(self) -> int | None:
        """The prospect's database id, when this call came from a campaign."""
        return self.prospect.prospect_id

    def describe(self) -> str:
        """One line for the log at the start of the session."""
        who = self.prospect.display_name or "unknown caller"
        if self.prospect.company:
            who = f"{who} at {self.prospect.company}"
        ids = []
        if self.prospect.prospect_id is not None:
            ids.append(f"prospect={self.prospect.prospect_id}")
        if self.campaign_id is not None:
            ids.append(f"campaign={self.campaign_id}")
        if self.call_attempt_id is not None:
            ids.append(f"attempt={self.call_attempt_id}")
        suffix = f" | {' '.join(ids)}" if ids else ""
        return f"{who} | via {self.source}{suffix}"


def _text(value: Any) -> str:
    """A trimmed string, or empty for anything that is not usable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _lines(value: Any) -> list[str]:
    """Normalise a list, or a delimited string, into a list of non-empty lines.

    Accepts both shapes because a campaign's configuration is edited by hand as
    JSON (where a list is natural) and the same settings arrive from `.env` as
    one string (where a separator is the only option).
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("\n", "|").split("|")]
        return [part for part in parts if part]
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value if _text(item)]
    return []
