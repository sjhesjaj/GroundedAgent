"""The Stage 6 Capability Gate (docs/v2/stage6-design.md §4.3).

The deployment whitelist - five read tools and three actions - is the upper
bound. A gate can only shrink it, and a per-run narrowing can only shrink the
gate. Asking for anything outside the bound is a configuration error, never a
silent grant. Raising an approval requirement is not a gate feature: the only
channel for that is a stricter, versioned risk policy (aftersales.action_policy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .action_errors import CapabilityConfigurationError
from .actions import ACTION_NAMES
from .registry import RUNTIME_TOOL_NAMES

DEPLOYMENT_READ_TOOLS = RUNTIME_TOOL_NAMES
DEPLOYMENT_ACTIONS = ACTION_NAMES


@dataclass(frozen=True)
class EffectiveCapabilities:
    """What one run may use. Both tuples keep the deployment order."""

    read_tools: tuple[str, ...]
    actions: tuple[str, ...]


def _subset(requested: Iterable[str] | None, bound: tuple[str, ...], what: str) -> tuple[str, ...]:
    if requested is None:
        return bound
    if isinstance(requested, str):
        raise CapabilityConfigurationError(what + " must be a collection of names, not one string")
    names = tuple(requested)
    if not all(isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise CapabilityConfigurationError(what + " must be distinct names")
    outside = set(names) - set(bound)
    if outside:
        # Configuration names are trusted server text; still, only a count is reported.
        raise CapabilityConfigurationError(
            what + " asks for " + str(len(outside)) + " capability(ies) outside the upper bound")
    return tuple(name for name in bound if name in names)


class CapabilityGate:
    """A static whitelist within the deployment bound. It can only shrink."""

    def __init__(self, *, read_tools: Iterable[str] | None = None,
                 actions: Iterable[str] | None = None) -> None:
        self._static = EffectiveCapabilities(
            read_tools=_subset(read_tools, DEPLOYMENT_READ_TOOLS, "read_tools"),
            actions=_subset(actions, DEPLOYMENT_ACTIONS, "actions"),
        )

    @property
    def static(self) -> EffectiveCapabilities:
        return self._static

    def narrow(self, *, read_tools: Iterable[str] | None = None,
               actions: Iterable[str] | None = None) -> EffectiveCapabilities:
        """The effective set of one run: static ∩ requested, never more."""
        return EffectiveCapabilities(
            read_tools=_subset(read_tools, self._static.read_tools, "read_tools"),
            actions=_subset(actions, self._static.actions, "actions"),
        )
