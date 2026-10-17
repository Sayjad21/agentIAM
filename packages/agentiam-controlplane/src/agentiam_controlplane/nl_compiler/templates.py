"""The offline policy compiler — APICTA-01 Part D, T-031.

When the model is unreachable, too slow, or switched off (venue Wi-Fi, a spent free-tier quota,
DEMO.md drill F-2), a sentence in one of the demo's policy shapes still compiles. A deterministic
matcher turns it into the Cedar statements it **adds** to the current policy, plus tests that
pin what it means. Anything outside those shapes is refused with the list of shapes rather than
guessed at: a policy compiled from a sentence the matcher did not understand is worse than none.

**Additions, not policies.** The current policy already permits ordinary work, so a restriction
is a `forbid` appended to it — never a blanket `permit(principal, action, resource);`, which
added to a real policy would allow everything not forbidden. The console appends the statements
(`compose`) and runs the composed policy through the same corpus gate as everything else.

**Nothing from the sentence reaches the Cedar verbatim.** Every value is re-derived from a parsed
form — digits for amounts, `agt-[a-z0-9-]+` for an agent, `[a-z]+` for a role — so nothing an
operator types can end a string literal or a comment. The shapes were written from the demo's
own policy vocabulary, not from the compiler's 30-case evaluation dataset, so that dataset stays
a held-out measure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final

from agentiam_controlplane.nl_compiler.compiler import CompilerTestCase

__all__ = ["SHAPES", "TemplateMatch", "compose", "match"]

#: What the offline compiler understands, for the message an unmatched sentence gets.
SHAPES: Final[tuple[tuple[str, str], ...]] = (
    ("Quarantine an agent", "Quarantine agent agt-payer"),
    ("Cap a payment", "No payments over 100000"),
    ("Cap a payment unless approved", "Payments over 40000 need approval"),
    ("Restrict an action to one role", "Only senior agents may send email"),
    ("Limit an action by delegation depth", "No payments beyond depth 1"),
    ("Block external email", "No external email"),
)


@dataclass(frozen=True, slots=True)
class TemplateMatch:
    """What a sentence compiled to: statements to add, a summary, and the tests."""

    shape: str
    summary: str
    cedar: str
    tests: list[CompilerTestCase] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Scope:
    operation: str
    tool: str
    noun: str


_PAYMENT = _Scope("payment:initiate", "payment_api", "payments")
_EMAIL = _Scope("email:send", "email_internal", "email")
_INVOICE_READ = _Scope("invoice:read", "invoice_api", "invoice reads")
_INVOICE_WRITE = _Scope("invoice:write", "invoice_api", "invoice writes")
_VENDOR_READ = _Scope("vendor:read", "vendor_api", "vendor reads")

_NEGATION = re.compile(
    r"\b(no|not|never|must\s+not|may\s+not|mustn't|cannot|can't|forbid\w*|block\w*|deny|"
    r"disallow\w*|prohibit\w*|stop\w*|ban\w*)\b"
)
_APPROVAL = re.compile(r"\b(approv\w*|elevat\w*|sign[- ]?off)\b")
_AGENT = re.compile(r"\b(agt-[a-z0-9][a-z0-9-]{0,40})\b")
_QUARANTINE = re.compile(
    r"\b(quarantin\w*|block\w*|ban\w*|suspend\w*|freez\w*|stop\w*|disabl\w*|isolat\w*|"
    r"cut\s+off|deny|revoke\w*)\b"
)
_COMPARATOR = re.compile(
    r"\b(?:over|above|more\s+than|exceed\w*|greater\s+than|beyond|larger\s+than)\s+"
    r"(?:৳|tk\.?\s*|bdt\s*)?([0-9][0-9,_]*(?:\.[0-9]+)?)\s*(k|lakhs?|lacs?|crores?)?\b"
)
_ONLY_ROLE = re.compile(
    r"\bonly\s+(?:the\s+)?([a-z]+?)s?\s+(?:agents?\s+)?(?:may|can|are\s+allowed\s+to|should)\s+(.+)"
)
_DEPTH_STRICT = re.compile(r"\b(?:deeper\s+than|beyond|below)\s+(?:depth\s+)?([0-9]{1,2})\b")
_DEPTH_INCLUSIVE = re.compile(
    r"\b(?:at\s+|from\s+)?depth\s+([0-9]{1,2})\s+or\s+(?:deeper|more|below|greater|beyond)\b"
)
_EXTERNAL_EMAIL = re.compile(
    r"\bexternal\s+e-?mails?\b|\be-?mails?\s+(?:to\s+)?outside\b|\be-?mail\s+outside\b"
)

_MULTIPLIERS: Final = {"k": 1_000, "lakh": 100_000, "lac": 100_000, "crore": 10_000_000}


def match(sentence: str) -> TemplateMatch | None:
    """Compile `sentence` if it is one of `SHAPES`, else `None` — never a guess."""
    text = " ".join(sentence.lower().split())
    if not text:
        return None
    for matcher in (_quarantine, _payment_cap, _depth_limit, _external_email, _only_role):
        found = matcher(text)
        if found is not None:
            return found
    return None


def compose(current_policy: str, found: TemplateMatch) -> str:
    """The current policy with `found`'s statements appended and labelled."""
    return (
        f"{current_policy.rstrip()}\n\n"
        f"// added by the offline compiler: {found.summary}\n"
        f"{found.cedar}\n"
    )


# --- the shapes ---------------------------------------------------------------------------


def _quarantine(text: str) -> TemplateMatch | None:
    agent = _AGENT.search(text)
    if agent is None or _QUARANTINE.search(text) is None:
        return None
    agent_id = agent.group(1)
    other = "agt-1" if agent_id != "agt-1" else "agt-2"
    return TemplateMatch(
        shape="Quarantine an agent",
        summary=f"quarantine {agent_id}",
        cedar=f'forbid(principal == Agent::"{agent_id}", action, resource);',
        tests=[
            _test(
                f"{agent_id}_is_refused",
                f"{agent_id} may do nothing",
                False,
                _INVOICE_READ,
                agent_id=agent_id,
            ),
            _test(
                "another_agent_is_unaffected",
                "any other agent keeps its access",
                True,
                _INVOICE_READ,
                agent_id=other,
            ),
        ],
    )


def _payment_cap(text: str) -> TemplateMatch | None:
    if _scope(text) is not _PAYMENT:
        return None
    comparator = _COMPARATOR.search(text)
    if comparator is None:
        return None
    approval = _APPROVAL.search(text) is not None
    if not approval and _NEGATION.search(text) is None:
        return None
    cap = _amount(comparator.group(1), comparator.group(2))
    if cap is None:
        return None
    literal = f"{cap:.1f}" if cap == cap.to_integral_value() else f"{cap}"
    condition = f'decimal("{literal}").lessThan(context.amount)'
    if approval:
        condition += " && !context.elevated"
    above = cap + 1
    within = min(cap, Decimal(100))
    tests = [
        _test(
            "above_the_cap_is_refused",
            f"a payment of {above} is refused",
            False,
            _PAYMENT,
            amount=above,
            role="senior",
        ),
        _test(
            "within_the_cap_is_allowed",
            f"a payment of {within} is allowed",
            True,
            _PAYMENT,
            amount=within,
            role="senior",
        ),
    ]
    if approval:
        tests.append(
            _test(
                "above_the_cap_with_approval_is_allowed",
                "an approved payment above the cap is allowed",
                True,
                _PAYMENT,
                amount=above,
                role="senior",
                elevated=True,
            )
        )
    return TemplateMatch(
        shape="Cap a payment unless approved" if approval else "Cap a payment",
        summary=f"payments above {_money(cap)}" + (" need approval" if approval else " forbidden"),
        cedar=f'forbid(principal, action == Action::"{_PAYMENT.operation}", resource)\n'
        f"when {{ {condition} }};",
        tests=tests,
    )


def _depth_limit(text: str) -> TemplateMatch | None:
    strict = _DEPTH_STRICT.search(text)
    inclusive = _DEPTH_INCLUSIVE.search(text)
    if strict is not None:
        limit, operator, deepest_allowed = int(strict.group(1)), ">", int(strict.group(1))
    elif inclusive is not None:
        limit, operator = int(inclusive.group(1)), ">="
        deepest_allowed = limit - 1
    else:
        return None
    if _NEGATION.search(text) is None:
        return None
    scope = _scope(text)
    if scope is None:
        return None
    role = "senior"  # critical tools need a senior, and the test is about depth, not role
    return TemplateMatch(
        shape="Limit an action by delegation depth",
        summary=f"no {scope.noun} at depth {operator} {limit}",
        cedar=f'forbid(principal, action == Action::"{scope.operation}", resource)\n'
        f"when {{ principal.depth {operator} {limit} }};",
        tests=[
            _test(
                "too_deep_is_refused",
                f"depth {deepest_allowed + 1} is refused",
                False,
                scope,
                depth=deepest_allowed + 1,
                role=role,
            ),
            _test(
                "shallow_enough_is_allowed",
                f"depth {max(deepest_allowed, 1)} is allowed",
                True,
                scope,
                depth=max(deepest_allowed, 1),
                role=role,
            ),
        ],
    )


def _external_email(text: str) -> TemplateMatch | None:
    if _EXTERNAL_EMAIL.search(text) is None or _NEGATION.search(text) is None:
        return None
    return TemplateMatch(
        shape="Block external email",
        summary="no external email",
        cedar=f'forbid(principal, action == Action::"{_EMAIL.operation}", resource)\n'
        "when { resource.is_external };",
        tests=[
            _test(
                "external_email_is_refused",
                "email leaving the company is refused",
                False,
                _EMAIL,
                tool="email_external",
            ),
            _test("internal_email_is_allowed", "internal email still goes out", True, _EMAIL),
        ],
    )


def _only_role(text: str) -> TemplateMatch | None:
    found = _ONLY_ROLE.search(text)
    if found is None:
        return None
    role, rest = found.group(1), found.group(2)
    if not re.fullmatch(r"[a-z]{2,20}", role):
        return None
    scope = _scope(rest)
    if scope is None:
        return None
    other = "worker" if role != "worker" else "auditor"
    return TemplateMatch(
        shape="Restrict an action to one role",
        summary=f"only {role} agents may perform {scope.noun}",
        cedar=f'forbid(principal, action == Action::"{scope.operation}", resource)\n'
        f'when {{ principal.role != "{role}" }};',
        tests=[
            _test(f"{role}_is_allowed", f"a {role} agent may", True, scope, role=role),
            _test(f"{other}_is_refused", f"a {other} agent may not", False, scope, role=other),
        ],
    )


# --- parsing helpers ----------------------------------------------------------------------


def _scope(text: str) -> _Scope | None:
    if re.search(r"\bpay(?:ment)?s?\b|\btransfer\w*", text):
        return _PAYMENT
    if re.search(r"\be-?mails?\b", text):
        return _EMAIL
    if re.search(r"\binvoices?\b", text):
        if re.search(r"\b(write|edit|modify|update|create|change)\w*\b", text):
            return _INVOICE_WRITE
        return _INVOICE_READ
    if re.search(r"\b(vendors?|suppliers?)\b", text):
        return _VENDOR_READ
    return None


def _amount(number: str, unit: str | None) -> Decimal | None:
    try:
        value = Decimal(number.replace(",", "").replace("_", ""))
    except InvalidOperation:
        return None
    if unit:
        value *= _MULTIPLIERS[unit.rstrip("s")]
    return value if value >= 0 else None


def _money(value: Decimal) -> str:
    return f"৳{value:,.0f}" if value == value.to_integral_value() else f"৳{value:,}"


def _test(
    name: str,
    description: str,
    expected: bool,
    scope: _Scope,
    *,
    role: str = "worker",
    amount: Decimal = Decimal(0),
    depth: int = 1,
    tool: str | None = None,
    elevated: bool = False,
    agent_id: str = "agt-1",
) -> CompilerTestCase:
    return CompilerTestCase(
        name=name,
        description=description,
        expected=expected,
        role=role,
        operation=scope.operation,
        tool=tool or scope.tool,
        amount=str(amount),
        depth=depth,
        elevated=elevated,
        agent_id=agent_id,
    )
