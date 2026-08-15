"""The policy test corpus — T-026: ≥50 cases derived from the demo workflows.

Every case is tied to a demo beat or a general-safety category. The `tags` field
links each case to its origin in `DEMO.md`, so a judge asking "how do you know this
policy works for the demo?" gets a traceable answer.

The bundle under test is the same one used since T-024's conformance suite — the
"real" organization policy that the demo runs against. The cases here absorb and
extend the 32-case corpus from `test_pep_policy.py::TestConformanceCorpus`.

Each case has a `description` explaining *why* the expected outcome is correct. A
corpus whose rows nobody can explain is a corpus nobody will maintain when the policy
changes.

**Rule 4**: money is `Decimal`, never `float`.
"""

from __future__ import annotations

from decimal import Decimal

from agentiam_core.policy_testing import PolicyTestCase

__all__ = ["CORPUS", "CORPUS_SOURCE", "CORPUS_TOOLS"]

#: The Cedar source the corpus is written against — the demo's organization policy.
#: This is the same source used in `test_pep_policy.py` and by the e2e thin slice.
CORPUS_SOURCE = """\
permit(principal, action == Action::"invoice:read", resource);
permit(principal, action == Action::"vendor:read", resource);

permit(principal, action == Action::"invoice:write", resource)
when { principal.role == "senior" };

permit(principal, action == Action::"payment:initiate", resource)
when {
  context.amount.lessThanOrEqual(decimal("500000.0")) && principal.depth <= 2
};

permit(principal, action == Action::"email:send", resource)
when { !resource.is_external };

forbid(principal, action, resource)
when { resource.sensitivity == "critical" && principal.role != "senior" };

forbid(principal, action == Action::"payment:initiate", resource)
when { decimal("1000000.0").lessThan(context.amount) };
"""

#: Tool catalogue matching the corpus. Shared with `test_pep_policy.py`.
CORPUS_TOOLS: dict[str, dict[str, object]] = {
    "invoice_api": {"tool_id": "invoice_api", "server": "erp", "sensitivity": "low"},
    "vendor_api": {"tool_id": "vendor_api", "server": "erp", "sensitivity": "low"},
    "payment_api": {
        "tool_id": "payment_api",
        "server": "bank",
        "sensitivity": "critical",
        "is_external": True,
    },
    "email_internal": {"tool_id": "email_internal", "server": "mail", "sensitivity": "low"},
    "email_external": {
        "tool_id": "email_external",
        "server": "mail",
        "sensitivity": "medium",
        "is_external": True,
    },
}


def _c(
    name: str,
    description: str,
    operation: str,
    expected: bool,
    *,
    role: str = "worker",
    tool: str | None = "invoice_api",
    amount: str = "0",
    depth: int = 1,
    tags: tuple[str, ...] = (),
) -> PolicyTestCase:
    """Shorthand for constructing a case. Amount is a string to avoid float."""
    return PolicyTestCase(
        name=name,
        description=description,
        operation=operation,
        expected=expected,
        role=role,
        tool=tool,
        amount=Decimal(amount),
        depth=depth,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# The corpus. ≥50 cases, each with a name, description, expected, and tags.
# ---------------------------------------------------------------------------

CORPUS: tuple[PolicyTestCase, ...] = (
    # -------------------------------------------------------------------------
    # Beat 1 — Human approves a task: unconditional permits
    # -------------------------------------------------------------------------
    _c(
        "beat1_worker_reads_invoices",
        "unconditional permit; invoice:read is always allowed",
        "invoice:read",
        True,
        tags=("beat-1", "unconditional"),
    ),
    _c(
        "beat1_worker_reads_vendors",
        "unconditional permit; vendor:read is always allowed",
        "vendor:read",
        True,
        tool="vendor_api",
        tags=("beat-1", "unconditional"),
    ),
    _c(
        "beat1_senior_reads_invoices",
        "role is irrelevant for an unconditional permit",
        "invoice:read",
        True,
        role="senior",
        tags=("beat-1", "unconditional"),
    ),
    _c(
        "beat1_deep_agent_reads_invoices",
        "depth is irrelevant for an unconditional permit",
        "invoice:read",
        True,
        depth=8,
        tags=("beat-1", "unconditional"),
    ),
    # -------------------------------------------------------------------------
    # Beat 2 — Delegation tree grows: role-conditioned permits
    # -------------------------------------------------------------------------
    _c(
        "beat2_senior_writes_invoices",
        "the role guard is satisfied",
        "invoice:write",
        True,
        role="senior",
        tags=("beat-2", "role-conditioned"),
    ),
    _c(
        "beat2_worker_cannot_write_invoices",
        "the role guard fails for a worker",
        "invoice:write",
        False,
        tags=("beat-2", "role-conditioned"),
    ),
    _c(
        "beat2_empty_role_cannot_write",
        "empty string is not 'senior'",
        "invoice:write",
        False,
        role="",
        tags=("beat-2", "role-conditioned"),
    ),
    _c(
        "beat2_admin_role_cannot_write",
        "any non-senior role fails the guard",
        "invoice:write",
        False,
        role="admin",
        tags=("beat-2", "role-conditioned"),
    ),
    _c(
        "beat2_senior_reads_vendors",
        "senior can do everything a worker can, plus more",
        "vendor:read",
        True,
        role="senior",
        tool="vendor_api",
        tags=("beat-2", "role-conditioned"),
    ),
    # -------------------------------------------------------------------------
    # Beat 3 — Least privilege enforced: default deny, deny causes
    # -------------------------------------------------------------------------
    _c(
        "beat3_worker_cannot_admin",
        "no permit mentions admin:write — deny by default",
        "admin:write",
        False,
        tags=("beat-3", "default-deny"),
    ),
    _c(
        "beat3_senior_cannot_admin",
        "seniority does not grant admin access — no permit mentions it",
        "admin:write",
        False,
        role="senior",
        tags=("beat-3", "default-deny"),
    ),
    _c(
        "beat3_worker_cannot_negotiate",
        "vendor:negotiate is not vendor:read — deny by default",
        "vendor:negotiate",
        False,
        tool="vendor_api",
        tags=("beat-3", "default-deny"),
    ),
    _c(
        "beat3_worker_cannot_delete",
        "invoice:delete is not permitted anywhere",
        "invoice:delete",
        False,
        tags=("beat-3", "default-deny"),
    ),
    # -------------------------------------------------------------------------
    # Beat 4 — Judge sets the ceiling: amount + depth conditions
    # -------------------------------------------------------------------------
    _c(
        "beat4_payment_under_ceiling",
        "৳1,000 is well under ৳500,000",
        "payment:initiate",
        True,
        amount="1000",
        tags=("beat-4", "amount-bounded"),
    ),
    _c(
        "beat4_payment_at_ceiling",
        "৳500,000 exactly — the boundary is inclusive",
        "payment:initiate",
        True,
        amount="500000",
        tags=("beat-4", "boundary"),
    ),
    _c(
        "beat4_payment_one_over_ceiling",
        "৳500,001 — one taka over the ceiling",
        "payment:initiate",
        False,
        amount="500001",
        tags=("beat-4", "boundary"),
    ),
    _c(
        "beat4_payment_well_over_ceiling",
        "৳750,000 — clearly above the ceiling",
        "payment:initiate",
        False,
        amount="750000",
        tags=("beat-4", "amount-bounded"),
    ),
    _c(
        "beat4_payment_at_max_depth",
        "depth 2 is the maximum allowed",
        "payment:initiate",
        True,
        depth=2,
        tags=("beat-4", "boundary"),
    ),
    _c(
        "beat4_payment_one_past_max_depth",
        "depth 3 — one past the maximum",
        "payment:initiate",
        False,
        depth=3,
        tags=("beat-4", "boundary"),
    ),
    _c(
        "beat4_payment_far_past_max_depth",
        "depth 8 — far past the maximum",
        "payment:initiate",
        False,
        depth=8,
        tags=("beat-4", "depth-bounded"),
    ),
    _c(
        "beat4_senior_payment_under_ceiling",
        "seniority does not bypass the amount ceiling",
        "payment:initiate",
        True,
        role="senior",
        amount="1000",
        tags=("beat-4", "amount-bounded"),
    ),
    _c(
        "beat4_senior_payment_over_ceiling",
        "nor does seniority raise the ceiling",
        "payment:initiate",
        False,
        role="senior",
        amount="500001",
        tags=("beat-4", "amount-bounded"),
    ),
    _c(
        "beat4_payment_zero_amount",
        "৳0 is a valid (degenerate) amount — below the ceiling",
        "payment:initiate",
        True,
        amount="0",
        tags=("beat-4", "edge-case"),
    ),
    _c(
        "beat4_payment_fractional_amount",
        "৳499999.9999 — fractional taka, under ceiling",
        "payment:initiate",
        True,
        amount="499999.9999",
        tags=("beat-4", "edge-case"),
    ),
    # -------------------------------------------------------------------------
    # Beat 5 — Judge writes a policy: cross-cutting interactions
    # -------------------------------------------------------------------------
    _c(
        "beat5_senior_depth1_under_ceiling",
        "all three conditions met simultaneously",
        "payment:initiate",
        True,
        role="senior",
        amount="100000",
        depth=1,
        tags=("beat-5", "multi-condition"),
    ),
    _c(
        "beat5_worker_depth1_at_ceiling",
        "role irrelevant here; amount and depth both at boundary",
        "payment:initiate",
        True,
        amount="500000",
        depth=1,
        tags=("beat-5", "multi-condition"),
    ),
    _c(
        "beat5_worker_deep_under_ceiling",
        "amount OK but depth too deep",
        "payment:initiate",
        False,
        amount="1000",
        depth=4,
        tags=("beat-5", "multi-condition"),
    ),
    _c(
        "beat5_worker_shallow_over_ceiling",
        "depth OK but amount too high",
        "payment:initiate",
        False,
        amount="600000",
        depth=1,
        tags=("beat-5", "multi-condition"),
    ),
    # -------------------------------------------------------------------------
    # Beat 7 — Revocation: resource-conditioned permits (email)
    # -------------------------------------------------------------------------
    _c(
        "beat7_internal_email_allowed",
        "internal tool is not external — permit applies",
        "email:send",
        True,
        tool="email_internal",
        tags=("beat-7", "resource-conditioned"),
    ),
    _c(
        "beat7_external_email_denied",
        "external tool — is_external is true, permit guard fails",
        "email:send",
        False,
        tool="email_external",
        tags=("beat-7", "resource-conditioned"),
    ),
    _c(
        "beat7_senior_external_email_denied",
        "role does not override the resource condition",
        "email:send",
        False,
        role="senior",
        tool="email_external",
        tags=("beat-7", "resource-conditioned"),
    ),
    # -------------------------------------------------------------------------
    # Beat 3/8 — Forbid beats permit: sensitivity and amount forbids
    # -------------------------------------------------------------------------
    _c(
        "forbid_critical_tool_worker",
        "critical tool, non-senior: forbid wins over the unconditional permit",
        "invoice:read",
        False,
        tool="payment_api",
        tags=("beat-3", "forbid-wins"),
    ),
    _c(
        "forbid_critical_tool_senior_allowed",
        "critical tool, senior: the forbid's guard does not fire",
        "invoice:read",
        True,
        role="senior",
        tool="payment_api",
        tags=("beat-8", "forbid-wins"),
    ),
    _c(
        "forbid_critical_tool_payment_worker",
        "would be permitted on amount, but the sensitivity forbid fires",
        "payment:initiate",
        False,
        tool="payment_api",
        amount="1000",
        tags=("beat-3", "forbid-wins"),
    ),
    _c(
        "forbid_critical_tool_payment_senior",
        "senior escapes the sensitivity forbid and is under the ceiling",
        "payment:initiate",
        True,
        role="senior",
        tool="payment_api",
        amount="1000",
        tags=("beat-8", "forbid-wins"),
    ),
    _c(
        "forbid_amount_over_million",
        "the ৳1,000,000 forbid applies to everyone, including seniors",
        "payment:initiate",
        False,
        role="senior",
        tool="payment_api",
        amount="1000001",
        tags=("beat-4", "forbid-amount"),
    ),
    _c(
        "forbid_amount_over_million_non_critical_tool",
        "the amount forbid is about the amount, not the tool",
        "payment:initiate",
        False,
        role="senior",
        amount="1000001",
        tags=("beat-4", "forbid-amount"),
    ),
    _c(
        "forbid_amount_at_million_boundary",
        "at ৳1,000,000 the permit's own ceiling has already failed",
        "payment:initiate",
        False,
        role="senior",
        amount="1000000",
        tags=("beat-4", "boundary"),
    ),
    _c(
        "forbid_amount_just_under_million",
        "৳999,999 — under the forbid but above the permit ceiling",
        "payment:initiate",
        False,
        role="senior",
        amount="999999",
        tags=("beat-4", "boundary"),
    ),
    # -------------------------------------------------------------------------
    # General safety — unknown tools, null tools, edge cases
    # -------------------------------------------------------------------------
    _c(
        "unknown_tool_defaults_safe",
        "unknown tools default to sensitivity=low, is_external=false",
        "invoice:read",
        True,
        tool="unknown_tool",
        tags=("safety", "unknown-tool"),
    ),
    _c(
        "no_tool_at_all",
        "a call with no tool is treated the same as an unknown tool",
        "invoice:read",
        True,
        tool=None,
        tags=("safety", "unknown-tool"),
    ),
    _c(
        "unknown_tool_vendor_read",
        "vendor:read also works with an unknown tool",
        "vendor:read",
        True,
        tool="unknown_tool",
        tags=("safety", "unknown-tool"),
    ),
    _c(
        "amount_irrelevant_for_read",
        "a high amount does not affect vendor:read — amount is not in its permit",
        "vendor:read",
        True,
        tool="vendor_api",
        amount="999999",
        tags=("safety", "edge-case"),
    ),
    _c(
        "sensitivity_forbid_on_vendor_read",
        "vendor:read on a critical tool is still denied by the sensitivity forbid",
        "vendor:read",
        False,
        tool="payment_api",
        tags=("safety", "forbid-wins"),
    ),
    # -------------------------------------------------------------------------
    # Depth boundaries on unconditioned actions
    # -------------------------------------------------------------------------
    _c(
        "depth8_invoice_read",
        "invoice:read has no depth condition — depth 8 is fine",
        "invoice:read",
        True,
        depth=8,
        tags=("safety", "depth-irrelevant"),
    ),
    _c(
        "depth1_payment_zero_amount",
        "payment at depth 1 with ৳0 — both conditions met",
        "payment:initiate",
        True,
        depth=1,
        amount="0",
        tags=("safety", "edge-case"),
    ),
    _c(
        "depth2_payment_exact_ceiling",
        "depth 2 with ৳500,000 — both conditions at their boundary",
        "payment:initiate",
        True,
        depth=2,
        amount="500000",
        tags=("safety", "boundary"),
    ),
    _c(
        "depth2_payment_one_over",
        "depth 2 OK, but ৳500,001 exceeds the ceiling",
        "payment:initiate",
        False,
        depth=2,
        amount="500001",
        tags=("safety", "boundary"),
    ),
    _c(
        "depth3_payment_zero",
        "depth 3 exceeds max_depth even with ৳0",
        "payment:initiate",
        False,
        depth=3,
        amount="0",
        tags=("safety", "boundary"),
    ),
    # -------------------------------------------------------------------------
    # Unrecognized actions
    # -------------------------------------------------------------------------
    _c(
        "unrecognized_action_worker",
        "an action with no matching permit is denied by default",
        "system:shutdown",
        False,
        tags=("safety", "default-deny"),
    ),
    _c(
        "unrecognized_action_senior",
        "seniority does not help with an unrecognized action",
        "system:shutdown",
        False,
        role="senior",
        tags=("safety", "default-deny"),
    ),
)

if len(CORPUS) < 50:  # pragma: no cover — guarded at definition time
    msg = f"T-026 requires ≥50 cases; have {len(CORPUS)}"
    raise RuntimeError(msg)
