"""Closed-schema validation for V2 tool arguments.

Every tool takes a fixed set of required string parameters and nothing else.
Validation runs before any handler, connection, or cursor is touched, and its
messages name the tool and the parameter path - never a value, because a value
may be an identity, a business key, or an injection payload.
"""

from __future__ import annotations

from typing import Mapping

MAX_ARGUMENT_LENGTH = 128

# Identity is injected from the trusted context. A caller - the Planner today,
# an LLM tomorrow - that tries to supply it is rejected, not silently ignored.
IDENTITY_ARGUMENT_NAMES = frozenset(
    {"customer_id", "persona_id", "subject_id", "user_id", "role"}
)


def validate_arguments(
    tool_name: str,
    parameter_names: tuple[str, ...],
    arguments: object,
) -> dict[str, str]:
    """Return a fresh copy of `arguments` if it matches the schema exactly."""
    prefix = tool_name + ".arguments"
    if not isinstance(arguments, Mapping):
        raise ValueError(
            prefix + " must be a mapping, got " + type(arguments).__name__
        )

    # Keys first, before any set/sort/join touches them: a non-string key would
    # otherwise escape as a TypeError from sorted() or join().
    for key in arguments:
        if not isinstance(key, str):
            raise ValueError(
                prefix + " keys must all be strings, got " + type(key).__name__
            )

    smuggled = sorted(IDENTITY_ARGUMENT_NAMES & set(arguments))
    if smuggled:
        raise ValueError(
            prefix + " must not carry " + ", ".join(smuggled)
            + "; identity comes from the trusted context"
        )

    supplied = set(arguments)
    missing = [name for name in parameter_names if name not in supplied]
    if missing:
        raise ValueError(prefix + " is missing: " + ", ".join(missing))
    unexpected = sorted(supplied - set(parameter_names))
    if unexpected:
        raise ValueError(prefix + " does not accept: " + ", ".join(unexpected))

    copied: dict[str, str] = {}
    for name in parameter_names:
        value = arguments[name]
        path = prefix + "." + name
        if not isinstance(value, str):
            raise ValueError(path + " must be a string, got " + type(value).__name__)
        if not value.strip():
            raise ValueError(path + " must not be empty")
        if len(value) > MAX_ARGUMENT_LENGTH:
            raise ValueError(
                path + " must be at most " + str(MAX_ARGUMENT_LENGTH) + " characters"
            )
        copied[name] = value
    return copied
