"""The Stage 6 action risk policy `s6-risk/1` (docs/v2/stage6-design.md §6.3).

Trusted, versioned configuration - a Guard input, applied as the Guard's last
step once every prerequisite has passed:

    create_return      REQUIRE_APPROVAL   risk_policy_requires_approval
    create_exchange    ALLOW              risk_policy_allows
    escalate_to_human  ALLOW              risk_policy_allows

This is Stage 6's simulated business risk policy, not a statement about real
e-commerce practice. There is no amount threshold: the published policy corpus
has no structured amount rule. It is deliberately not in policy_sources/ (the
frozen Stage 4/5 corpus). There is no runtime override parameter; the only way
to require more approval is a new, stricter version of this policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .actions import ACTION_NAMES, CREATE_EXCHANGE, CREATE_RETURN, ESCALATE_TO_HUMAN

RISK_POLICY_VERSION = "s6-risk/1"

RISK_ALLOW = "ALLOW"
RISK_REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
RISK_DISPOSITIONS = (RISK_ALLOW, RISK_REQUIRE_APPROVAL)


@dataclass(frozen=True)
class ActionRiskPolicy:
    """action name -> the disposition once every prerequisite passes."""

    version: str
    dispositions: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("ActionRiskPolicy.version must be a non-empty string")
        if not isinstance(self.dispositions, Mapping) or set(self.dispositions) != set(ACTION_NAMES):
            raise ValueError("ActionRiskPolicy must cover exactly the Stage 6 actions")
        if not all(value in RISK_DISPOSITIONS for value in self.dispositions.values()):
            raise ValueError("ActionRiskPolicy dispositions must be ALLOW or REQUIRE_APPROVAL")
        object.__setattr__(self, "dispositions", MappingProxyType(dict(self.dispositions)))

    def disposition(self, action_name: str) -> str:
        return self.dispositions[action_name]


S6_RISK_POLICY = ActionRiskPolicy(
    version=RISK_POLICY_VERSION,
    dispositions={
        CREATE_RETURN: RISK_REQUIRE_APPROVAL,
        CREATE_EXCHANGE: RISK_ALLOW,
        ESCALATE_TO_HUMAN: RISK_ALLOW,
    },
)
