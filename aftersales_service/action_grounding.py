"""The action grounding gate (M1-A1): an action may only target what its run observed.

Position. Conversation._act runs the gate after ActionIntentValidator (the
closed argument contract) and before ActionGateway.start_action (the only
write path). It is deterministic product code: no model, no text, no
database. Its only input besides the validated action is the
VisibleObservations fixed before the decision that proposed the action
(observation_provenance.py).

Rules, grounding version `m1-grounding/2`, for a new submission:

    order_id        equals the record_id of the order record of a successful
                    get_order observation of this run.
    order_item_id   equals the record_id of an order_item record of the SAME
                    get_order observation, whose relations.order_id is order_id.
                    Ids from different observations are never combined.
    latest read     the latest get_order of one order_id (one logical query
                    target) is the one that counts. If it is empty, failed or
                    incomplete, the action is rejected: an earlier success is
                    never fallen back to. Reads of other orders change nothing.
    contract only   target_sku (create_exchange), reason_code and
                    handoff_trigger keep the ActionIntentValidator contract and
                    nothing more: a binding never claims that an observation
                    supports them. target_sku is the customer's choice of
                    variant, not a record the agent acts on; whether it is a
                    valid, compatible SKU with enough stock is the Guard's
                    decision on trusted state (E-10, E-13). m1-grounding/1 also
                    required a get_inventory of the target SKU; M1-A1.1 dropped
                    that rule (docs/v2/m1-a1-action-grounding.md, "M1-A1.1").

When every rule holds, the target arguments are rebuilt from the matched
records (plus the contract-only arguments) and must equal the proposal
exactly, by value and by args_sha256; otherwise the action is rejected. The
gate never substitutes another item. The result is a GroundingBinding.

Replay. Per conversation, a SubmissionIndex keeps one GroundedSubmission per
idempotency key (the frozen core's pure `idempotency_key(identity, action)`):
the action, its args_sha256, its binding, the run it was first submitted in
and the first ActionOutcome. Only when that first outcome left a replay
anchor in the core - a pending action, or an EXECUTED outcome with its
receipt - does a later identical proposal reuse the binding without new
reads; the gateway then returns the core's own idempotent replay. A DENIED
or FAILED first outcome leaves no anchor: the core would run the Guard again
(a possible new side-effect attempt), so the proposal is a new submission
and needs grounding in its own run. The key never leaves the index.

A rejection carries a closed code (GROUNDING_REJECTION_CODES). It is a normal
product outcome, never a Guard reason code or an ActionStatus.
"""

from __future__ import annotations

from dataclasses import dataclass

from aftersales.action_outcome import ActionOutcome, ActionStatus
from aftersales.actions import ValidatedAction, args_digest, canonical_args
from aftersales.business_tools import GET_ORDER
from aftersales.ids import IDEMPOTENCY_KEY_PATTERN

from .observation_provenance import ObservationRecord, ObservedRecord, VisibleObservations

GROUNDING_VERSION = "m1-grounding/2"

# Closed rejection codes.
MISSING_ORDER_OBSERVATION = "missing_order_observation"          # no get_order of that order this run
TARGET_NOT_OBSERVED = "target_not_observed"                      # no read of this run returned the item
TARGET_RELATION_MISMATCH = "target_relation_mismatch"            # the item belongs to another order
STALE_OR_FAILED_OBSERVATION = "stale_or_failed_observation"      # the latest read is empty / failed /
                                                                 # incomplete, or no longer has the target
TARGET_RECONSTRUCTION_MISMATCH = "target_reconstruction_mismatch"  # rebuilt arguments != the proposal
GROUNDING_REJECTION_CODES = (
    MISSING_ORDER_OBSERVATION,
    TARGET_NOT_OBSERVED,
    TARGET_RELATION_MISMATCH,
    STALE_OR_FAILED_OBSERVATION,
    TARGET_RECONSTRUCTION_MISMATCH,
)
# m1-grounding/1 also had missing_inventory_observation (no get_inventory of an
# exchange's target SKU). M1-A1.1 removed the rule and, with it, the code.

# Entities and keys as the business tools structure them (aftersales.business_tools).
ENTITY_ORDER = "order"
ENTITY_ORDER_ITEM = "order_item"
ORDER_ID = "order_id"
ORDER_ITEM_ID = "order_item_id"
TARGET_SKU = "target_sku"

# Checked by the ActionIntentValidator contract only, never "supported by an observation".
# target_sku is the customer's choice of variant; the Guard judges it on trusted state.
CONTRACT_ONLY_ARGUMENTS = (TARGET_SKU, "reason_code", "handoff_trigger")


class GroundingRejected(Exception):
    """The proposal is not grounded in this run's observations. `code` is closed."""

    def __init__(self, code: str) -> None:
        if code not in GROUNDING_REJECTION_CODES:
            raise ValueError("a grounding rejection carries a closed code")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class ArgumentSupport:
    """The observed record one argument value was rebuilt from."""

    argument: str
    observation_id: str
    tool_name: str
    entity: str
    record_id: str
    state_version: int

    def to_dict(self) -> dict[str, object]:
        return {"argument": self.argument, "observation_id": self.observation_id,
                "tool_name": self.tool_name, "entity": self.entity, "record_id": self.record_id,
                "state_version": self.state_version}


@dataclass(frozen=True, kw_only=True)
class GroundingBinding:
    """Why the server accepted one action's targets: which observations support them."""

    action_name: str
    args_sha256: str
    run_index: int
    supports: tuple[ArgumentSupport, ...]
    contract_only: tuple[str, ...]
    grounding_version: str = GROUNDING_VERSION

    def matches(self, action_name: object, args_sha256: object) -> bool:
        return self.action_name == action_name and self.args_sha256 == args_sha256

    def to_dict(self) -> dict[str, object]:
        return {"version": self.grounding_version, "action_name": self.action_name,
                "args_sha256": self.args_sha256, "run_index": self.run_index,
                "supports": [item.to_dict() for item in self.supports],
                "contract_only": list(self.contract_only)}


def _latest(visible: VisibleObservations, tool_name: str, parameter: str,
            value: str) -> ObservationRecord | None:
    """The latest read of one logical query target in the frozen set."""
    latest = None
    for observation in visible.observations:  # ledger order
        if observation.tool_name == tool_name and observation.tool_arguments.get(parameter) == value:
            latest = observation
    return latest


def _target(observation: ObservationRecord, entity: str, record_id: str) -> ObservedRecord:
    """The queried record itself, from a read that must have fully succeeded."""
    record = observation.record(entity, record_id) if observation.usable else None
    if record is None:
        raise GroundingRejected(STALE_OR_FAILED_OBSERVATION)
    return record


def _order_item(visible: VisibleObservations, order_read: ObservationRecord,
                order: ObservedRecord, order_item_id: str) -> ObservedRecord:
    """The item, from the same read as its order, structurally linked to it."""
    item = order_read.record(ENTITY_ORDER_ITEM, order_item_id)
    if item is not None:
        if item.relations.get(ORDER_ID) != order.record_id:
            raise GroundingRejected(TARGET_RELATION_MISMATCH)
        return item
    # Not in the read that counts. Another observation never fills the gap; it
    # only tells why the target is not grounded.
    elsewhere = [found for observation in visible.observations if observation is not order_read
                 for found in (observation.record(ENTITY_ORDER_ITEM, order_item_id),)
                 if found is not None]
    if not elsewhere:
        raise GroundingRejected(TARGET_NOT_OBSERVED)
    if all(found.relations.get(ORDER_ID) == order.record_id for found in elsewhere):
        raise GroundingRejected(STALE_OR_FAILED_OBSERVATION)  # only an older read of this order had it
    raise GroundingRejected(TARGET_RELATION_MISMATCH)


def _support(argument: str, observation: ObservationRecord, record: ObservedRecord) -> ArgumentSupport:
    return ArgumentSupport(argument=argument, observation_id=observation.observation_id,
                           tool_name=observation.tool_name, entity=record.entity,
                           record_id=record.record_id, state_version=record.state_version)


def ground_action(action: ValidatedAction, visible: VisibleObservations) -> GroundingBinding:
    """Bind a validated action to the observations of its run, or raise GroundingRejected."""
    if type(action) is not ValidatedAction:
        raise TypeError("action must be a ValidatedAction")
    if type(visible) is not VisibleObservations:
        raise TypeError("visible must be the VisibleObservations fixed before the decision")
    args = action.args
    order_read = _latest(visible, GET_ORDER, ORDER_ID, action.target_order_id)
    if order_read is None:
        raise GroundingRejected(MISSING_ORDER_OBSERVATION)
    order = _target(order_read, ENTITY_ORDER, action.target_order_id)
    item = _order_item(visible, order_read, order, action.target_order_item_id)
    supports = [_support(ORDER_ID, order_read, order), _support(ORDER_ITEM_ID, order_read, item)]
    rebuilt = {ORDER_ID: order.record_id, ORDER_ITEM_ID: item.record_id}
    contract_only = tuple(name for name in CONTRACT_ONLY_ARGUMENTS if name in args)
    rebuilt.update({name: args[name] for name in contract_only})
    # The server's own reconstruction must be the proposal, exactly.
    if rebuilt != dict(args) or args_digest(canonical_args(rebuilt)) != action.args_sha256:
        raise GroundingRejected(TARGET_RECONSTRUCTION_MISMATCH)
    return GroundingBinding(action_name=action.action_name, args_sha256=action.args_sha256,
                            run_index=visible.run_index, supports=tuple(supports),
                            contract_only=contract_only)


# --------------------------------------------------------------------------
# Grounded submissions and replay anchors
# --------------------------------------------------------------------------


def leaves_replay_anchor(outcome: ActionOutcome) -> bool:
    """Whether the core replays this key from now on: it has a pending row or a receipt."""
    if type(outcome) is not ActionOutcome:
        raise TypeError("outcome must be an ActionOutcome")
    if outcome.pending_action_id is not None:
        return True
    return outcome.status is ActionStatus.EXECUTED and outcome.receipt is not None


@dataclass(frozen=True, kw_only=True)
class GroundedSubmission:
    """One submitted action and the binding it went to the gateway with. Internal only."""

    key: str                      # the core's idempotency key; never leaves the index
    action_name: str
    args_sha256: str
    binding: GroundingBinding
    first_run_index: int
    first_outcome: ActionOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not IDEMPOTENCY_KEY_PATTERN.match(self.key):
            raise ValueError("a submission is indexed by the core's idempotency key")
        if type(self.binding) is not GroundingBinding or not self.binding.matches(
                self.action_name, self.args_sha256):
            raise ValueError("a submission's binding is for this very action")
        if self.first_run_index != self.binding.run_index:
            raise ValueError("a new submission is grounded in the run it is submitted in")
        if type(self.first_outcome) is not ActionOutcome or self.first_outcome.action_name != self.action_name:
            raise ValueError("a submission keeps the first outcome of this action")

    @property
    def replay_anchored(self) -> bool:
        return leaves_replay_anchor(self.first_outcome)

    @property
    def pending_action_id(self) -> str | None:
        return self.first_outcome.pending_action_id

    def binds(self, action_name: object, args_sha256: object) -> bool:
        return (self.action_name == action_name and self.args_sha256 == args_sha256
                and self.binding.matches(action_name, args_sha256))


class SubmissionIndex:
    """Idempotency key -> GroundedSubmission for one conversation, in memory."""

    def __init__(self) -> None:
        self._by_key: dict[str, GroundedSubmission] = {}

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, key: str) -> GroundedSubmission | None:
        return self._by_key.get(key)

    def anchored(self, key: str) -> GroundedSubmission | None:
        """The submission of this key, if the core will replay it."""
        submission = self._by_key.get(key)
        return submission if submission is not None and submission.replay_anchored else None

    def record(self, submission: GroundedSubmission) -> None:
        """Index a new submission. One the core replays is never replaced."""
        if type(submission) is not GroundedSubmission:
            raise ValueError("the index holds GroundedSubmissions only")
        if self.anchored(submission.key) is not None:
            raise ValueError("an anchored submission is never replaced")
        self._by_key[submission.key] = submission

    def for_pending(self, pending_action_id: str) -> GroundedSubmission | None:
        """The submission that created this pending action."""
        for submission in self._by_key.values():
            if pending_action_id is not None and submission.pending_action_id == pending_action_id:
                return submission
        return None

    def snapshot(self) -> dict[str, GroundedSubmission]:
        return dict(self._by_key)

    def restore(self, saved: dict[str, GroundedSubmission]) -> None:
        self._by_key = dict(saved)
