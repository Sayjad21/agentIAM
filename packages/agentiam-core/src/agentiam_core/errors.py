"""Errors and reason codes.

`ReasonCode` lives here rather than in `models` because it is the vocabulary shared by
every deny path, and exceptions carry it. `PLAN.md` §6.9 fixes the enum as closed: every
deny in the codebase maps to exactly one code, and a CI test asserts each is reachable.

This module imports nothing from the rest of the package. It is the root of the
dependency graph.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class ReasonCode(StrEnum):
    """Why a decision came out the way it did.

    Closed enum, fixed in `PLAN.md` §6.9. Never a freeform string: the console filters on
    these, the Grafana dashboard groups by them, and "denied" without a machine-readable
    cause is a bug (P-18).
    """

    OK = "OK"

    # token integrity and validity
    TOKEN_INVALID_SIGNATURE = "TOKEN_INVALID_SIGNATURE"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_NOT_YET_VALID = "TOKEN_NOT_YET_VALID"
    TOKEN_TOO_LARGE = "TOKEN_TOO_LARGE"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"

    # revocation
    TOKEN_REVOKED = "TOKEN_REVOKED"
    ANCESTOR_REVOKED = "ANCESTOR_REVOKED"

    # authority
    SCOPE_NOT_GRANTED = "SCOPE_NOT_GRANTED"
    SCOPE_ATTENUATED_AWAY = "SCOPE_ATTENUATED_AWAY"
    TOOL_DENIED = "TOOL_DENIED"
    ARG_PREDICATE_FAILED = "ARG_PREDICATE_FAILED"
    DEPTH_EXCEEDED = "DEPTH_EXCEEDED"
    INTENT_MISMATCH = "INTENT_MISMATCH"

    # budget
    BUDGET_EXHAUSTED_MANDATE = "BUDGET_EXHAUSTED_MANDATE"
    BUDGET_EXHAUSTED_CAVEAT = "BUDGET_EXHAUSTED_CAVEAT"
    LEASE_UNAVAILABLE = "LEASE_UNAVAILABLE"
    LEASE_NOT_ACTIVE = "LEASE_NOT_ACTIVE"
    RATE_LIMITED = "RATE_LIMITED"

    # policy and drift
    POLICY_DENIED = "POLICY_DENIED"
    POLICY_BUNDLE_STALE = "POLICY_BUNDLE_STALE"
    DRIFT_ESCALATION = "DRIFT_ESCALATION"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

    # infrastructure
    CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED = "CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    VERIFICATION_LIMIT_EXCEEDED = "VERIFICATION_LIMIT_EXCEEDED"


class AgentIAMError(Exception):
    """Base for every error raised by AgentIAM.

    Carries an optional `reason_code` so that an exception surfacing at the PEP boundary
    can become a decision record without a lookup table.
    """

    reason_code: ReasonCode | None = None

    def __init__(self, message: str, *, reason_code: ReasonCode | None = None) -> None:
        """Initialize the error, optionally tagging it with a reason code."""
        super().__init__(message)
        if reason_code is not None:
            self.reason_code = reason_code


class AttenuationError(AgentIAMError):
    """A proposed child caveat is not narrower than its parent.

    Raised at mint time, not at verification (spec 03 §3.2). Covers EC-T17 (adding an
    ungranted scope), EC-T18 (raising a ceiling), and EC-T19 (extending expiry).

    Note that this is a *usability* guarantee, not the security boundary — biscuit's
    append-only structure is what actually prevents widening. Failing here turns a
    misleading token into an early, legible error.
    """


class TokenError(AgentIAMError):
    """A token could not be minted or verified.

    Every subclass fixes its own `reason_code`, so a caller that catches `TokenError` can
    build a decision record without a lookup table (P-18).
    """


class MalformedTokenError(TokenError):
    """The token could not be parsed at all: absent, empty, or not valid base64."""

    reason_code = ReasonCode.MALFORMED_REQUEST


class InvalidSignatureError(TokenError):
    """The signature chain does not verify against any accepted root key.

    Covers a forged block, a flipped bit, truncated bytes, and a token minted by a key
    that is not in the accepted set (EC-T02…EC-T05).
    """

    reason_code = ReasonCode.TOKEN_INVALID_SIGNATURE


class TokenExpiredError(TokenError):
    """`now` is at or past `expires_at`. The boundary is exclusive (EC-T06)."""

    reason_code = ReasonCode.TOKEN_EXPIRED


class TokenNotYetValidError(TokenError):
    """`now` is before `not_before` (EC-T07)."""

    reason_code = ReasonCode.TOKEN_NOT_YET_VALID


class TokenTooLargeError(TokenError):
    """The token exceeds the hard size limit (spec 01 §9, EC-T11)."""

    reason_code = ReasonCode.TOKEN_TOO_LARGE


class DepthExceededError(TokenError):
    """The chain is deeper than the mandate's `max_depth` (INV-6, EC-T10)."""

    reason_code = ReasonCode.DEPTH_EXCEEDED


class VerificationLimitError(TokenError):
    """The Datalog engine ran out of its resource budget while reading the token.

    Two causes, and the reason code deliberately does not distinguish them because the
    caller's response is the same — fail closed:

    * A pathological token engineered to be expensive (TM-14).
    * A perfectly ordinary token verified while the process was starved of CPU (TM-25).
      `max_time` is **wall clock, not work**, so this is reachable under nothing worse than
      load. Measured at 2 in 42,014 authorize calls before ADR-021 raised the limits.

    It exists as a `TokenError` so a caller that catches `TokenError` keeps getting a
    reason code rather than a raw `biscuit_auth.AuthorizationError` — which is what
    escaped before, and what TM-25 recorded as its residual.
    """

    reason_code = ReasonCode.VERIFICATION_LIMIT_EXCEEDED


class CaveatError(AgentIAMError):
    """A caveat is malformed, or two caveats were compared across kinds.

    Comparing different kinds raises rather than returning False: a silent False would
    let the mint-time check pass a comparison it never actually made (spec 03 §3).
    """


class BudgetError(AgentIAMError):
    """Budget arithmetic failed or would violate an invariant."""


class ScaleError(BudgetError):
    """A value cannot be represented exactly at `BUDGET_SCALE`.

    Money is `NUMERIC(20,4)`; anything finer is a rounding decision, and rounding money
    silently is how ledgers stop balancing. Raise instead.
    """


class CanonicalizationError(AgentIAMError):
    """A value cannot be canonically serialized.

    Raised for floats (never exact), naive datetimes (ambiguous instant), decimals
    exceeding the fixed scale, and types with no defined encoding. Every one of these is
    a bug at the call site rather than something to coerce.
    """


class SinkRejectedRecord(AgentIAMError):
    """A sink will *never* accept this batch, so retrying it is pointless.

    The one distinction a buffered writer cannot make for itself, and getting it wrong is
    how audit records go missing. `DecisionEmitter` retries a failed batch indefinitely,
    because an unreachable database is temporary and giving up on it loses exactly the
    records ADR-026 refuses requests to protect. But a batch the sink will reject forever —
    a value that cannot be encoded, a constraint it can never satisfy — would then wedge
    the queue until the buffer fills and the PEP denies everything.

    Only the sink can tell those apart, because only the sink knows what its failures mean.
    So it raises this for the permanent kind and lets everything else propagate as it is.

    It lives in `agentiam-core` because both sides need to name it: `agentiam-pep` catches
    it and `agentiam-controlplane` raises it, and neither package imports the other — the
    sinks are structural `Protocol`s precisely to keep it that way.

    T-052's CH-1 is why this exists. Before it, `DecisionEmitter` bounded *every* failure by
    `max_retries` and discarded the batch afterwards, so a 30 s database outage was
    indistinguishable from a poison batch: records were dropped roughly every
    `(max_retries + 1) x flush_interval_s`, the queue never filled, back-pressure never
    engaged, and the PEP kept authorizing requests it could no longer record.
    """
