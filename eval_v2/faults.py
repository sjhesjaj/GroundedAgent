"""Deterministic fault injection at the tool-execution boundary (Stage 4.4.1).

Eval only. A case's frozen `initial_state.faults` declarations

    {"tool": ..., "match": {...}, "mode": "error|timeout|malformed", "on_call": N}

are applied by a `FaultInjectingGateway` bound to one `V2CaseRuntime`:

    gateway = FaultInjectingGateway(runtime)
    result = gateway.execute(tool_name, arguments, observation_id="obs-001")

Where the fault happens
    The runtime's registry is never touched. The gateway builds a private
    mirror registry - same names, same specs field for field - in which only
    each handler is wrapped, and every call still goes through the existing
    `aftersales.executor.execute_tool`. The executor keeps doing everything it
    does today: closed arguments, identity, side-effect and read-only guards,
    the ToolResult contract, trace / evidence sanitation, error classification.

    Matching and counting happen inside the wrapped handler, i.e. only for a
    call that passed argument validation and actually reached the handler
    boundary. An invalid call never counts, never consumes a fault, and never
    appears in the ledger.

What a fault is
    The real handler is not run. Nothing executes a tool and then edits its
    ToolResult:

    - error:     raises `InjectedToolError` -> the executor's `tool_error`
    - timeout:   raises the production `ToolTimeout` -> `tool_timeout`, with no
                 waiting of any kind; the ledger records a fixed simulated
                 latency (`DEFAULT_SIMULATED_TIMEOUT_MS`), not elapsed time
    - malformed: returns a value that is not a ToolResult, so the executor's
                 own result check raises `ValueError`; there is no ToolResult

Matching semantics
    `tool` matches exactly; `match` is a subset exact-match on the validated
    arguments (`{}` matches every call of the tool). Each declaration, keyed by
    its list index, has its own counter, advanced only by calls it matches,
    whatever happens to them. It fires once, when its counter equals
    `on_call`. Two or more declarations due on the same call are a
    configuration error (`FaultConfigurationError`); none is picked, and the
    real handler does not run.

All state belongs to the gateway instance, and a runtime accepts exactly one
gateway, so counters never outlive or span a case-run. Nothing here reads a
clock, generates an id, or draws a random number.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from aftersales.errors import ToolTimeout
from aftersales.executor import execute_tool
from aftersales.registry import Handler, ToolRegistry, ToolSpec
from orchestration.contracts import ToolResult

from .runtime import EvalRuntimeError, V2CaseRuntime

# A fixed, simulated figure for the future trace span - never a measured one.
DEFAULT_SIMULATED_TIMEOUT_MS = 30_000

MODE_ERROR = "error"
MODE_TIMEOUT = "timeout"
MODE_MALFORMED = "malformed"
FAULT_MODES = (MODE_ERROR, MODE_TIMEOUT, MODE_MALFORMED)

OUTCOME_DELEGATED = "delegated"
OUTCOME_INJECTED_ERROR = "injected_error"
OUTCOME_INJECTED_TIMEOUT = "injected_timeout"
OUTCOME_INJECTED_MALFORMED = "injected_malformed"
_INJECTED_OUTCOMES = MappingProxyType({
    MODE_ERROR: OUTCOME_INJECTED_ERROR,
    MODE_TIMEOUT: OUTCOME_INJECTED_TIMEOUT,
    MODE_MALFORMED: OUTCOME_INJECTED_MALFORMED,
})
CALL_OUTCOMES = (OUTCOME_DELEGATED,) + tuple(_INJECTED_OUTCOMES.values())

_FAULT_KEYS = frozenset({"tool", "match", "mode", "on_call"})


# --------------------------------------------------------------------------
# Errors and injected values
# --------------------------------------------------------------------------


class FaultConfigurationError(EvalRuntimeError, ValueError):
    """The fault declarations cannot be applied deterministically.

    Also a ValueError on purpose: raised at the handler boundary, it is then
    propagated by the executor like any other contract violation, instead of
    being classified as a `tool_error`. Messages carry fault indices and field
    names only, never a match value.
    """


class InjectedToolError(RuntimeError):
    """An `error` fault. The executor classifies it as an ordinary tool_error."""


class _InjectedMalformedResult:
    """A `malformed` fault's return value. Deliberately not a ToolResult."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<injected malformed tool result>"


# --------------------------------------------------------------------------
# Fault declarations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fault:
    index: int
    tool: str
    match: tuple[tuple[str, str], ...]
    mode: str
    on_call: int

    def matches(self, tool_name: str, arguments: Mapping[str, str]) -> bool:
        return tool_name == self.tool and all(
            name in arguments and arguments[name] == value for name, value in self.match
        )


def _parse_faults(raw: tuple, registry: ToolRegistry) -> tuple[_Fault, ...]:
    """Re-check the declarations the case contract already accepted.

    Defence in depth: the gateway must not depend on every caller having
    validated the case. Errors name the index and the field, never a value.
    """
    faults: list[_Fault] = []
    for index, fault in enumerate(raw):
        where = "faults[" + str(index) + "]"
        if not isinstance(fault, Mapping) or set(fault) != _FAULT_KEYS:
            raise FaultConfigurationError(
                where + " must have exactly the keys " + ", ".join(sorted(_FAULT_KEYS)))
        tool = fault["tool"]
        if not isinstance(tool, str) or tool not in registry:
            raise FaultConfigurationError(where + ".tool is not a registered tool")
        parameters = registry.get(tool).parameter_names
        match = fault["match"]
        if not isinstance(match, Mapping):
            raise FaultConfigurationError(where + ".match must be an object")
        for name in match:
            if not isinstance(name, str) or name not in parameters:
                raise FaultConfigurationError(where + ".match has a key that is not an "
                                              "argument of " + tool)
            if not isinstance(match[name], str) or not match[name].strip():
                raise FaultConfigurationError(where + ".match." + name
                                              + " must be a non-empty string")
        mode = fault["mode"]
        if mode not in FAULT_MODES:
            raise FaultConfigurationError(where + ".mode must be one of "
                                          + ", ".join(FAULT_MODES))
        on_call = fault["on_call"]
        if isinstance(on_call, bool) or not isinstance(on_call, int) or on_call < 1:
            raise FaultConfigurationError(where + ".on_call must be an integer >= 1")
        faults.append(_Fault(index=index, tool=tool, match=tuple(sorted(match.items())),
                             mode=mode, on_call=on_call))

    # Same tool, same match, same on_call: always due together, whatever the
    # calls are. Other overlaps depend on the calls and are caught at run time.
    seen: dict[tuple, int] = {}
    for fault in faults:
        key = (fault.tool, fault.match, fault.on_call)
        if key in seen:
            raise FaultConfigurationError(
                "faults[" + str(seen[key]) + "] and faults[" + str(fault.index)
                + "] are the same declaration and would fire on the same call")
        seen[key] = fault.index
    return tuple(faults)


# --------------------------------------------------------------------------
# The call ledger
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class FaultCallRecord:
    """One call that reached the handler boundary. Structure only.

    No argument value, match value, identity, SQL, evidence, or user text:
    faults are identified by their index, calls by their sequence number and
    the caller-supplied observation_id.

    `matched_faults` lists `(fault_index, matching_call_number)` for every
    declaration this call matched, fired or not. `fault_index`, `fault_mode`
    and `matching_call_number` describe the injected fault and are None on a
    delegated call. `simulated_latency_ms` is set for a timeout only.
    """

    sequence: int
    tool_name: str
    observation_id: str
    outcome: str
    fault_index: int | None
    fault_mode: str | None
    matching_call_number: int | None
    simulated_latency_ms: int | None
    matched_faults: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "tool_name": self.tool_name,
            "observation_id": self.observation_id,
            "outcome": self.outcome,
            "fault_index": self.fault_index,
            "fault_mode": self.fault_mode,
            "matching_call_number": self.matching_call_number,
            "simulated_latency_ms": self.simulated_latency_ms,
            "matched_faults": [list(pair) for pair in self.matched_faults],
        }


# --------------------------------------------------------------------------
# The gateway
# --------------------------------------------------------------------------


def _require_mirror(original: ToolRegistry, mirror: ToolRegistry) -> None:
    """Every spec field but the handler must be identical, in the same order."""
    if original.names() != mirror.names():
        raise EvalRuntimeError("gateway registry does not mirror the runtime registry")
    for real, wrapped in zip(original, mirror):
        if dataclasses.replace(wrapped, handler=real.handler) != real:
            raise EvalRuntimeError(real.name + " spec changed in the gateway registry")
        if wrapped.handler is real.handler:
            raise EvalRuntimeError(real.name + " handler is not wrapped")


class FaultInjectingGateway:
    """The controlled tool-execution boundary of one case-run.

    Use it for every tool call of the case-run, faults or not: with no faults
    it behaves exactly like `execute_observation`.
    """

    def __init__(self, runtime: V2CaseRuntime) -> None:
        if not isinstance(runtime, V2CaseRuntime):
            raise ValueError("runtime must be a V2CaseRuntime, got " + type(runtime).__name__)
        runtime.require_open()
        faults = _parse_faults(runtime.faults, runtime.registry)
        mirror = ToolRegistry(
            dataclasses.replace(spec, handler=self._wrap(spec)) for spec in runtime.registry
        )
        _require_mirror(runtime.registry, mirror)
        # Last, so a rejected configuration leaves the runtime unclaimed.
        runtime.claim_tool_gateway()
        self._runtime = runtime
        self._registry = mirror
        self._faults = faults
        self._counts = [0] * len(faults)
        self._records: list[FaultCallRecord] = []
        self._observation_id: str | None = None
        self._stopped = False

    # -- read-only views ---------------------------------------------------

    @property
    def runtime(self) -> V2CaseRuntime:
        return self._runtime

    @property
    def records(self) -> tuple[FaultCallRecord, ...]:
        return tuple(self._records)

    @property
    def matching_counts(self) -> tuple[int, ...]:
        """Matching calls seen so far, by fault index."""
        return tuple(self._counts)

    # -- execution ---------------------------------------------------------

    def execute(self, tool_name: str, arguments: Mapping[str, str], *,
                observation_id: str) -> ToolResult:
        """One tool call through the existing executor and the fault boundary."""
        if self._stopped:
            raise FaultConfigurationError("gateway stopped after a fault configuration error")
        self._runtime.require_open()
        if not isinstance(observation_id, str) or not observation_id.strip():
            raise ValueError("observation_id is required and must be a non-empty string")
        if self._observation_id is not None:
            raise EvalRuntimeError("gateway calls cannot be nested")
        self._observation_id = observation_id
        try:
            return execute_tool(self._registry, self._runtime.context, tool_name, arguments,
                                observation_id=observation_id)
        finally:
            self._observation_id = None

    # -- the handler boundary ----------------------------------------------

    def _wrap(self, spec: ToolSpec) -> Handler:
        real = spec.handler
        name = spec.name

        def handler(context, arguments):
            fault = self._at_boundary(name, arguments)
            if fault is None:
                return real(context, arguments)
            return self._inject(fault, name)

        return handler

    def _at_boundary(self, tool_name: str, arguments: Mapping[str, str]) -> _Fault | None:
        """Count one validated call, decide its fault, and record it."""
        observation_id = self._observation_id
        if observation_id is None:
            raise FaultConfigurationError(
                "a gateway handler ran outside FaultInjectingGateway.execute")
        matched = self._matching_faults(tool_name, arguments)
        counts = {fault.index: self._counts[fault.index] + 1 for fault in matched}
        due = [fault for fault in matched if self._is_due(fault, counts[fault.index])]
        fault = self._select(due)
        for index, count in counts.items():
            self._counts[index] = count
        self._records.append(FaultCallRecord(
            sequence=len(self._records) + 1,
            tool_name=tool_name,
            observation_id=observation_id,
            outcome=OUTCOME_DELEGATED if fault is None else _INJECTED_OUTCOMES[fault.mode],
            fault_index=None if fault is None else fault.index,
            fault_mode=None if fault is None else fault.mode,
            matching_call_number=None if fault is None else counts[fault.index],
            simulated_latency_ms=(DEFAULT_SIMULATED_TIMEOUT_MS
                                  if fault is not None and fault.mode == MODE_TIMEOUT
                                  else None),
            matched_faults=tuple(sorted(counts.items())),
        ))
        return fault

    def _matching_faults(self, tool_name: str, arguments: Mapping[str, str]) -> list[_Fault]:
        return [fault for fault in self._faults if fault.matches(tool_name, arguments)]

    @staticmethod
    def _is_due(fault: _Fault, matching_call_number: int) -> bool:
        # Exactly the on_call-th matching call: once, never before or after.
        return matching_call_number == fault.on_call

    def _select(self, due: list[_Fault]) -> _Fault | None:
        if len(due) > 1:
            # Never pick one by list order. The case-run is invalid from here.
            self._stopped = True
            raise FaultConfigurationError(
                "faults " + ", ".join("[" + str(fault.index) + "]" for fault in due)
                + " are all due on the same call; a call can carry one fault only")
        return due[0] if due else None

    def _inject(self, fault: _Fault, tool_name: str) -> object:
        """Replace the real handler for this one call. Never runs it."""
        if fault.mode == MODE_ERROR:
            raise InjectedToolError(tool_name + " injected error")
        if fault.mode == MODE_TIMEOUT:
            # No waiting of any kind; the latency is recorded, not spent.
            raise ToolTimeout(tool_name + " timed out")
        if fault.mode == MODE_MALFORMED:
            return _InjectedMalformedResult()
        raise FaultConfigurationError("unknown fault mode")
