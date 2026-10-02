"""The runtime of one Stage 6 case-run (docs/v2/stage6-design.md §10.1, §13.4, §19.2).

    Stage6CaseRuntime.from_case(case)
        validated case (eval/v2/stage6_case_contract.py)
        fresh FILE-BACKED Stage 6 database in its own temporary directory
        initial_state patches, applied by the trusted harness in BEGIN IMMEDIATE
        read side:   Stage6ReadRuntime (query_only connection, trusted context)
                     + exactly one FaultInjectingGateway for the whole case-run
        write side:  ActionGateway (deterministic ids, formal), built on demand
        CapabilityGate().narrow(): the full deployment set
        ActionFaultInjector: the case's action_faults

System objects and harness state are kept apart. A `restart` closes every
connection and discards the read context, the ActionGateway and the policy
catalog, then rebuilds them from the database file, the static configuration
and the current business time. The fault counters are harness instrumentation,
not system state: they span the whole case-run, as the frozen contract says
(read faults across the main run and every rerun; action faults across start
and resume), so they survive a restart.

Action faults fire at the real boundaries; nothing runs first and is then
rewritten:
    business_write / receipt_write / commit   the ActionGateway's fault hooks
    guard_read                                the Guard reader's read hook
                                              (error: the read raises
                                              sqlite3.Error; malformed: the
                                              read yields a NULL row)
    policy_catalog                            the catalog's n-th snapshot()
                                              raises; the Guard maps it to
                                              policy_unavailable
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from aftersales.action_db import create_stage6_database
from aftersales.action_gateway import ActionGateway
from aftersales.action_policy import S6_RISK_POLICY
from aftersales.capabilities import CapabilityGate, EffectiveCapabilities
from aftersales.clock import FixedClock
from aftersales.context import Persona, TrustedExecutionContext
from aftersales.guard import GuardDecision
from aftersales.guard_state import GUARD_READS, READ_MALFORMED
from aftersales.ids import DeterministicIdProvider, RequestIdentity
from aftersales.policy_catalog import PublishedPolicyCatalog
from aftersales.registry import ToolRegistry, build_runtime_registry

from .faults import FaultInjectingGateway
from .runtime import (
    REPO_ROOT,
    EvalCaseInvalid,
    EvalRuntimeError,
    check_runtime_registry,
    parse_virtual_now,
    resolve_case_persona,
)
from .stage6_state import (
    apply_initial_state,
    apply_mutate,
    harness_connection,
    read_audit,
    read_state,
)

STAGE6_CONTRACT_PATH = REPO_ROOT / "eval" / "v2" / "stage6_case_contract.py"
STAGE6_ID_NAMESPACE = "eval"
MAIN_REQUEST_ID = "req-1"
NEW_REQUEST_ID = "req-2"
OPERATOR_REF = "op-demo-1"

ACTION_FAULT_POINTS = ("guard_read", "policy_catalog", "business_write", "receipt_write", "commit")
GATEWAY_FAULT_POINTS = frozenset({"business_write", "receipt_write", "commit"})
OUTCOME_DELEGATED = "delegated"
OUTCOME_INJECTED_ERROR = "injected_error"
OUTCOME_INJECTED_MALFORMED = "injected_malformed"


# --------------------------------------------------------------------------
# The case contract
# --------------------------------------------------------------------------


_CONTRACT = None


def stage6_contract():
    """eval/v2/stage6_case_contract.py, loaded by path (it is stdlib-only)."""
    global _CONTRACT
    if _CONTRACT is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("v2_stage6_case_contract", STAGE6_CONTRACT_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _CONTRACT = module
    return _CONTRACT


def validate_stage6_case(case: object) -> None:
    errors = stage6_contract().case_errors(case)
    if errors:
        raise EvalCaseInvalid(errors)


# --------------------------------------------------------------------------
# Action faults
# --------------------------------------------------------------------------


class ActionFaultConfigurationError(EvalRuntimeError, ValueError):
    """The action_faults cannot be applied deterministically."""


class InjectedActionFault(RuntimeError):
    """An `error` action fault at a gateway write or commit boundary."""


class InjectedCatalogFault(RuntimeError):
    """An `error` action fault at the policy catalog."""


@dataclass(frozen=True, kw_only=True)
class ActionFaultRecord:
    """One call that reached an action fault boundary. Structure only."""

    sequence: int
    point: str
    read: str | None
    outcome: str
    fault_index: int | None

    def to_dict(self) -> dict[str, object]:
        return {"sequence": self.sequence, "point": self.point, "read": self.read,
                "outcome": self.outcome, "fault_index": self.fault_index}


@dataclass(frozen=True)
class _ActionFault:
    index: int
    point: str
    read: str | None
    mode: str
    on_call: int


class ActionFaultInjector:
    """Harness-owned counters for the case's action_faults; survives restart.

    One counter per declaration, advanced by every call it matches (point, and
    read for guard_read), whatever happens to the call. A declaration fires
    once, on its on_call-th matching call; two due on one call is a
    configuration error. It also observes every Guard decision, for scoring
    only (a decision whose transaction rolls back is otherwise not persisted).
    """

    def __init__(self, declarations: object) -> None:
        faults = []
        for index, raw in enumerate(declarations or ()):
            where = "action_faults[" + str(index) + "]"
            if not isinstance(raw, Mapping) or not {"point", "mode", "on_call"} <= set(raw) <= {
                    "point", "read", "mode", "on_call"}:
                raise ActionFaultConfigurationError(where + " has the wrong keys")
            point, mode, read, on_call = raw["point"], raw["mode"], raw.get("read"), raw["on_call"]
            if point not in ACTION_FAULT_POINTS:
                raise ActionFaultConfigurationError(where + ".point is not an action fault point")
            if read is not None and (point != "guard_read" or read not in GUARD_READS):
                raise ActionFaultConfigurationError(where + ".read is only a Guard read name")
            if mode not in ("error", "malformed") or (mode == "malformed" and point != "guard_read"):
                raise ActionFaultConfigurationError(where + ".mode is not allowed here")
            if isinstance(on_call, bool) or not isinstance(on_call, int) or on_call < 1:
                raise ActionFaultConfigurationError(where + ".on_call must be an integer >= 1")
            faults.append(_ActionFault(index, point, read, mode, on_call))
        self._faults = tuple(faults)
        self._counts = [0] * len(faults)
        self.records: list[ActionFaultRecord] = []
        self.decisions: list[GuardDecision] = []

    def _at(self, point: str, read: str | None) -> _ActionFault | None:
        matched = [fault for fault in self._faults if fault.point == point
                   and (fault.read is None or fault.read == read)]
        due = []
        for fault in matched:
            self._counts[fault.index] += 1
            if self._counts[fault.index] == fault.on_call:
                due.append(fault)
        if len(due) > 1:
            raise ActionFaultConfigurationError("action faults " + ", ".join(
                "[" + str(fault.index) + "]" for fault in due) + " are due on the same call")
        fault = due[0] if due else None
        outcome = OUTCOME_DELEGATED if fault is None else (
            OUTCOME_INJECTED_MALFORMED if fault.mode == "malformed" else OUTCOME_INJECTED_ERROR)
        self.records.append(ActionFaultRecord(sequence=len(self.records) + 1, point=point, read=read,
                                              outcome=outcome,
                                              fault_index=None if fault is None else fault.index))
        return fault

    # The ActionGateway's fault hooks (business_write / receipt_write / commit).
    def before(self, point: str) -> None:
        if point not in GATEWAY_FAULT_POINTS:
            return  # e.g. "compensation": not an eval fault point
        if self._at(point, None) is not None:
            raise InjectedActionFault(point + " injected error")

    # The Guard reader's read hook.
    def before_read(self, read: str) -> str | None:
        fault = self._at("guard_read", read)
        if fault is None:
            return None
        if fault.mode == "malformed":
            return READ_MALFORMED
        raise sqlite3.OperationalError("injected guard read error")

    def observe(self, decision: GuardDecision) -> None:
        self.decisions.append(decision)

    def wrap_catalog(self, catalog: object) -> "_FaultingCatalog":
        return _FaultingCatalog(catalog, self)

    def fired_since(self, position: int) -> bool:
        return any(record.outcome != OUTCOME_DELEGATED for record in self.records[position:])


class _FaultingCatalog:
    """The real catalog; its matching snapshot() call raises instead of reading."""

    def __init__(self, catalog: object, injector: ActionFaultInjector) -> None:
        self._catalog = catalog
        self._injector = injector

    def snapshot(self):
        if self._injector._at("policy_catalog", None) is not None:
            raise InjectedCatalogFault("policy catalog injected error")
        return self._catalog.snapshot()


# --------------------------------------------------------------------------
# The read side
# --------------------------------------------------------------------------


class Stage6ReadRuntime:
    """The read runtime of a Stage 6 case-run (eval_v2.faults.ReadRuntime).

    A read-only, query_only connection to the case database, the trusted
    context (persona + FixedClock at the current business time) and the five
    read tools. `reopen` closes the connection and builds a new context, for
    advance_clock and restart.
    """

    def __init__(self, db_path: Path, *, persona: Persona, faults: tuple, virtual_now: str) -> None:
        registry = build_runtime_registry()
        check_runtime_registry(registry)
        self._db_path = db_path
        self._persona = persona
        self._faults = tuple(faults)
        self._registry = registry
        self._connection: sqlite3.Connection | None = None
        self._context: TrustedExecutionContext | None = None
        self._gateway_claimed = False
        self._closed = False
        self.reopen(virtual_now)

    @property
    def faults(self) -> tuple[dict, ...]:
        return self._faults

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def context(self) -> TrustedExecutionContext:
        self.require_open()
        return self._context

    def require_open(self) -> None:
        if self._closed or self._context is None:
            raise EvalRuntimeError("the Stage 6 read runtime is closed")

    def claim_tool_gateway(self) -> None:
        self.require_open()
        if self._gateway_claimed:
            raise EvalRuntimeError("a Stage 6 read runtime accepts exactly one tool gateway")
        self._gateway_claimed = True

    def reopen(self, virtual_now: str) -> None:
        self._release()
        connection = sqlite3.connect(self._db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise EvalRuntimeError("SQLite refused to enable query_only")
            context = TrustedExecutionContext(persona=self._persona,
                                              clock=FixedClock(parse_virtual_now(virtual_now)),
                                              connection=connection)
        except BaseException:
            connection.close()
            raise
        self._connection, self._context = connection, context

    def _release(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._connection, self._context = None, None

    def close(self) -> None:
        self._release()
        self._closed = True


# --------------------------------------------------------------------------
# The case runtime
# --------------------------------------------------------------------------


class Stage6CaseRuntime:
    """Database, read side, write side and fault counters of one case-run."""

    def __init__(self, case: dict, directory: tempfile.TemporaryDirectory, db_path: Path,
                 persona: Persona) -> None:
        self.case = case
        self._directory = directory
        self.db_path = db_path
        self.persona = persona
        self.virtual_now = case["virtual_now"]
        self.capabilities: EffectiveCapabilities = CapabilityGate().narrow()
        self.injector = ActionFaultInjector(case["initial_state"]["action_faults"])
        self.read_runtime = Stage6ReadRuntime(db_path, persona=persona,
                                              faults=tuple(case["initial_state"]["faults"]),
                                              virtual_now=self.virtual_now)
        self.read_gateway = FaultInjectingGateway(self.read_runtime)
        self._gateway: ActionGateway | None = None
        self.restarts = 0
        self.gateways_built = 0
        self._closed = False

    @classmethod
    def from_case(cls, case: object) -> "Stage6CaseRuntime":
        validate_stage6_case(case)
        persona = resolve_case_persona(case["initial_state"]["trusted_context"]["persona_id"])
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            db_path = Path(directory.name) / "case.db"
            create_stage6_database(db_path)
            connection = harness_connection(db_path)
            try:
                apply_initial_state(connection, case["initial_state"])
            finally:
                connection.close()
            return cls(case, directory, db_path, persona)
        except BaseException:
            directory.cleanup()
            raise

    @property
    def directory(self) -> Path:
        return Path(self._directory.name)

    @property
    def customer_id(self) -> str:
        return self.persona.customer_id

    def identity(self, request_id: str) -> RequestIdentity:
        """The trusted request identity; built by the harness, never by a policy."""
        return RequestIdentity(persona_id=self.persona.persona_id, request_id=request_id)

    @property
    def gateway(self) -> ActionGateway:
        if self._closed:
            raise EvalRuntimeError("the Stage 6 case runtime is closed")
        if self._gateway is None:
            self._gateway = ActionGateway(
                self.db_path,
                clock=FixedClock(parse_virtual_now(self.virtual_now)),
                id_provider=DeterministicIdProvider(STAGE6_ID_NAMESPACE),
                catalog=self.injector.wrap_catalog(PublishedPolicyCatalog()),
                capabilities=self.capabilities,
                risk_policy=S6_RISK_POLICY,
                formal=True,
                fault_hooks=self.injector,
                guard_read_hook=self.injector,
                decision_observer=self.injector.observe,
            )
            self.gateways_built += 1
        return self._gateway

    def advance_clock(self, virtual_now: str) -> None:
        if parse_virtual_now(virtual_now) <= parse_virtual_now(self.virtual_now):
            raise EvalRuntimeError("advance_clock must move business time forward")
        self.virtual_now = virtual_now
        self.read_runtime.reopen(virtual_now)
        self._gateway = None  # the next one gets a FixedClock at the new instant

    def restart(self) -> None:
        """Close every connection and drop every system object; rebuild from file + config."""
        self.read_runtime.reopen(self.virtual_now)
        self._gateway = None
        self.restarts += 1

    def mutate(self, event: Mapping) -> None:
        connection = harness_connection(self.db_path)
        try:
            apply_mutate(connection, event)
        finally:
            connection.close()

    def state(self) -> dict:
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return read_state(connection)
        finally:
            connection.close()

    def audit(self) -> tuple[dict, ...]:
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return read_audit(connection)
        finally:
            connection.close()

    def business_time(self) -> datetime:
        return parse_virtual_now(self.virtual_now)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gateway = None
        self.read_runtime.close()
        self._directory.cleanup()

    def __enter__(self) -> "Stage6CaseRuntime":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

