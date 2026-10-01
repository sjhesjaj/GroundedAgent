"""Closed failure vocabularies and exceptions of the Stage 6 action layer.

docs/v2/stage6-design.md §5.1 (validation diagnostics), §16 (infrastructure
failures). Every message names a code, an action name or a parameter path -
never an argument value, an identity, SQL, or another exception's text.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Infrastructure failures (§16). A failure is FAILED, never a Guard decision.
# --------------------------------------------------------------------------

FAILURE_TRANSACTION = "transaction_failed"
FAILURE_STATE_READ = "state_read_failed"
FAILURE_STATE_MALFORMED = "state_malformed"
FAILURE_STATE_VERSION_MISSING = "state_version_missing"
FAILURE_POLICY_UNAVAILABLE = "policy_unavailable"
FAILURE_GUARD_INTERNAL = "guard_internal_error"
FAILURE_IDENTITY_UNRESOLVABLE = "identity_unresolvable"
FAILURE_ID_GENERATION = "id_generation_failed"
FAILURE_WRITE = "write_failed"
FAILURE_INVARIANT = "invariant_violation"

FAILURE_CODES = frozenset({
    FAILURE_TRANSACTION,
    FAILURE_STATE_READ,
    FAILURE_STATE_MALFORMED,
    FAILURE_STATE_VERSION_MISSING,
    FAILURE_POLICY_UNAVAILABLE,
    FAILURE_GUARD_INTERNAL,
    FAILURE_IDENTITY_UNRESOLVABLE,
    FAILURE_ID_GENERATION,
    FAILURE_WRITE,
    FAILURE_INVARIANT,
})

# The subset a Guard (capture or decide) can raise.
GUARD_FAILURE_CODES = frozenset({
    FAILURE_STATE_READ,
    FAILURE_STATE_MALFORMED,
    FAILURE_STATE_VERSION_MISSING,
    FAILURE_POLICY_UNAVAILABLE,
    FAILURE_GUARD_INTERNAL,
})

# --------------------------------------------------------------------------
# Validation diagnostics (§5.1, §15.3): an invalid intent never reaches the DB.
# --------------------------------------------------------------------------

DIAG_UNKNOWN_FUNCTION = "unknown_function"
DIAG_ACTION_NOT_ALLOWED = "action_not_allowed"
DIAG_INVALID_ACTION_ARGUMENTS = "invalid_action_arguments"
DIAG_IDENTITY_ARGUMENT = "identity_argument"
DIAG_FORBIDDEN_ACTION_ARGUMENT = "forbidden_action_argument"

VALIDATION_DIAGNOSTICS = frozenset({
    DIAG_UNKNOWN_FUNCTION,
    DIAG_ACTION_NOT_ALLOWED,
    DIAG_INVALID_ACTION_ARGUMENTS,
    DIAG_IDENTITY_ARGUMENT,
    DIAG_FORBIDDEN_ACTION_ARGUMENT,
})


class GuardFailure(RuntimeError):
    """An infrastructure failure inside the Guard. Never ALLOW, never DENY."""

    def __init__(self, code: str) -> None:
        if code not in GUARD_FAILURE_CODES:
            raise ValueError("unknown Guard failure code")
        super().__init__(code)
        self.code = code


class ActionValidationError(ValueError):
    """An action intent failed the closed contract. Carries a diagnostic only."""

    def __init__(self, diagnostic: str) -> None:
        if diagnostic not in VALIDATION_DIAGNOSTICS:
            raise ValueError("unknown validation diagnostic")
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class ActionCapabilityError(PermissionError):
    """An action outside the effective capability set reached the gateway.

    A programmer / protocol error: no Guard runs and nothing is written.
    """


class CapabilityConfigurationError(ValueError):
    """A capability configuration tried to grant something the deployment does not."""


class ActionContractError(ValueError):
    """A ValidatedAction or a Stage 6 record does not satisfy its own contract."""
