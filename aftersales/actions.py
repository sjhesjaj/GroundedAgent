"""The Stage 6 action contracts `s6-actions/1` (docs/v2/stage6-design.md §4, §5).

Exactly three simulated business actions over fixture data:

- create_return      one after_sales_cases row, type return, status 待处理
- create_exchange    one after_sales_cases row, type exchange, status 待处理
- escalate_to_human  one human_handoff_tickets row, status 待处理

An action is not a tool. An `ActionSpec` has kind BUSINESS_ACTION and
side_effect True as class constants, and no handler: there is nothing that
`execute_tool` could run, and `ToolRegistry` accepts ToolSpec only. Actions are
executed by the ActionGateway alone.

What a caller (a model, a test, an API) may supply is an action name and a
closed set of string arguments. `ActionIntentValidator` checks them before any
database is touched, in the frozen order of §5.1. Identity, approval, control
and system-owned id arguments are rejected by name, and no ActionSpec may even
declare one.

Canonical arguments use `aftersales.policy_source.canonical`, the same strict
canonical JSON as `eval_v2.control.canonical_json` (sorted keys, no ASCII
escaping, compact separators, no NaN); the domain package does not import the
eval package.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Iterable, Iterator, Mapping

from .action_errors import (
    DIAG_ACTION_NOT_ALLOWED,
    DIAG_FORBIDDEN_ACTION_ARGUMENT,
    DIAG_IDENTITY_ARGUMENT,
    DIAG_INVALID_ACTION_ARGUMENTS,
    DIAG_UNKNOWN_FUNCTION,
    ActionContractError,
    ActionValidationError,
    CapabilityConfigurationError,
)
from .arguments import IDENTITY_ARGUMENT_NAMES, MAX_ARGUMENT_LENGTH
from .policy_source import canonical
from .registry import ToolKind

ACTION_SPEC_VERSION = "s6-actions/1"

CREATE_RETURN = "create_return"
CREATE_EXCHANGE = "create_exchange"
ESCALATE_TO_HUMAN = "escalate_to_human"
ACTION_NAMES = (CREATE_RETURN, CREATE_EXCHANGE, ESCALATE_TO_HUMAN)

# Closed argument vocabularies.
NO_LONGER_WANTED = "no_longer_wanted"
SIZE_OR_SPEC_MISMATCH = "size_or_spec_mismatch"
QUALITY_ISSUE = "quality_issue"
QUALITY_DISPUTE = "quality_dispute"

RETURN_REASON_CODES = (NO_LONGER_WANTED, SIZE_OR_SPEC_MISMATCH, QUALITY_ISSUE)
EXCHANGE_REASON_CODES = (SIZE_OR_SPEC_MISMATCH, QUALITY_ISSUE)
HANDOFF_TRIGGERS = (QUALITY_DISPUTE,)

# The exact text written to after_sales_cases.reason. The free-text column is
# generated from a closed code; the Guard never reads it back.
REASON_LABELS: Mapping[str, str] = MappingProxyType({
    NO_LONGER_WANTED: "不想要了（无理由退货）",
    SIZE_OR_SPEC_MISMATCH: "尺码或规格不合适",
    QUALITY_ISSUE: "商品质量问题",
})

# A reason that the published handoff policy may route to a human (R-9 / E-9).
REASON_HANDOFF_TRIGGER: Mapping[str, str] = MappingProxyType({
    QUALITY_ISSUE: QUALITY_DISPUTE,
})

RESOURCE_AFTER_SALES_CASE = "after_sales_case"
RESOURCE_HANDOFF_TICKET = "human_handoff_ticket"

# Names no action argument may carry (§5.1). Identity is checked first, with
# its own diagnostic; the whole union is also forbidden as a parameter name.
IDENTITY_AND_AUTHORITY_ARGUMENTS = frozenset({
    "customer_id", "persona_id", "subject_id", "user_id", "role",
    "is_admin", "admin", "manager", "operator", "approver", "approver_ref",
})
APPROVAL_AND_CONTROL_ARGUMENTS = frozenset({
    "approval_required", "approved", "approval", "approval_decision",
    "skip_approval", "force", "override", "decision", "status",
})
SYSTEM_OWNED_ID_ARGUMENTS = frozenset({
    "idempotency_key", "request_id", "run_id", "case_id", "ticket_id",
    "pending_action_id", "receipt_id",
})
FORBIDDEN_ACTION_ARGUMENT_NAMES = (
    IDENTITY_AND_AUTHORITY_ARGUMENTS | APPROVAL_AND_CONTROL_ARGUMENTS | SYSTEM_OWNED_ID_ARGUMENTS
)

if not IDENTITY_ARGUMENT_NAMES <= IDENTITY_AND_AUTHORITY_ARGUMENTS:
    raise ImportError("Stage 6 forbidden arguments must cover the read-path identity names")


# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ActionParameter:
    """One required string parameter, optionally limited to a closed enum."""

    name: str
    description: str
    enum: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ActionParameter.name must be a non-empty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("ActionParameter.description must be a non-empty string")
        if self.enum is not None and (
            not isinstance(self.enum, tuple) or not self.enum
            or not all(isinstance(item, str) and item.strip() for item in self.enum)
            or len(set(self.enum)) != len(self.enum)
        ):
            raise ValueError("ActionParameter.enum must be a non-empty tuple of distinct strings")


@dataclass(frozen=True, kw_only=True)
class ActionSpec:
    """A side-effecting business action. Not a ToolSpec; it has no handler."""

    kind: ClassVar[ToolKind] = ToolKind.BUSINESS_ACTION
    side_effect: ClassVar[bool] = True

    name: str
    description: str
    parameters: tuple[ActionParameter, ...]
    resource_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ActionSpec.name must be a non-empty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("ActionSpec.description must be a non-empty string")
        if not isinstance(self.parameters, tuple) or not all(
            isinstance(item, ActionParameter) for item in self.parameters
        ):
            raise ValueError("ActionSpec.parameters must be a tuple of ActionParameter")
        names = [item.name for item in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("ActionSpec.parameters must have unique names")
        forbidden = sorted(FORBIDDEN_ACTION_ARGUMENT_NAMES & set(names))
        if forbidden:
            raise ValueError(
                "ActionSpec may not declare identity, approval, control or system id "
                "parameters: " + ", ".join(forbidden))
        if "order_id" not in names or "order_item_id" not in names:
            raise ValueError("every Stage 6 action targets one order item")
        if self.resource_type not in (RESOURCE_AFTER_SALES_CASE, RESOURCE_HANDOFF_TICKET):
            raise ValueError("ActionSpec.resource_type is not a Stage 6 resource")

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.parameters)

    def parameter(self, name: str) -> ActionParameter:
        for item in self.parameters:
            if item.name == name:
                return item
        raise KeyError(name)

    def input_schema(self) -> dict[str, object]:
        """The closed JSON Schema of this action's arguments."""
        properties: dict[str, object] = {}
        for item in self.parameters:
            schema: dict[str, object] = {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_ARGUMENT_LENGTH,
                "description": item.description,
            }
            if item.enum is not None:
                schema["enum"] = list(item.enum)
            properties[item.name] = schema
        return {
            "type": "object",
            "properties": properties,
            "required": list(self.parameter_names),
            "additionalProperties": False,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind.value,
            "side_effect": self.side_effect,
            "resource_type": self.resource_type,
            "input_schema": self.input_schema(),
        }


class ActionRegistry:
    """An immutable name -> ActionSpec map. Accepts ActionSpec only."""

    def __init__(self, specs: Iterable[ActionSpec]) -> None:
        actions: dict[str, ActionSpec] = {}
        for spec in specs:
            if type(spec) is not ActionSpec:
                raise ValueError("ActionRegistry accepts ActionSpec only, got " + type(spec).__name__)
            if spec.name in actions:
                raise ValueError("ActionRegistry already has an action named " + spec.name)
            actions[spec.name] = spec
        self._actions = MappingProxyType(actions)

    def get(self, name: object) -> ActionSpec:
        if not isinstance(name, str) or name not in self._actions:
            # Caller input is not echoed.
            raise KeyError("unknown action")
        return self._actions[name]

    def names(self) -> tuple[str, ...]:
        return tuple(self._actions)

    def __contains__(self, name: object) -> bool:
        return name in self._actions

    def __iter__(self) -> Iterator[ActionSpec]:
        return iter(self._actions.values())

    def __len__(self) -> int:
        return len(self._actions)


_PARAMETER_DESCRIPTIONS = {
    "order_id": "订单号，例如 ORD-1001。",
    "order_item_id": "要办理的那一件订单商品的明细号，例如 OI-1001-1。",
    "target_sku": "换货的目标 SKU，例如 SKU-TSHIRT-L。",
    "reason_code": "顾客发起申请的原因类别。",
    "handoff_trigger": "需要人工处理的问题类别。",
}


def _parameter(name: str, enum: tuple[str, ...] | None = None) -> ActionParameter:
    return ActionParameter(name=name, description=_PARAMETER_DESCRIPTIONS[name], enum=enum)


def build_action_registry() -> ActionRegistry:
    """The frozen `s6-actions/1` registry: exactly three actions."""
    return ActionRegistry((
        ActionSpec(
            name=CREATE_RETURN,
            description="为一件订单商品提交退货售后申请。提交不等于执行：是否允许、是否需要人工审批由系统决定。",
            parameters=(
                _parameter("order_id"),
                _parameter("order_item_id"),
                _parameter("reason_code", RETURN_REASON_CODES),
            ),
            resource_type=RESOURCE_AFTER_SALES_CASE,
        ),
        ActionSpec(
            name=CREATE_EXCHANGE,
            description="为一件订单商品提交换货售后申请（不发货、不改库存）。是否允许由系统决定。",
            parameters=(
                _parameter("order_id"),
                _parameter("order_item_id"),
                _parameter("target_sku"),
                _parameter("reason_code", EXCHANGE_REASON_CODES),
            ),
            resource_type=RESOURCE_AFTER_SALES_CASE,
        ),
        ActionSpec(
            name=ESCALATE_TO_HUMAN,
            description="为一件订单商品创建人工处理工单。只用于已发布规则要求人工处理的问题类别。",
            parameters=(
                _parameter("order_id"),
                _parameter("order_item_id"),
                _parameter("handoff_trigger", HANDOFF_TRIGGERS),
            ),
            resource_type=RESOURCE_HANDOFF_TICKET,
        ),
    ))


# --------------------------------------------------------------------------
# Validated actions
# --------------------------------------------------------------------------


def canonical_args(args: Mapping[str, str]) -> str:
    """The canonical JSON of validated arguments (sorted keys)."""
    return canonical({name: args[name] for name in sorted(args)})


def args_digest(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ValidatedAction:
    """An action intent that passed the closed contract. Values are verbatim."""

    action_name: str
    args: Mapping[str, str]
    canonical_args_json: str
    args_sha256: str
    target_order_id: str
    target_order_item_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.args, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.args.items()
        ):
            raise ActionContractError("ValidatedAction.args must map strings to strings")
        object.__setattr__(self, "args", MappingProxyType(dict(self.args)))
        if self.canonical_args_json != canonical_args(self.args):
            raise ActionContractError("ValidatedAction.canonical_args_json disagrees with its args")
        if self.args_sha256 != args_digest(self.canonical_args_json):
            raise ActionContractError("ValidatedAction.args_sha256 disagrees with its args")
        if (self.target_order_id != self.args.get("order_id")
                or self.target_order_item_id != self.args.get("order_item_id")):
            raise ActionContractError("ValidatedAction targets disagree with its args")

    def args_object(self) -> dict[str, str]:
        """A fresh, sorted plain dict of the arguments."""
        return {name: self.args[name] for name in sorted(self.args)}


class ActionIntentValidator:
    """The closed contract of §5.1, checked in its frozen order. Touches no DB."""

    def __init__(self, registry: ActionRegistry, effective_actions: Iterable[str]) -> None:
        if not isinstance(registry, ActionRegistry):
            raise ValueError("registry must be an ActionRegistry")
        effective = tuple(effective_actions)
        unknown = [name for name in effective if name not in registry]
        if unknown or len(set(effective)) != len(effective):
            raise CapabilityConfigurationError(
                "effective actions must be distinct registered actions")
        self._registry = registry
        self._effective = frozenset(effective)

    @property
    def effective_actions(self) -> frozenset[str]:
        return self._effective

    def validate(self, action_name: object, arguments: object) -> ValidatedAction:
        # 1. a registered action, granted for this run
        if not isinstance(action_name, str) or action_name not in self._registry:
            raise ActionValidationError(DIAG_UNKNOWN_FUNCTION)
        if action_name not in self._effective:
            raise ActionValidationError(DIAG_ACTION_NOT_ALLOWED)
        spec = self._registry.get(action_name)
        # 2. a mapping with string keys
        if not isinstance(arguments, Mapping) or not all(isinstance(key, str) for key in arguments):
            raise ActionValidationError(DIAG_INVALID_ACTION_ARGUMENTS)
        keys = set(arguments)
        # 3. identity, before anything else about the values
        if keys & IDENTITY_ARGUMENT_NAMES:
            raise ActionValidationError(DIAG_IDENTITY_ARGUMENT)
        # 4. authority, approval, control and system-owned ids
        if keys & FORBIDDEN_ACTION_ARGUMENT_NAMES:
            raise ActionValidationError(DIAG_FORBIDDEN_ACTION_ARGUMENT)
        # 5. exactly the declared parameters
        if keys != set(spec.parameter_names):
            raise ActionValidationError(DIAG_INVALID_ACTION_ARGUMENTS)
        # 6. non-blank strings, bounded, enums respected; values kept verbatim
        args: dict[str, str] = {}
        for parameter in spec.parameters:
            value = arguments[parameter.name]
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > MAX_ARGUMENT_LENGTH
                    or (parameter.enum is not None and value not in parameter.enum)):
                raise ActionValidationError(DIAG_INVALID_ACTION_ARGUMENTS)
            args[parameter.name] = value
        canonical_json = canonical_args(args)
        return ValidatedAction(
            action_name=action_name,
            args=args,
            canonical_args_json=canonical_json,
            args_sha256=args_digest(canonical_json),
            target_order_id=args["order_id"],
            target_order_item_id=args["order_item_id"],
        )
