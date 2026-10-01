"""Server-owned identity, idempotency keys and ids (docs/v2/stage6-design.md §8, §9).

- `RequestIdentity` is trusted composition-root input: the server-side persona
  and the server-issued id of one logical user request. It is never derived
  from user text, an action intent or model output.
- `idempotency_key` binds that identity, the action name and the canonical
  validated arguments (`s6-idempotency/1`). It never uses customer_id and is
  never accepted from a caller.
- Ids (`PA`, `AS6`, `HT`, `RC`) come from an injected `IdProvider`. The
  deterministic provider is stateless and derives an id from the idempotency
  key (`s6-ids/1`), so the same key gives the same id after a restart and in
  another process. No action argument can carry an id.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .actions import ValidatedAction
from .policy_source import canonical

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

IDEMPOTENCY_SCHEMA = "s6-idempotency/1"
IDEMPOTENCY_KEY_PREFIX = "s6k1-"
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^s6k1-[0-9a-f]{64}$")

IDS_SCHEMA = "s6-ids/1"
ID_PATTERN = re.compile(r"^(PA|AS6|HT|RC)-[0-9A-F]{16,32}$")


@dataclass(frozen=True, kw_only=True)
class RequestIdentity:
    """Who is asking (server-side persona) and which logical request this is."""

    persona_id: str
    request_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.persona_id, str) or not self.persona_id.strip():
            raise ValueError("RequestIdentity.persona_id must be a non-empty string")
        if not isinstance(self.request_id, str) or not REQUEST_ID_PATTERN.match(self.request_id):
            raise ValueError("RequestIdentity.request_id does not match the request id format")


def idempotency_key(identity: RequestIdentity, action: ValidatedAction) -> str:
    """The server-owned key of one (identity, action, canonical args)."""
    if not isinstance(identity, RequestIdentity):
        raise ValueError("identity must be a RequestIdentity")
    if not isinstance(action, ValidatedAction):
        raise ValueError("action must be a ValidatedAction")
    material = canonical({
        "schema": IDEMPOTENCY_SCHEMA,
        "persona_id": identity.persona_id,
        "request_id": identity.request_id,
        "action_name": action.action_name,
        "args": action.args_object(),
    })
    return IDEMPOTENCY_KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


class IdKind(str, Enum):
    PENDING_ACTION = "PA"
    AFTER_SALES_CASE = "AS6"
    HANDOFF_TICKET = "HT"
    RECEIPT = "RC"


@runtime_checkable
class IdProvider(Protocol):
    def new_id(self, kind: IdKind, idempotency_key: str) -> str:
        ...


def _require_key(key: object) -> str:
    if not isinstance(key, str) or not IDEMPOTENCY_KEY_PATTERN.match(key):
        raise ValueError("idempotency_key does not match the s6-idempotency/1 format")
    return key


class DeterministicIdProvider:
    """Stateless ids derived from the idempotency key. Formal eval uses this."""

    def __init__(self, namespace: str) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a non-empty string")
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    def new_id(self, kind: IdKind, idempotency_key: str) -> str:
        if not isinstance(kind, IdKind):
            raise ValueError("kind must be an IdKind")
        material = canonical({
            "schema": IDS_SCHEMA,
            "namespace": self._namespace,
            "kind": kind.value,
            "key": _require_key(idempotency_key),
        })
        return kind.value + "-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16].upper()


class UuidIdProvider:
    """Random ids for a real deployment only. Formal runtimes refuse it."""

    def new_id(self, kind: IdKind, idempotency_key: str) -> str:
        if not isinstance(kind, IdKind):
            raise ValueError("kind must be an IdKind")
        _require_key(idempotency_key)
        return kind.value + "-" + uuid.uuid4().hex.upper()
