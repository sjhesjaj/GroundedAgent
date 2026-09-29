"""The runtime ToolRegistry: exactly five read-only tools in Stage 4.

Every tool declares its name, a closed input schema, whether it has a side
effect, and its handler. The runtime registry built here holds only the five
read-only tools of docs/v2/stage4-design.md §6. Future actions exist only in
the domain specification: there is no code, stub, or registration for them.

`ToolRegistry` itself accepts any well-formed spec, including one declaring
`side_effect=True`, so that the executor's refusal is a real, tested guard and
not an accident of what happened to be registered.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Callable, Iterable, Iterator, Mapping

from orchestration.contracts import ToolResult

from .arguments import MAX_ARGUMENT_LENGTH
from .business_tools import (
    BUSINESS_HANDLERS,
    BUSINESS_TOOL_NAMES,
    GET_AFTER_SALES_CASE,
    GET_INVENTORY,
    GET_LOGISTICS,
    GET_ORDER,
    IDENTITY_SCOPED_TOOLS,
    TOOL_PARAMETERS,
)
from .context import TrustedExecutionContext
from .policy import (
    POLICY_TOOL_NAME,
    PolicyAdapterNotReady,
    PolicySearchAdapter,
    make_policy_handler,
)

Handler = Callable[[TrustedExecutionContext, Mapping[str, str]], ToolResult]


class ToolKind(str, Enum):
    KNOWLEDGE_READ = "knowledge_read"
    BUSINESS_READ = "business_read"


@dataclass(frozen=True, kw_only=True)
class ParameterSpec:
    name: str
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ParameterSpec.name must be a non-empty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("ParameterSpec.description must be a non-empty string")


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    name: str
    description: str
    kind: ToolKind
    parameters: tuple[ParameterSpec, ...]
    side_effect: bool
    identity_scoped: bool
    handler: Handler

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ToolSpec.name must be a non-empty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("ToolSpec.description must be a non-empty string")
        if not isinstance(self.kind, ToolKind):
            raise ValueError("ToolSpec.kind must be a ToolKind")
        if not isinstance(self.parameters, tuple) or not all(
            isinstance(item, ParameterSpec) for item in self.parameters
        ):
            raise ValueError("ToolSpec.parameters must be a tuple of ParameterSpec")
        names = [item.name for item in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("ToolSpec.parameters must have unique names")
        # A bool, exactly: a truthy placeholder must not pass for "no side effect".
        if not isinstance(self.side_effect, bool):
            raise ValueError("ToolSpec.side_effect must be a bool")
        if not isinstance(self.identity_scoped, bool):
            raise ValueError("ToolSpec.identity_scoped must be a bool")
        if not callable(self.handler):
            raise ValueError("ToolSpec.handler must be callable")

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.parameters)

    def input_schema(self) -> dict[str, object]:
        """The closed JSON Schema for this tool's arguments."""
        return {
            "type": "object",
            "properties": {
                item.name: {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_ARGUMENT_LENGTH,
                    "description": item.description,
                }
                for item in self.parameters
            },
            "required": list(self.parameter_names),
            "additionalProperties": False,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind.value,
            "side_effect": self.side_effect,
            "identity_scoped": self.identity_scoped,
            "input_schema": self.input_schema(),
        }


class ToolRegistry:
    """An immutable name -> ToolSpec map."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        tools: dict[str, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec):
                raise ValueError(
                    "ToolRegistry accepts ToolSpec only, got " + type(spec).__name__
                )
            if spec.name in tools:
                raise ValueError("ToolRegistry already has a tool named " + spec.name)
            tools[spec.name] = spec
        self._tools = MappingProxyType(tools)

    def get(self, name: object) -> ToolSpec:
        if not isinstance(name, str):
            raise ValueError("tool name must be a string, got " + type(name).__name__)
        spec = self._tools.get(name)
        if spec is None:
            # A registered name is safe to report; an unknown one is caller
            # input and is not echoed.
            raise ValueError(
                "unknown tool; registered tools are: " + ", ".join(self._tools)
            )
        return spec

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


RUNTIME_TOOL_NAMES = (POLICY_TOOL_NAME,) + BUSINESS_TOOL_NAMES

_DESCRIPTIONS = {
    POLICY_TOOL_NAME: "检索当前业务时间下生效的售后规则（退换货时限、不可退品类、转人工条件等）。",
    GET_ORDER: "查询当前顾客的一个订单及其订单明细。",
    GET_LOGISTICS: "查询当前顾客某个订单的全部物流包裹（可能有多个运单号）的状态与签收时间。",
    GET_INVENTORY: "查询一个 SKU 的当前可售库存。",
    GET_AFTER_SALES_CASE: "查询当前顾客某个订单上已有的售后单及其进度。",
}

_PARAMETER_DESCRIPTIONS = {
    "query": "要检索的售后规则问题，例如「签收后几天内可以无理由退货」。",
    "order_id": "订单号，例如 ORD-1001。",
    "sku": "商品 SKU，例如 SKU-TSHIRT-M。",
}


def _parameters(names: tuple[str, ...]) -> tuple[ParameterSpec, ...]:
    return tuple(
        ParameterSpec(name=name, description=_PARAMETER_DESCRIPTIONS[name])
        for name in names
    )


def build_runtime_registry(
    policy_adapter: PolicySearchAdapter | None = None,
) -> ToolRegistry:
    """The Stage 4 runtime registry. The composition root picks the adapter."""
    adapter = PolicyAdapterNotReady() if policy_adapter is None else policy_adapter
    specs = [
        ToolSpec(
            name=POLICY_TOOL_NAME,
            description=_DESCRIPTIONS[POLICY_TOOL_NAME],
            kind=ToolKind.KNOWLEDGE_READ,
            parameters=_parameters(("query",)),
            side_effect=False,
            identity_scoped=False,
            handler=make_policy_handler(adapter),
        )
    ]
    for name in BUSINESS_TOOL_NAMES:
        specs.append(
            ToolSpec(
                name=name,
                description=_DESCRIPTIONS[name],
                kind=ToolKind.BUSINESS_READ,
                # Single source of truth: the handler validates against the same
                # tuple the registry publishes.
                parameters=_parameters(TOOL_PARAMETERS[name]),
                side_effect=False,
                identity_scoped=name in IDENTITY_SCOPED_TOOLS,
                handler=BUSINESS_HANDLERS[name],
            )
        )
    return ToolRegistry(specs)
