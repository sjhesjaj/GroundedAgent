"""ActionOutcome and its deterministic user-facing rendering (docs/v2/stage6-design.md §13.2, §18).

The outcome is what a caller gets back: a status, the opaque ids it needs, the
Guard's decision and a closed code. No customer id, SQL, snapshot internals or
exception text.

`ActionOutcomeRenderer` is not a model: fixed templates keyed by the persisted
outcome. Only an EXECUTED outcome (which always carries a receipt) may say an
action was submitted or created; every other outcome says it was not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .action_errors import FAILURE_CODES
from .actions import (
    ACTION_NAMES,
    CREATE_EXCHANGE,
    CREATE_RETURN,
    ESCALATE_TO_HUMAN,
    RESOURCE_AFTER_SALES_CASE,
    RESOURCE_HANDOFF_TICKET,
)
from .guard import (
    DENY_REASON_CODES,
    GUARD_REASON_CODES,
    REASON_ACTIVE_CASE,
    REASON_BUSINESS_STATE_CONFLICT,
    REASON_DELIVERY_NOT_ESTABLISHED,
    REASON_EXCHANGE_TARGET_INCOMPATIBLE,
    REASON_EXCHANGE_TARGET_INVALID,
    REASON_EXCHANGE_WINDOW_CLOSED,
    REASON_HANDOFF_NOT_REQUIRED,
    REASON_HANDOFF_REQUIRED,
    REASON_HANDOFF_TICKET_EXISTS,
    REASON_INVENTORY_UNAVAILABLE,
    REASON_ITEM_ALREADY_RETURNED,
    REASON_NO_APPLICABLE_POLICY,
    REASON_NON_RETURNABLE,
    REASON_NOT_DELIVERED,
    REASON_ORDER_ITEM_NOT_IN_ORDER,
    REASON_ORDER_NOT_ACCESSIBLE,
    REASON_ORDER_STATUS_INELIGIBLE,
    REASON_PENDING_REQUEST,
    REASON_POLICY_CONFLICT,
    REASON_RETURN_WINDOW_CLOSED,
    GuardDecisionKind,
)

APPROVAL_REJECTED = "approval_rejected"
STALE_REASON_CODES = frozenset({
    "record_set_changed", "record_version_changed", "policy_changed",
    "action_policy_changed", "guard_decision_changed",
})


class ActionStatus(str, Enum):
    EXECUTED = "EXECUTED"
    WAITING_APPROVAL = "WAITING_APPROVAL"   # frozen contract; produced from Stage 6.2
    DENIED = "DENIED"
    REJECTED = "REJECTED"                   # frozen contract; produced from Stage 6.2
    STALE = "STALE"                         # frozen contract; produced from Stage 6.2
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class ReceiptView:
    receipt_id: str
    resource_type: str
    resource_id: str


@dataclass(frozen=True, kw_only=True)
class GuardView:
    decision: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.decision not in {item.value for item in GuardDecisionKind}:
            raise ValueError("GuardView.decision is not a Guard decision")
        if self.reason_code not in GUARD_REASON_CODES:
            raise ValueError("GuardView.reason_code is not a Guard reason code")


@dataclass(frozen=True, kw_only=True)
class ActionOutcome:
    status: ActionStatus
    action_name: str
    request_id: str
    idempotent_replay: bool = False
    decision_conflict: bool = False
    approval_recorded: bool = False
    pending_action_id: str | None = None
    receipt: ReceiptView | None = None
    guard: GuardView | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ActionStatus):
            raise ValueError("ActionOutcome.status must be an ActionStatus")
        if self.action_name not in ACTION_NAMES:
            raise ValueError("ActionOutcome.action_name is not a Stage 6 action")
        if (self.status is ActionStatus.EXECUTED) != (self.receipt is not None):
            raise ValueError("an EXECUTED outcome, and only one, carries a receipt")
        if self.status is ActionStatus.DENIED:
            if (self.guard is None or self.guard.decision != GuardDecisionKind.DENY.value
                    or self.code != self.guard.reason_code or self.code not in DENY_REASON_CODES):
                raise ValueError("a DENIED outcome carries its DENY reason code")
        elif self.status is ActionStatus.FAILED:
            if self.code not in FAILURE_CODES:
                raise ValueError("a FAILED outcome carries an infrastructure failure code")
        elif self.status is ActionStatus.STALE:
            if self.code not in STALE_REASON_CODES:
                raise ValueError("a STALE outcome carries a stale reason")
        elif self.status is ActionStatus.REJECTED:
            if self.code != APPROVAL_REJECTED:
                raise ValueError("a REJECTED outcome carries approval_rejected")
        elif self.code is not None:
            raise ValueError("EXECUTED and WAITING_APPROVAL outcomes carry no code")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "action_name": self.action_name,
            "request_id": self.request_id,
            "idempotent_replay": self.idempotent_replay,
            "decision_conflict": self.decision_conflict,
            "approval_recorded": self.approval_recorded,
            "pending_action_id": self.pending_action_id,
            "receipt": None if self.receipt is None else {
                "receipt_id": self.receipt.receipt_id,
                "resource_type": self.receipt.resource_type,
                "resource_id": self.receipt.resource_id,
            },
            "guard": None if self.guard is None else {
                "decision": self.guard.decision,
                "reason_code": self.guard.reason_code,
            },
            "code": self.code,
        }


# --------------------------------------------------------------------------
# Rendering (§18)
# --------------------------------------------------------------------------

ACTION_LABELS = {CREATE_RETURN: "退货", CREATE_EXCHANGE: "换货", ESCALATE_TO_HUMAN: "转人工"}

# Phrases that claim an action happened. Only an EXECUTED rendering may use any.
COMPLETION_CLAIM_MARKERS = (
    "已提交", "已为您提交", "已办理", "已退款", "已创建", "已为您创建",
    "已转人工", "已为您换", "已受理",
)

# reason_code -> (user-facing explanation, whether it already says nothing was submitted)
DENIED_EXPLANATIONS: dict[str, tuple[str, bool]] = {
    REASON_ORDER_NOT_ACCESSIBLE: ("当前身份下未找到该订单。", False),
    REASON_ORDER_ITEM_NOT_IN_ORDER: ("该订单中没有找到这件商品。", False),
    REASON_BUSINESS_STATE_CONFLICT: ("订单或物流记录目前无法确认，请联系人工客服核实。", False),
    REASON_DELIVERY_NOT_ESTABLISHED: ("订单或物流记录目前无法确认，请联系人工客服核实。", False),
    REASON_POLICY_CONFLICT: ("订单或物流记录目前无法确认，请联系人工客服核实。", False),
    REASON_NOT_DELIVERED: ("订单尚未签收，暂时无法提交该申请。", False),
    REASON_ORDER_STATUS_INELIGIBLE: ("该订单当前状态不支持该申请。", False),
    REASON_ACTIVE_CASE: ("这件商品已有处理中的售后申请，没有重复提交。", True),
    REASON_PENDING_REQUEST: ("这件商品已有处理中的售后申请，没有重复提交。", True),
    REASON_ITEM_ALREADY_RETURNED: ("这件商品已完成退货。", False),
    REASON_HANDOFF_REQUIRED: ("该问题需要人工核实；如需要，可以申请转人工处理。", False),
    REASON_NON_RETURNABLE: ("该商品属于不支持退货的品类。", False),
    REASON_NO_APPLICABLE_POLICY: ("当前没有适用于该商品的售后规则。", False),
    REASON_RETURN_WINDOW_CLOSED: ("该商品已超过退货时限。", False),
    REASON_EXCHANGE_WINDOW_CLOSED: ("该商品已超过换货时限。", False),
    REASON_EXCHANGE_TARGET_INVALID: ("目标商品不能用于这次换货。", False),
    REASON_EXCHANGE_TARGET_INCOMPATIBLE: ("目标商品不能用于这次换货。", False),
    REASON_INVENTORY_UNAVAILABLE: ("目标商品当前库存不足。", False),
    REASON_HANDOFF_NOT_REQUIRED: ("当前规则下该问题不需要转人工。", False),
    REASON_HANDOFF_TICKET_EXISTS: ("这件商品已有处理中的人工工单，没有重复创建。", True),
}

if set(DENIED_EXPLANATIONS) != set(DENY_REASON_CODES):
    raise ImportError("every DENY reason code needs exactly one rendering")

FAILED_TEXT = "系统暂时无法完成该操作，申请没有提交。请稍后再试，或联系人工客服。"
STALE_TEXT = "审批期间订单或售后状态发生了变化，原申请没有执行。请重新发起申请，我们会按最新状态重新核对。"


class ActionOutcomeRenderer:
    """Fixed templates. No model, no free text from any record."""

    def render(self, outcome: ActionOutcome) -> str:
        if not isinstance(outcome, ActionOutcome):
            raise ValueError("outcome must be an ActionOutcome")
        action = outcome.action_name
        status = outcome.status
        if status is ActionStatus.EXECUTED:
            receipt = outcome.receipt
            if action == CREATE_RETURN and receipt.resource_type == RESOURCE_AFTER_SALES_CASE:
                return "已提交退货申请（售后单号 " + receipt.resource_id + "），当前状态：待处理。"
            if action == CREATE_EXCHANGE and receipt.resource_type == RESOURCE_AFTER_SALES_CASE:
                return ("已提交换货申请（售后单号 " + receipt.resource_id
                        + "），当前状态：待处理。换货申请提交后不会立即发货。")
            if action == ESCALATE_TO_HUMAN and receipt.resource_type == RESOURCE_HANDOFF_TICKET:
                return "已创建人工客服工单（工单号 " + receipt.resource_id + "），客服会跟进处理。"
            raise ValueError("receipt resource does not belong to the action")
        if status is ActionStatus.WAITING_APPROVAL:
            return "该" + ACTION_LABELS[action] + "申请需要人工审批，目前正在等待审批；审批通过前不会执行。"
        if status is ActionStatus.REJECTED:
            return ("您的" + ACTION_LABELS[action]
                    + "申请未通过人工审批，该操作没有执行。如需进一步处理，可以联系人工客服。")
        if status is ActionStatus.STALE:
            return STALE_TEXT
        if status is ActionStatus.FAILED:
            return FAILED_TEXT
        explanation, says_not_submitted = DENIED_EXPLANATIONS[outcome.code]
        if says_not_submitted:
            return explanation
        ending = "工单没有创建。" if action == ESCALATE_TO_HUMAN else "申请没有提交。"
        return explanation + ending
