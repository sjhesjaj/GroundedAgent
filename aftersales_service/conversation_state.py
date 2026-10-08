"""Versioned, closed JSON codec for the conversation graph's state (M2).

Only the explicit types below may cross the checkpoint boundary. Type names
are wire tags, never Python import paths; decoding cannot import a class named
by stored data. Tuples, read-only mappings and enums retain their types. Every
decode is re-encoded and compared, so a constructor which changes persisted
data is rejected instead of silently changing the conversation.

The checkpointer therefore only ever sees JSON-native values (dict, list, str,
int, float, bool, None). Its serializer is strict as well, but in langgraph
1.2.14 a blocked object comes back as a plain dict instead of raising, so the
guarantee is this module's: `decode` validates every tag, field and the schema
version, and raises StateCodecError on anything else.

ConversationState (schema 1)
    generation     the data-directory generation the conversation belongs to:
                   recovery refuses a checkpoint of another generation
    snapshot       the eleven pieces Conversation._snapshot() captures
    turn           this request's trace and reply (_Turn); None on a state written
                   outside a turn (a new conversation, an operator decision)
    route          where the last node sends the graph next
    decision       the policy's last decision (ToolCall / Clarify / ActionIntent / Finish)
    visible        the observations that decision could see, fixed before it was made
    submission     the grounded action the gateway node submits
    customer_text  the customer message this request delivers (graph input)
Every node writes every field, so no value of an earlier step survives into a
later one by accident.
"""

from __future__ import annotations

import dataclasses
import math
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import TypedDict

from aftersales.action_outcome import ActionOutcome, ActionStatus, GuardView, ReceiptView
from orchestration.contracts import (
    BusinessEvidence, DerivedEvidence, Evidence, FreshnessContract, SourceType,
    ToolResult, ToolStatus,
)

from . import agent_core as core
from .action_grounding import ArgumentSupport, GroundedSubmission, GroundingBinding
from .observation_provenance import ObservationRecord, ObservedRecord, VisibleObservations

SCHEMA_VERSION = 1


class StateCodecError(ValueError):
    """Stored state is unsupported, malformed, or cannot round-trip exactly."""


class ConversationState(TypedDict, total=False):
    schema: int
    generation: str
    snapshot: object
    turn: object
    route: object
    decision: object
    visible: object
    submission: object
    customer_text: object


_STEP_FIELDS = ("turn", "route", "decision", "visible", "submission", "customer_text")
_FIELDS = frozenset(("schema", "generation", "snapshot") + _STEP_FIELDS)
SNAPSHOT_COMPONENTS = 11


@lru_cache(maxsize=1)
def _types() -> dict[str, type]:
    # Conversation imports this codec; resolve its two data classes only once
    # the caller is encoding real state, after both modules have initialized.
    from .conversation import _ControlRun, _Turn

    classes = (
        Evidence, BusinessEvidence, DerivedEvidence, ToolResult,
        SourceType, FreshnessContract, ToolStatus, ActionStatus,
        core.UserMessage, core.ToolObservation, core.ToolCall, core.Clarify,
        core.Finish, core.ActionIntent, _ControlRun, _Turn,
        ObservedRecord, ObservationRecord, VisibleObservations,
        ArgumentSupport, GroundingBinding, GroundedSubmission,
        GuardView, ReceiptView, ActionOutcome,
    )
    return {cls.__name__: cls for cls in classes}


def _encode(value: object) -> object:
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return value
    if kind is float:
        if not math.isfinite(value):
            raise StateCodecError("non-finite numbers cannot be checkpointed")
        return value
    if kind is list:
        return [_encode(item) for item in value]
    if kind is tuple:
        return {"type": "tuple", "items": [_encode(item) for item in value]}
    if kind in (dict, MappingProxyType):
        if not all(type(key) is str for key in value):
            raise StateCodecError("checkpoint mappings must have string keys")
        return {"type": "mappingproxy" if kind is MappingProxyType else "dict",
                "items": {key: _encode(item) for key, item in value.items()}}
    registered = _types().get(kind.__name__)
    if registered is not kind:
        raise StateCodecError("unregistered checkpoint type: " + kind.__name__)
    if isinstance(value, Enum):
        return {"type": kind.__name__, "value": _encode(value.value)}
    return {"type": kind.__name__, "fields": {
        field.name: _encode(getattr(value, field.name)) for field in dataclasses.fields(value)}}


def _decode(value: object) -> object:
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return value
    if kind is float:
        if not math.isfinite(value):
            raise StateCodecError("non-finite numbers cannot be checkpointed")
        return value
    if kind is list:
        return [_decode(item) for item in value]
    if kind is not dict or type(value.get("type")) is not str:
        raise StateCodecError("checkpoint value must be plain data with a known type tag")
    tag = value["type"]
    if tag in ("tuple", "dict", "mappingproxy"):
        if set(value) != {"type", "items"}:
            raise StateCodecError("unexpected checkpoint container fields")
        items = value["items"]
        if tag == "tuple":
            if type(items) is not list:
                raise StateCodecError("tuple items must be a list")
            return tuple(_decode(item) for item in items)
        if type(items) is not dict or not all(type(key) is str for key in items):
            raise StateCodecError("mapping items must be a string-keyed dict")
        decoded = {key: _decode(item) for key, item in items.items()}
        return MappingProxyType(decoded) if tag == "mappingproxy" else decoded
    cls = _types().get(tag)
    if cls is None:
        raise StateCodecError("unregistered checkpoint type tag")
    if issubclass(cls, Enum):
        if set(value) != {"type", "value"}:
            raise StateCodecError("unexpected checkpoint enum fields")
        return cls(_decode(value["value"]))
    if set(value) != {"type", "fields"} or type(value["fields"]) is not dict:
        raise StateCodecError("unexpected checkpoint record fields")
    fields = {field.name: field for field in dataclasses.fields(cls)}
    if set(value["fields"]) != set(fields):
        raise StateCodecError("missing or unknown fields for " + tag)
    decoded = {key: _decode(item) for key, item in value["fields"].items()}
    restored = cls(**{key: item for key, item in decoded.items() if fields[key].init})
    # ToolObservation's init=False result snapshot is reconstructed by its
    # constructor. Refuse a mutated result instead of losing that old snapshot.
    for key, field in fields.items():
        if not field.init and getattr(restored, key) != decoded[key]:
            raise StateCodecError("derived checkpoint field changed: " + tag + "." + key)
    return restored


def encode(value: object) -> object:
    """Return plain JSON data, refusing anything not exactly reconstructible."""
    try:
        encoded = _encode(value)
        restored = _decode(encoded)
        if restored != value or _encode(restored) != encoded:
            raise StateCodecError("checkpoint object did not round-trip equally")
        return encoded
    except StateCodecError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError) as error:
        raise StateCodecError("invalid checkpoint object") from error


def decode(value: object) -> object:
    """Rebuild only an allowlisted type, retaining every persisted field."""
    try:
        restored = _decode(value)
        if _encode(restored) != value:
            raise StateCodecError("checkpoint data did not round-trip equally")
        return restored
    except StateCodecError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError) as error:
        raise StateCodecError("invalid checkpoint data") from error


def encode_text(text: str) -> str:
    """The graph input of one customer message."""
    if type(text) is not str:
        raise StateCodecError("a customer message is text")
    return encode(text)


def _generation(value: object) -> str:
    if type(value) is not str or not value:
        raise StateCodecError("a conversation state names its generation")
    return value


def pack_state(snapshot: tuple, *, generation: str, turn=None, route: str | None = None,
               decision=None, visible=None, submission=None,
               customer_text: str | None = None) -> ConversationState:
    """Encode a complete state: the generation, the snapshot and every per-step field."""
    if type(snapshot) is not tuple or len(snapshot) != SNAPSHOT_COMPONENTS:
        raise StateCodecError("a conversation snapshot has eleven components")
    if route is not None and type(route) is not str:
        raise StateCodecError("a route is a node name")
    fields = {"turn": turn, "route": route, "decision": decision, "visible": visible,
              "submission": submission, "customer_text": customer_text}
    state = {"schema": SCHEMA_VERSION, "generation": _generation(generation),
             "snapshot": encode(snapshot)}
    state.update({key: encode(fields[key]) for key in _STEP_FIELDS})
    return state


def unpack_state(state: ConversationState) -> dict[str, object]:
    """Validate a complete state of this schema and decode every field."""
    if type(state) is not dict or set(state) != _FIELDS:
        raise StateCodecError("a conversation state has exactly the schema's fields")
    if type(state["schema"]) is not int or state["schema"] != SCHEMA_VERSION:
        raise StateCodecError("unsupported conversation state schema")
    decoded = {key: decode(state[key]) for key in ("snapshot",) + _STEP_FIELDS}
    decoded["generation"] = _generation(state["generation"])
    snapshot = decoded["snapshot"]
    if type(snapshot) is not tuple or len(snapshot) != SNAPSHOT_COMPONENTS:
        raise StateCodecError("a conversation snapshot has eleven components")
    if decoded["route"] is not None and type(decoded["route"]) is not str:
        raise StateCodecError("a route is a node name")
    if decoded["customer_text"] is not None and type(decoded["customer_text"]) is not str:
        raise StateCodecError("a customer message is text")
    return decoded
