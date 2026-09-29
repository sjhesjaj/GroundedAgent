"""Check V2 eval cases against eval/v2/spec/case.schema.json.

Stdlib only: a small interpreter for exactly the JSON Schema 2020-12 keywords
the case schema uses. An unsupported keyword is a SchemaError rather than
something silently ignored, so the schema cannot grow a rule this checker
does not enforce.

`case_errors` adds the cross-field rules a JSON Schema cannot state (see
docs/v2/holdout-domain-spec.md, "Case rules"). Nothing here reads the system
clock: timestamps are only parsed and checked for an explicit offset.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parent / "spec"
CASE_SCHEMA_PATH = SPEC_DIR / "case.schema.json"
SLOTS_PATH = SPEC_DIR / "slots.json"
ARCHETYPES_PATH = SPEC_DIR / "archetypes.json"
PERSONAS_PATH = SPEC_DIR / "personas.json"
FINAL_OUTCOMES_PATH = SPEC_DIR / "final-outcomes.json"

TOOL_ARGUMENTS = {
    "search_after_sales_policy": ("query",),
    "get_order": ("order_id",),
    "get_logistics": ("order_id",),
    "get_inventory": ("sku",),
    "get_after_sales_case": ("order_id",),
}

_ANNOTATIONS = frozenset({"$schema", "$id", "$comment", "title", "description", "$defs"})
_SUPPORTED = _ANNOTATIONS | frozenset({
    "$ref", "type", "enum", "const", "format",
    "properties", "required", "additionalProperties", "propertyNames", "minProperties",
    "items", "prefixItems", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "minimum", "oneOf", "anyOf",
})


class SchemaError(ValueError):
    """The schema itself uses something this checker does not implement."""


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _same(a: object, b: object) -> bool:
    # JSON equality: True is not 1, 1.0 equals 1.
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _is_type(value: object, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return {
        "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
    }[name] is type(value)


def aware_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


class _Checker:
    def __init__(self, root: dict) -> None:
        self.root = root

    def resolve(self, ref: str) -> dict:
        if not ref.startswith("#/"):
            raise SchemaError("only local $ref is supported")
        node: object = self.root
        for part in ref[2:].split("/"):
            node = node[part]  # type: ignore[index]
        return node  # type: ignore[return-value]

    def errors(self, value: object, schema: object, path: str) -> list[str]:
        if not isinstance(schema, dict):
            raise SchemaError("schema node at " + path + " is not an object")
        unknown = set(schema) - _SUPPORTED
        if unknown:
            raise SchemaError("unsupported keyword(s): " + ", ".join(sorted(unknown)))
        out: list[str] = []
        if "$ref" in schema:
            out += self.errors(value, self.resolve(schema["$ref"]), path)
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_is_type(value, n) for n in names):
                return out + [path + ": expected " + "/".join(names)]
        if "enum" in schema and not any(_same(value, v) for v in schema["enum"]):
            out.append(path + ": not an allowed value")
        if "const" in schema and not _same(value, schema["const"]):
            out.append(path + ": must equal " + json.dumps(schema["const"]))
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                out.append(path + ": too short")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                out.append(path + ": too long")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                out.append(path + ": does not match pattern")
            if schema.get("format") == "date-time" and not aware_timestamp(value):
                out.append(path + ": not a timezone-aware ISO-8601 timestamp")
        if _is_type(value, "number") and "minimum" in schema and value < schema["minimum"]:
            out.append(path + ": below minimum")
        if isinstance(value, dict):
            out += self._object(value, schema, path)
        if isinstance(value, list):
            out += self._array(value, schema, path)
        for key in ("oneOf", "anyOf"):
            if key in schema:
                matched = [b for b in schema[key] if not self.errors(value, b, path)]
                if key == "oneOf" and len(matched) != 1:
                    out.append(path + ": must match exactly one alternative, matched " + str(len(matched)))
                if key == "anyOf" and not matched:
                    out.append(path + ": matches no alternative")
        return out

    def _object(self, value: dict, schema: dict, path: str) -> list[str]:
        out: list[str] = []
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                out.append(path + ": missing " + name)
        if len(value) < schema.get("minProperties", 0):
            out.append(path + ": too few properties")
        for name, item in value.items():
            child = path + "." + name
            if "propertyNames" in schema:
                out += self.errors(name, schema["propertyNames"], child + " (key)")
            if name in props:
                out += self.errors(item, props[name], child)
            else:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    out.append(child + ": unexpected key")
                elif isinstance(extra, dict):
                    out += self.errors(item, extra, child)
        return out

    def _array(self, value: list, schema: dict, path: str) -> list[str]:
        out: list[str] = []
        if len(value) < schema.get("minItems", 0):
            out.append(path + ": too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(path + ": too many items")
        if schema.get("uniqueItems"):
            for i, item in enumerate(value):
                if any(_same(item, other) for other in value[:i]):
                    out.append(path + ": duplicate items")
                    break
        prefix = schema.get("prefixItems", [])
        for i, item in enumerate(value):
            child = path + "[" + str(i) + "]"
            if i < len(prefix):
                out += self.errors(item, prefix[i], child)
            elif "items" in schema:
                out += self.errors(item, schema["items"], child)
        return out


def schema_errors(instance: object, schema: dict | None = None) -> list[str]:
    schema = load_json(CASE_SCHEMA_PATH) if schema is None else schema
    return _Checker(schema).errors(instance, schema, "$")


def semantic_errors(case: dict) -> list[str]:
    """Cross-field rules. Call only on a schema-valid case."""
    out: list[str] = []
    caps = case["expected_capabilities"]
    both = sorted(set(caps["required"]) & set(caps["forbidden"]))
    if both:
        out.append("$.expected_capabilities: required and forbidden overlap: " + ", ".join(both))

    conditional = case["user_turns"][1:]
    offered = {slot for turn in conditional for slot in turn["on_clarify"]}
    clarify = case["expected_answerability"]["clarify"]
    if clarify["required"] and not conditional:
        out.append("$.expected_answerability.clarify: required, but user_turns has no conditional turn")
    if not clarify["required"] and conditional:
        out.append("$.user_turns: conditional turns present, but clarify.required is false")
    missing = sorted(set(clarify["slots"]) - offered)
    if missing:
        out.append("$.expected_answerability.clarify.slots not offered by any on_clarify: " + ", ".join(missing))

    for i, fault in enumerate(case["initial_state"]["faults"]):
        bad = sorted(set(fault["match"]) - set(TOOL_ARGUMENTS[fault["tool"]]))
        if bad:
            out.append("$.initial_state.faults[" + str(i) + "].match: not arguments of "
                       + fault["tool"] + ": " + ", ".join(bad))
    return out


def persona_customer_ids() -> dict[str, str]:
    """The frozen demo fixture mapping persona_id -> customer_id. Not authentication."""
    personas = load_json(PERSONAS_PATH)["personas"]  # type: ignore[index]
    mapping = {p["persona_id"]: p["customer_id"] for p in personas}
    if len(mapping) != len(personas) or len(set(mapping.values())) != len(mapping):
        raise ValueError("personas.json must map unique persona ids to unique customers")
    return mapping


def case_errors(case: object) -> list[str]:
    errors = schema_errors(case)
    return errors if errors else semantic_errors(case)  # type: ignore[arg-type]
