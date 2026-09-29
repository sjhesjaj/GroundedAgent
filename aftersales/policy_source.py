"""Closed JSON front matter inside Markdown `---` fences (no YAML coercion).

Business fields have exactly one authority: this header. The body is retained
for people and Wiki compilation, never used to extract business parameters.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .policy import PolicyRecord, PolicyRuleType, WINDOW_RULE_TYPES

PARSER_VERSION = "after-sales-front-matter/1"
HEADER_KEYS = frozenset({
    "policy_id", "version", "title", "rule_type", "scope", "priority", "params",
    "effective_from", "effective_to", "source_doc", "locator", "provenance",
})
PROVENANCE_KEYS = frozenset({"issuer", "revision"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def closed(value: object, keys: frozenset[str], path: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(path + " must be an object")
    if set(value) != keys:
        raise ValueError(path + " missing=" + repr(sorted(keys - set(value)))
                         + " unknown=" + repr(sorted(set(value) - keys)))
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate front matter key: " + key)
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("invalid JSON constant: " + value)


@dataclass(frozen=True)
class PolicySource:
    # Immutable canonical header; callers get detached dictionaries.
    header_json: str
    body: str

    @property
    def header(self) -> dict:
        return json.loads(self.header_json)

    def record(self, build_id: str) -> PolicyRecord:
        values = self.header
        values.pop("provenance")
        values["scope"] = tuple(values["scope"])
        values["rule_type"] = PolicyRuleType(values["rule_type"])
        return PolicyRecord(**values, build_id=build_id)

    def render(self) -> str:
        return "---\n" + self.header_json + "\n---\n\n" + self.body.strip() + "\n"


def parse_policy_source(text: str) -> PolicySource:
    if not isinstance(text, str):
        raise ValueError("policy source must be text")
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "---":
        raise ValueError("policy source must begin with front matter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("front matter closing fence missing") from None
    header = json.loads("\n".join(lines[1:end]), object_pairs_hook=_unique_object,
                        parse_constant=_invalid_constant)
    closed(header, HEADER_KEYS, "front matter")
    for name in ("policy_id", "version", "source_doc"):
        if not isinstance(header[name], str) or not IDENTIFIER.fullmatch(header[name]):
            raise ValueError(name + " must be a safe, non-empty identifier")
    scope = header["scope"]
    if not isinstance(scope, list) or not all(isinstance(v, str) and v.strip() for v in scope):
        raise ValueError("scope must be a list of category strings; [] means all")
    if len(set(scope)) != len(scope):
        raise ValueError("scope contains duplicate categories")
    header["scope"] = sorted(scope)
    provenance = closed(header["provenance"], PROVENANCE_KEYS, "provenance")
    if not all(isinstance(v, str) and v.strip() for v in provenance.values()):
        raise ValueError("provenance values must be non-empty strings")
    rule_type = PolicyRuleType(header["rule_type"])
    if rule_type not in WINDOW_RULE_TYPES:
        key = "reason_code" if rule_type is PolicyRuleType.NON_RETURNABLE else "trigger"
        params = closed(header["params"], frozenset({key}), "params")
        if not isinstance(params[key], str) or not IDENTIFIER.fullmatch(params[key]):
            raise ValueError("params." + key + " must be a non-empty identifier")
    body = "\n".join(lines[end + 1:]).strip()
    locator = header["locator"]
    if not isinstance(locator, str) or not locator.startswith("section:"):
        raise ValueError("locator must be section:<heading>")
    heading = locator[len("section:"):]
    if not heading or "## " + heading not in body.splitlines():
        raise ValueError("locator must resolve to an actual level-2 body heading")
    source = PolicySource(canonical(header), body)
    source.record("validation")  # strict types, params, timezone, interval, priority
    return source
