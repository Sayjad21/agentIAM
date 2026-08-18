"""Fold the submission's evidence into one HTML bundle — T-055, `PLAN.md` §14.

`PLAN.md` §14.1 lists twelve things the evidence pack must carry. Some of them are files
this repository already produces and proves correct on their own (`chaos-results.md`,
`performance.md`, `security-scan.md`, `sbom.json`, `threat-model.md`) — this script's job
for those is to *fold*, not re-derive: it imports the sibling generators
(`generate_chaos_results`, `generate_benchmark_results`) and reads the other files verbatim,
so nothing here can quietly disagree with the document it is summarizing.

The rest — the formal invariants table, the PB-1..PB-12 coverage table, and the A-01..A-33
red-team table — have no committed machine-readable source, because they are themselves
summaries of prose spread across `docs/specs/03-attenuation.md`, `PLAN.md` §13.1 and §12.
They are hardcoded here the same way `generate_chaos_results.SCENARIOS` hardcodes
`PLAN.md` §13.2's twelve scenarios: a literal transcription, not a derivation, checked
against the source documents by `tests/unit/test_generate_evidence_pack.py`.

**No new dependency.** `PLAN.md` T-055 asks for "a single PDF/HTML bundle" — HTML satisfies
that literally, needs nothing beyond the standard library, and opens identically on Linux,
Windows and macOS with no native rendering toolchain to install (a PDF library such as
`weasyprint` pulls in Cairo/Pango system packages; `reportlab`/`fpdf2` avoid that but are
still a dependency this ticket does not need to add). A judge who wants a PDF can print the
page from any browser. Every value embedded from another file is escaped with
:func:`_escape` before being placed in the page, so nothing here can inject markup.

**No fabrication.** Where `PLAN.md` §14.1 asks for something that has not actually been
built — the drift model card (T-034/T-035 are deferred, `ADR-036`), a `mutmut` report
(`STATUS.md` gap 4, never run), a live audit-tamper transcript (would need a real database
this script does not assume), OSS traction (pre-release) — the corresponding section says so
plainly, with a citation, rather than presenting an empty or invented substitute as if it
were evidence. Rule 9 in spirit: a missing measurement is not a passing one.

Same `--check` habit as `generate_chaos_results.py` / `generate_benchmark_results.py` /
`generate_sbom.py`: this script renders from disk and nobody hand-edits the output.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Final

# Every sibling generate_*.py is invoked as `python scripts/generate_x.py`, which puts
# `scripts/` (not the repo root) at sys.path[0] — pytest's `pythonpath = ["."]` config is
# what makes `from scripts import ...` work under test, and that setting does not apply to
# a bare interpreter invocation. This is the first script under `scripts/` that imports a
# sibling module rather than only `agentiam_*` packages, so it is the first to need the
# repo root on sys.path explicitly, checked before the import that needs it.
_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import generate_benchmark_results, generate_chaos_results  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Sequence

_DOCS: Final = _REPO_ROOT / "docs"
_OUTPUT: Final = _DOCS / "evidence" / "evidence-pack.html"

_SECURITY_SCAN: Final = _DOCS / "evidence" / "security-scan.md"
_SBOM: Final = _DOCS / "evidence" / "sbom.json"
_THREAT_MODEL: Final = _DOCS / "threat-model.md"

#: `PLAN.md` §3.1's component diagram, transcribed verbatim so the evidence pack does not
#: depend on a diagramming tool this project has no other use for.
_ARCHITECTURE_DIAGRAM: Final = """\
+----------------------------------------------------------------------+
| CLIENT SIDE                                                          |
|                                                                        |
|  Reference agent (procurement)          agentiam-sdk (Python)        |
|    root agent --spawns--> sub-agents      . attenuate()              |
|         |                                 . spend context mgr        |
|         +-- all tool calls --------+      . escalate()               |
+---------------------------------------|-------------------------------+
                                        |
                                        v
+----------------------------------------------------------------------+
| DATA PLANE -- PEP (agentiam-pep)         [HOT PATH, must be fast]    |
|                                                                        |
|  1. extract token          (us)                                      |
|  2. verify biscuit chain   (us)   <- offline, public key only        |
|  3. check revocation       (us)   <- in-memory bloom + set           |
|  4. evaluate token Datalog (us)   <- biscuit authorizer               |
|  5. evaluate Cedar policy  (us)   <- in-process, cached bundle        |
|  6. drift check            (cached / async / inline-strict)          |
|  7. reserve from lease     (us)   <- local counter, no network       |
|  8. forward upstream       (network)                                  |
|  9. commit/refund actual   (us)                                      |
| 10. emit decision record   (async, buffered)                         |
+---------------+-------------------------------------------+-----------+
                | async: leases, revocation, records         | upstream
                v                                            v
+----------------------------------------------+  +----------------------+
| CONTROL PLANE (FastAPI services)              |  | TOOL SERVERS         |
|                                                |  | MCP servers,         |
|  issuance     -- mandates, root tokens        |  | internal APIs,       |
|  ledger       -- budgets, leases, invariant   |  | stub PSP             |
|  policy       -- Cedar bundles, versioning    |  +----------------------+
|  compiler     -- NL->Cedar + verifier         |
|  drift        -- embeddings + classifier      |
|  revocation   -- revoke, subtree, gossip      |
|  audit        -- hash-chained ledger          |
|  escalation   -- approval workflow            |
|                                                |
|  Postgres . Redis . Ollama . MinIO(opt)       |
+----------------------------------------------------------------------+
                |
                v
+----------------------------------------------------------------------+
| SURFACES & OBSERVABILITY                                             |
|  Admin console (FastAPI + Jinja2 + HTMX)                             |
|  OTEL Collector -> Tempo (traces) . Prometheus (metrics)             |
|  Grafana dashboards: decisions, budgets                              |
|  Keycloak (human OIDC)                                               |
+----------------------------------------------------------------------+
"""

#: The 10 specs — `docs/specs/`. `written_by` is the ticket that wrote it, `STATUS.md`.
_SPECS: Final[list[tuple[str, str, str]]] = [
    ("01", "token-format", "Authority/attenuation block structure, money encoding, size limits"),
    ("02", "caveat-language", "The 9 caveat types, Datalog compilation, check-if vs reject-if"),
    ("03", "attenuation", "The narrows() partial order, the mint-time check, INV-1..INV-10"),
    ("04", "lease-protocol", "All 7 ledger operations, safety/liveness argument, clock skew"),
    ("05", "policy", "Cedar `PolicyEngine`, the NoDecision trap, bundle signing and rollback"),
    ("06", "drift-detection", "Stateless intent binding, rule-based v0, f1/f2/f5 features"),
    ("07", "revocation", "Token/subtree/mandate revocation as one mechanism, push+pull"),
    ("08", "audit-chain", "The hash chain, serialized appends, head-truncation detection"),
    ("09", "decision-record", "The 10-step pipeline and its precedence contract"),
    (
        "10",
        "scope-extraction",
        "HTTP -> RequestContext, and where the PEP can disagree with"
        " the upstream about what a request means",
    ),
]

#: `docs/specs/03-attenuation.md` §4 (mechanism) and §6 (property test mapping). INV-4's
#: test id is `None`: it is verified by T-007's tamper/wrong-key/truncation unit tests,
#: not by a numbered P-xx in spec 03's own table — citing a P-id here would name a test
#: that does not test this invariant.
INVARIANTS: Final[list[tuple[str, str, str, str | None]]] = [
    (
        "INV-1",
        "Monotonicity",
        "Block facts are scoped and invisible to earlier blocks' checks; authority is the "
        "intersection over all blocks in the chain.",
        "P-01",
    ),
    (
        "INV-2",
        "Transitivity",
        "INV-1 applied inductively: each further attenuation can only shrink the "
        "intersection already established.",
        "P-02",
    ),
    (
        "INV-3",
        "Offline soundness",
        "Verification needs only the root public key and the token bytes -- no network, "
        "no database, no shared state.",
        "P-05",
    ),
    (
        "INV-4",
        "Non-forgeability",
        "Per-block Ed25519 signatures over the preceding chain. Verified: wrong key, a "
        "single flipped bit, and truncation are all rejected.",
        None,
    ),
    (
        "INV-5",
        "Budget subadditivity",
        "Not enforceable in-token by design -- siblings may each hold the full parent "
        "ceiling. Enforced dynamically by the lease ledger (spec 04).",
        "P-10",
    ),
    (
        "INV-6",
        "Depth bound",
        "current_depth = block_count - 1, computed by the verifier, never read from a "
        "block's own (attacker-writable) depth fact.",
        "P-06",
    ),
    (
        "INV-7",
        "Intent stability",
        "intent_hash is set once in the authority block; a later block's intent fact is "
        "invisible to the authority block's own check.",
        "P-07",
    ),
    (
        "INV-8",
        "Deny precedence",
        "Every check in every block must pass; any reject anywhere in the chain wins, "
        "regardless of any allow.",
        "P-09",
    ),
    (
        "INV-9",
        "Expiry contraction",
        "Every block's TimeWindow upper bound is a check that must pass, so the "
        "effective expiry is the minimum across the chain.",
        "P-08",
    ),
    (
        "INV-10",
        "No resurrection",
        "A token is revoked if any block id in its chain is in the revoked set, which "
        "gives subtree revocation for free.",
        "P-21",
    ),
]

#: `PLAN.md` §13.1. `status` states plainly what has and has not been measured; PB-9 and
#: PB-10 have real numbers in prose (JOURNAL.md, DECISIONS.md) but no committed JSON
#: artifact of their own the way PB-1/PB-2/PB-3 do, so they are cited rather than folded.
BENCHMARKS: Final[list[tuple[str, str, str]]] = [
    (
        "PB-1",
        "Pure decision latency, warm (pytest-benchmark)",
        "measured -- see NFR-1 in the Benchmarks section below (decide() against the real "
        "Cedar engine)",
    ),
    (
        "PB-2",
        "Per-step breakdown (verify / revocation / Datalog / Cedar / lease)",
        "measured -- see the step table below",
    ),
    (
        "PB-3",
        "End-to-end proxy overhead, 500 RPS (Locust-equivalent driver)",
        "not established at 500 RPS on this host -- see NFR-2 below (ADR-052)",
    ),
    (
        "PB-4",
        "Throughput per PEP instance, report the knee",
        "not measured -- needs the load generator off-box (ADR-052)",
    ),
    ("PB-5", "Latency vs token depth (1..8)", "not measured"),
    (
        "PB-6",
        "Cedar vs OPA decision latency",
        "not measured -- OpaEngine is a stub (spec 05 §7); full OPA deferred (PLAN.md §21)",
    ),
    ("PB-7", "Ledger acquire throughput", "not measured"),
    (
        "PB-8",
        "Top-up RPS vs lease sizing policy",
        "not measured -- adaptive lease sizing (T-015) is deferred (PLAN.md §21)",
    ),
    (
        "PB-9",
        "Revocation propagation time, 3 PEPs",
        "measured, not a committed benchmark artifact -- p99 ranged 11 us to 16 ms across "
        "5 runs, loopback-only (T-039/T-040, ADR-044, ADR-045)",
    ),
    (
        "PB-10",
        "Drift scoring latency (cached vs cold)",
        "measured, not a committed benchmark artifact -- cold 14,244 ms; warm median "
        "17.8 ms / p95 83.4 ms (ADR-037, spec 06 §4.1)",
    ),
    ("PB-11", "Memory under 10k revocations + 100 policies", "not measured"),
    (
        "PB-12",
        "Cold start",
        "measured each CI run in the infrastructure job's step summary (T-001 budget: "
        "90 s; observed ~69 s cold / ~16 s on CI), not committed as a file",
    ),
]

#: `PLAN.md` §12, transcribed. `tested_by` cites the actual test file per `ADR-048` and the
#: red-team suite's own module docstrings, not a guess -- attacks outside T-051's named
#: scope (`ROADMAP.md` line 288) are marked as covered elsewhere or not dedicated-tested,
#: matching `tests/security/test_redteam_suite.py` and `tests/integration/
#: test_redteam_suite.py`'s own statements about what they do and do not cover.
RED_TEAM: Final[list[tuple[str, str, str, str, str]]] = [
    (
        "A-01",
        "Token",
        "Forge a block without the parent key",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-02",
        "Token",
        "Strip an attenuation block to widen authority",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    ("A-03", "Token", "Reorder blocks", "mitigated", "tests/security/test_redteam_suite.py"),
    (
        "A-04",
        "Token",
        "Splice blocks from two different tokens",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-05",
        "Token",
        "Replay an expired token",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-06",
        "Token",
        "Replay a valid token from a different agent",
        "accepted risk",
        "tests/security/test_redteam_suite.py (TM-01, bearer semantics)",
    ),
    (
        "A-07",
        "Token",
        "Algorithm-confusion / downgrade attempt",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-08",
        "Token",
        "Oversized token as a DoS vector",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-09",
        "Token",
        "Deeply nested chain (depth 100) as a DoS vector",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-10",
        "Privilege escalation",
        "Sub-agent requests a scope its parent lacks",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-11",
        "Privilege escalation",
        "Sub-agent spawns a sibling to route around its caveat",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-12",
        "Privilege escalation",
        "Confused deputy",
        "partially mitigated",
        "tests/security/test_redteam_suite.py (TM-05, residual risk stated)",
    ),
    (
        "A-13",
        "Privilege escalation",
        "Depth-limit bypass via token re-minting",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-14",
        "Privilege escalation",
        "Elevation replay after TTL",
        "mitigated",
        "tests/unit/test_escalation.py (not in T-051's named scope)",
    ),
    (
        "A-15",
        "Privilege escalation",
        "Self-approval of an escalation",
        "mitigated",
        "tests/unit/test_escalation.py (not in T-051's named scope)",
    ),
    (
        "A-16",
        "Privilege escalation",
        "Race the policy hot-reload window to slip through",
        "mitigated",
        "tests/unit/test_pep_policy_cache.py (not in T-051's named scope)",
    ),
    (
        "A-17",
        "Budget",
        "Sibling swarm: 20 concurrent sub-agents all spending",
        "mitigated",
        "tests/integration/test_redteam_suite.py",
    ),
    (
        "A-18",
        "Budget",
        "Reserve-then-never-commit to strand budget",
        "mitigated",
        "tests/integration/test_redteam_suite.py",
    ),
    (
        "A-19",
        "Budget",
        "Under-report actual to hide spend",
        "accepted risk",
        "no dedicated red-team test (spec 04 §14 pt.4, TM-23)",
    ),
    (
        "A-20",
        "Budget",
        "Rapid top-up loop as ledger DoS",
        "mitigated",
        "no dedicated red-team test (PLAN.md §12)",
    ),
    (
        "A-21",
        "Budget",
        "Negative or NaN amounts",
        "mitigated",
        "no dedicated red-team test (model validators, T-005)",
    ),
    (
        "A-22",
        "Budget",
        "Currency-unit confusion (paisa vs taka)",
        "mitigated",
        "no dedicated red-team test (single canonical unit at the type level, T-005)",
    ),
    (
        "A-23",
        "Prompt injection",
        "Injected instruction telling agent to exfiltrate",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-24",
        "Prompt injection",
        "Injection redirects to a different task",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-25",
        "Prompt injection",
        "Injection asks agent to spawn a wider sub-agent",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-26",
        "Prompt injection",
        "Injection targeting the NL policy compiler",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-27",
        "Prompt injection",
        "Slow-drift attack: 20 small steps, cumulatively off-task",
        "accepted risk",
        "threat-model.md TM-11 (no dedicated test; trajectory scoring is future work)",
    ),
    (
        "A-28",
        "Infrastructure",
        "Policy bundle rollback",
        "mitigated",
        "tests/security/test_redteam_suite.py",
    ),
    (
        "A-29",
        "Infrastructure",
        "Revocation suppression by blocking pub/sub",
        "mitigated",
        "tests/integration/test_redteam_suite.py",
    ),
    (
        "A-30",
        "Infrastructure",
        "Audit tampering",
        "mitigated",
        "tests/integration/test_redteam_suite.py",
    ),
    (
        "A-31",
        "Infrastructure",
        "Log injection via crafted role names",
        "mitigated",
        "tests/unit/test_caveats.py (Datalog layer) + test_secret_scanning.py",
    ),
    (
        "A-32",
        "Infrastructure",
        "Timing side channel on deny reasons",
        "partially mitigated",
        "no dedicated red-team test (PLAN.md §12, measured leak accepted)",
    ),
    (
        "A-33",
        "Infrastructure",
        "Secret exposure in error responses",
        "mitigated",
        "tests/security/test_secret_scanning.py",
    ),
]

#: TM-19/TM-20 -- the two `threat-model.md` §6 coverage gaps T-051 closed, additional to the
#: 33 A-ids above per `ROADMAP.md` line 288.
_TM_CLOSED: Final[list[tuple[str, str, str]]] = [
    (
        "TM-19",
        "A caveat that appears to enforce and does not (existential checks against grant facts)",
        "tests/security/test_redteam_suite.py::TestTM19ExistentialChecks...",
    ),
    (
        "TM-20",
        "Incomplete request context must never be constructible",
        "tests/security/test_redteam_suite.py::TestTM20IncompleteRequestContext...",
    ),
]

SECTION_IDS: Final[list[str]] = [
    "architecture",
    "specs",
    "invariants",
    "benchmarks",
    "chaos",
    "security-scan",
    "red-team",
    "drift-model-card",
    "coverage-mutation",
    "audit-transcript",
    "threat-model",
    "ip-statement",
    "oss-traction",
]


def _escape(text: str) -> str:
    """Escape text for placement inside an HTML element. The only sanctioned entry point."""
    return html.escape(text, quote=True)


def _pre(text: str) -> str:
    return f"<pre>{_escape(text)}</pre>"


def _table(headers: list[str], rows: list[list[str]], *, raw_column: int | None = None) -> str:
    """Render an HTML table.

    Every cell is escaped except `raw_column`, which must already be safe markup
    (produced by a helper like :func:`_verdict_badge`, never raw input).
    """
    head = "".join(f"<th>{_escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{cell if i == raw_column else _escape(cell)}</td>" for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _verdict_badge(verdict: str) -> str:
    slug = verdict.replace(" ", "-")
    return f'<span class="verdict verdict-{slug}">{_escape(verdict)}</span>'


def _section(anchor: str, title: str, body: str) -> str:
    return f'<section id="{anchor}"><h2>{_escape(title)}</h2>{body}</section>'


# --------------------------------------------------------------------------- section builders


def _render_architecture() -> str:
    body = (
        "<p>The one-sentence definition (<code>PLAN.md</code> §1.1): AgentIAM gives every "
        "AI agent and sub-agent its own cryptographic identity carrying a strictly narrower "
        "set of permissions and a bounded spend budget than its parent, enforced at the tool "
        "boundary in under a millisecond, with a complete chain of custody for every action "
        "taken.</p>"
        f"{_pre(_ARCHITECTURE_DIAGRAM)}"
        "<p>Full component, sequence and deployment detail lives in "
        "<code>docs/PLAN.md</code> §3; the protocol specs below are the precise contract.</p>"
    )
    return _section("architecture", "1. Architecture", body)


def _render_specs() -> str:
    rows = [[f"specs/{n}-{slug}.md", desc] for n, slug, desc in _SPECS]
    body = (
        "<p>Every spec was checked against a running <code>biscuit-python</code> or "
        "<code>cedarpy</code> before being written down -- each document's own §9/§13 "
        "section lists what was verified. All 10 specs `PLAN.md` names now exist.</p>"
        + _table(["Spec", "Covers"], rows)
    )
    return _section("specs", "2. Specifications", body)


def _render_invariants() -> str:
    rows = [
        [inv_id, name, mechanism, test_id or "(no numbered property test -- see T-007)"]
        for inv_id, name, mechanism, test_id in INVARIANTS
    ]
    body = (
        "<p>The formal core of the paper: attenuation can only narrow authority. Source: "
        "<code>docs/specs/03-attenuation.md</code> §4 (mechanism) and §6 (property test "
        "mapping). The security boundary is biscuit's append-only block structure, not "
        "<code>narrows()</code> -- a <code>narrows()</code> bug produces a misleading "
        "token, not an over-privileged one.</p>"
        + _table(["Invariant", "Name", "Mechanism", "Property test"], rows)
    )
    return _section("invariants", "3. Formal invariants (INV-1..INV-10)", body)


def _render_benchmarks() -> str:
    rows = [[pb_id, measurement, status] for pb_id, measurement, status in BENCHMARKS]
    perf_md = generate_benchmark_results.render()
    body = (
        "<p><code>PLAN.md</code> §13.1 names twelve benchmarks. Two are the headline "
        "numbers (NFR-1, NFR-2); the rest are reported honestly below rather than omitted "
        "-- a table with two rows and no mention of the other ten reads as ten passes to "
        "anyone skimming it.</p>"
        + _table(["ID", "Measurement", "Status"], rows)
        + "<h3>Full performance report</h3>"
        + "<p>Regenerated by <code>scripts/generate_benchmark_results.py</code> from "
        "<code>docs/benchmarks/pb2-breakdown.json</code> and "
        "<code>docs/benchmarks/nfr2-load.json</code>; embedded verbatim below.</p>" + _pre(perf_md)
    )
    return _section("benchmarks", "4. Benchmarks", body)


def _render_chaos() -> str:
    results = generate_chaos_results.load_results()
    chaos_md = generate_chaos_results.render(results)
    body = (
        "<p><code>PLAN.md</code> §13.2 names twelve chaos scenarios. T-052 is scoped to "
        "five (CH-1, CH-3, CH-4, CH-8, CH-10); the other seven are listed as deferred "
        "rather than omitted. The invariant checker (T-016) runs as a sidecar throughout "
        "every scenario. Regenerated by "
        "<code>scripts/generate_chaos_results.py</code> from the JSON each run writes; "
        "embedded verbatim below.</p>" + _pre(chaos_md)
    )
    return _section("chaos", "5. Chaos results (CH-1..CH-12)", body)


def _render_security_scan() -> str:
    scan_md = _SECURITY_SCAN.read_text(encoding="utf-8") if _SECURITY_SCAN.exists() else ""
    sbom_line = "SBOM not found -- run `make sbom` to generate it."
    if _SBOM.exists():
        sbom = json.loads(_SBOM.read_text(encoding="utf-8"))
        sbom_line = (
            f"CycloneDX {sbom.get('specVersion', '?')}, "
            f"{len(sbom.get('components', []))} components (see "
            "<code>docs/evidence/sbom.json</code>)."
        )
    body = (
        "<p>T-054: <code>bandit</code>, <code>pip-audit</code>, <code>trivy fs</code> and "
        "<code>gitleaks</code>, all clean or waived with a documented rationale, plus the "
        "log secret-scanning test that found and closed a real PII leak in shipped code "
        "(the NL compiler was logging an operator's policy statement verbatim). SBOM: "
        f"{sbom_line}</p>"
        "<p>Embedded verbatim from <code>docs/evidence/security-scan.md</code> below.</p>"
        + _pre(scan_md)
    )
    return _section("security-scan", "6. Security scanning and SBOM", body)


def _render_red_team() -> str:
    rows = [
        [attack_id, category, description, _verdict_badge(verdict), tested_by]
        for attack_id, category, description, verdict, tested_by in RED_TEAM
    ]
    mitigated = sum(1 for r in RED_TEAM if r[3] == "mitigated")
    accepted = sum(1 for r in RED_TEAM if r[3] == "accepted risk")
    partial = sum(1 for r in RED_TEAM if r[3] == "partially mitigated")
    tm_rows = [[tm_id, desc, tested_by] for tm_id, desc, tested_by in _TM_CLOSED]
    body = (
        f"<p><code>PLAN.md</code> §12 catalogues 33 attacks. <strong>{mitigated} mitigated, "
        f"{partial} partially mitigated, {accepted} accepted risk(s)</strong> -- source: "
        "PLAN.md §12's own per-attack outcome line, cross-checked against the test files "
        "that exercise each one (ADR-048). A table claiming 33/33 mitigated would be "
        "marketing, not a threat model; stating the accepted risks and their bound is "
        "what makes the other rows believable.</p>"
        + _table(["ID", "Category", "Attack", "Verdict", "Tested by"], rows, raw_column=3)
        + "<h3>Two coverage gaps closed by T-051, additional to the 33</h3>"
        + "<p>Found by measurement rather than the original brainstormed catalogue "
        "(<code>threat-model.md</code> §6).</p>" + _table(["ID", "Gap", "Tested by"], tm_rows)
    )
    return _section("red-team", "7. Red-team results (A-01..A-33)", body)


def _render_drift_model_card() -> str:
    body = (
        "<p><strong>Not built for this submission, stated plainly rather than "
        "implied.</strong> <code>PLAN.md</code> §14.1 item 7 asks for a drift model card -- "
        "dataset construction, a calibration curve, false-positive rate, failure analysis, "
        "and inter-annotator agreement (&kappa;). All of that presumes a trained classifier, "
        "and T-035 (the calibrated classifier) is deferred, coupled to T-034 (the &ge;2,000 "
        "labelled-pair dataset), which is itself deferred as weeks of irreducible human "
        "labelling (<code>ROADMAP.md</code> Part 1, <code>PLAN.md</code> §21).</p>"
        "<p>What ships instead is a <strong>rule-based drift v0</strong> "
        "(<code>docs/specs/06-drift-detection.md</code>): cosine similarity between "
        "embedded task and action intent, thresholded, escalating rather than denying "
        "above 0.7. It gives the same demo experience (Beat 6: an injected instruction "
        "raises an escalation to a human) without a model that would need the deferred "
        "dataset to calibrate honestly.</p>"
        "<p>What <em>is</em> measured, from T-033 (ADR-036, ADR-037, spec 06 §5): f1/f2/f5 "
        "feature behaviour on a small hand-built set of cases, including the finding that "
        "no embedding feature can catch a large amount-inflation attack (a 211x payment "
        "increase moved the semantic similarity by 0.0102, inside the noise between aligned "
        "cases) -- which is why f5, plain symbolic argument-overlap, exists at all. This is "
        "reported as a design finding, not as a calibrated model's evaluation, because it "
        "is not one.</p>"
    )
    return _section("drift-model-card", "8. Drift model card", body)


def _render_coverage_and_mutation() -> str:
    body = (
        "<h3>Coverage</h3>"
        "<p><code>agentiam-core</code> has held <strong>100% statement coverage</strong> "
        "since T-005 -- the rule `ENGINEERING-RULES.md` keeps deliberately. Across the "
        "whole tree, coverage is reported at roughly 98% (<code>agentiam-sdk</code> 89%, "
        "<code>agentiam-pep</code> 95-100% by module, <code>agentiam-controlplane</code> "
        "86%), per <code>docs/STATUS.md</code> §1.</p>"
        "<p><strong>That figure is reported, not gated.</strong> "
        "<code>pyproject.toml</code>'s <code>[tool.coverage.report]</code> carries no "
        "<code>fail_under</code>, and CI's <code>quality</code> job uploads "
        "<code>coverage.xml</code> as an artifact without asserting on it -- so there is no "
        "committed coverage number this script can fold without either running the whole "
        "suite itself (which this generator deliberately does not do; it renders from "
        "already-committed sources) or repeating a figure that has already drifted once "
        "before without CI noticing (<code>STATUS.md</code> gap 14, "
        "<code>agentiam-core</code> measured dropping to 96% during T-033 and was caught "
        "by hand). Reproduce with:</p>"
        "<pre>make cov</pre>"
        "<h3>Mutation testing</h3>"
        "<p><strong>No <code>mutmut</code> run has been committed for this submission.</strong> "
        "<code>PLAN.md</code> §10.2 asks for a run against <code>attenuation.py</code> and "
        "<code>caveats.py</code> with a surviving-mutant rate under 10%; "
        '<code>ROADMAP.md</code> Part 1 explicitly reduces this to "run once, keep the '
        'output as evidence" rather than an iterated campaign. That single run has not '
        "happened yet (<code>docs/STATUS.md</code> §3 gap 4), and <code>mutmut</code> is "
        "not currently a project dependency. Listed here rather than silently omitted, "
        "because a table that only shows what passed and never mentions what was not run "
        "reads as more complete than it is.</p>"
    )
    return _section("coverage-mutation", "9. Coverage and mutation testing", body)


def _render_audit_transcript() -> str:
    body = (
        '<p>Chain-of-custody -- <em>"the credit limit and chain of custody for AI '
        'agents"</em> -- rests on the hash chain in <code>docs/specs/08-audit-chain.md</code>. '
        "Tamper, deletion, reordering and head-truncation detection are each proven against "
        "a real Postgres instance in <code>tests/integration/test_audit_chain.py</code>, "
        "including the specific NFR-6 requirement that verification names the "
        "<strong>first</strong> inconsistent sequence number rather than only reporting "
        '"invalid".</p>'
        "<p>This generator renders from already-committed files and assumes no live "
        "database, so it does not embed a fresh transcript here -- doing so would mean "
        "either running one of the destructive tamper tests against a real deployment's "
        "audit trail (not something a documentation build should ever do) or fabricating "
        "output that was not actually produced by a live chain (which this project's own "
        'standing rule -- "never weaken a test to make it pass", and by the same logic '
        "never manufacture evidence to make a report look complete -- rules out).</p>"
        "<p>Reproduce a live transcript, including a deliberate tamper and its detection, "
        "against a real chain:</p>"
        "<pre>make up\n"
        "uv run python scripts/verify_audit_chain.py --json\n"
        "# then tamper one record's body and re-run to see the first bad seq reported</pre>"
    )
    return _section("audit-transcript", "10. Audit-chain verification transcript", body)


def _render_threat_model() -> str:
    text = _THREAT_MODEL.read_text(encoding="utf-8") if _THREAT_MODEL.exists() else ""
    body = (
        "<p>27 STRIDE threats, each with a mitigation, a status, and the test id that "
        "covers it -- 20 mitigated, 4 partially mitigated, 3 accepted risks. Six came out "
        "of implementation rather than brainstorming. Embedded verbatim from "
        "<code>docs/threat-model.md</code> below.</p>" + _pre(text)
    )
    return _section("threat-model", "11. Threat model", body)


def _render_ip_statement() -> str:
    body = (
        "<p><code>PLAN.md</code> §14.4, the BIIN compliance statement:</p>"
        "<ul>"
        "<li><strong>&ge;51% of research, design and engineering performed in "
        "Bangladesh &rarr; 100%.</strong> Documented with commit history.</li>"
        "<li><strong>IP ownership:</strong> all core code original and Apache-2.0, "
        "sole-authored. Dependencies are permissively licensed OSS. No proprietary "
        "black-box API anywhere in the enforcement core. All model weights used for the "
        "NL compiler and drift detection are open-weight; a self-hosted local backend "
        "(<code>AGENTIAM_LLM_BACKEND=ollama</code>) remains a config flip away, with "
        "hosted inference as the current prototype default for latency reasons "
        "(ADR-040, which states that trade openly rather than glossing it).</li>"
        "</ul>"
        "<p>This is the answer to the BIIN report's warning about teams that "
        '"merely own the interface."</p>'
    )
    return _section("ip-statement", "12. IP and compliance statement", body)


def _render_oss_traction() -> str:
    body = (
        "<p><strong>Not applicable -- pre-release.</strong> "
        "<code>PLAN.md</code> §21 and <code>ROADMAP.md</code> Part 1 both defer full OSS "
        "release (public traction, a verified quickstart, an example integration) to "
        "post-award, on the reasoning that community traction is a post-award activity "
        "and BIIN scores the demo and the technical report, not a publication. T-061's "
        "reduced scope for this submission is a public GitHub repository, an Apache-2.0 "
        "LICENSE, a README with an architecture overview and usage, and a recorded demo "
        "video -- no stars, forks or external users to report yet, and none claimed.</p>"
    )
    return _section("oss-traction", "13. OSS traction", body)


# --------------------------------------------------------------------------- top level


_CSS: Final = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; max-width: 68rem;
       margin: 0 auto; padding: 2rem 1.5rem 6rem; line-height: 1.5; }
header { border-bottom: 3px solid #444; padding-bottom: 1rem; margin-bottom: 2rem; }
h1 { margin-bottom: 0.25rem; }
h2 { border-top: 1px solid #888; padding-top: 1.5rem; margin-top: 2.5rem; }
nav ul { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; }
nav a { text-decoration: none; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.92rem; }
th, td { border: 1px solid #999; padding: 0.4rem 0.6rem; text-align: left;
         vertical-align: top; }
th { background: rgba(128,128,128,0.15); }
pre { white-space: pre-wrap; word-break: break-word; background: rgba(128,128,128,0.08);
      padding: 1rem; border-radius: 4px; overflow-x: auto; font-size: 0.85rem; }
code { background: rgba(128,128,128,0.15); padding: 0.05rem 0.3rem; border-radius: 3px; }
.verdict { font-weight: 600; }
.verdict-accepted-risk { color: #b45309; }
.verdict-partially-mitigated { color: #b91c1c; }
.verdict-mitigated { color: #15803d; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #888; font-size: 0.85rem;
         opacity: 0.8; }
"""


def render() -> str:
    """The whole self-contained HTML document. Pure: reads committed files, writes nothing."""
    sections = [
        _render_architecture(),
        _render_specs(),
        _render_invariants(),
        _render_benchmarks(),
        _render_chaos(),
        _render_security_scan(),
        _render_red_team(),
        _render_drift_model_card(),
        _render_coverage_and_mutation(),
        _render_audit_transcript(),
        _render_threat_model(),
        _render_ip_statement(),
        _render_oss_traction(),
    ]
    nav_items = "".join(
        f'<li><a href="#{anchor}">{n}</a></li>' for n, anchor in enumerate(SECTION_IDS, start=1)
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>AgentIAM Evidence Pack</title>"
        f"<style>{_CSS}</style></head><body>"
        "<header><h1>AgentIAM &mdash; Evidence Pack</h1>"
        "<p>Generated by <code>scripts/generate_evidence_pack.py</code> "
        f"(T-055) &mdash; source data as committed on {date.today().isoformat()}. "
        "Every claim below is backed by a document or test already in this repository; "
        "see each section for its source. No network request is made or embedded anywhere "
        "on this page.</p>"
        f"<nav><ul>{nav_items}</ul></nav></header>"
        f"{''.join(sections)}"
        "<footer><p>AgentIAM &mdash; the credit limit and chain of custody for AI agents. "
        "BIIN submission evidence pack, generated from committed sources only.</p></footer>"
        "</body></html>\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Render the evidence pack. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="generate_evidence_pack",
        description="Regenerate docs/evidence/evidence-pack.html from committed sources.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if the committed file differs from what would be written.",
    )
    args = parser.parse_args(argv)

    rendered = render()

    if args.check:
        if not _OUTPUT.exists():
            print(f"{_OUTPUT} does not exist; run without --check", file=sys.stderr)
            return 1
        # `newline=""` disables universal-newline translation, so this compares the file's
        # actual bytes. With the default -- `Path.read_text()`, which has no `newline`
        # parameter until Python 3.13 -- a CRLF copy of the file reads back as LF and
        # would compare *equal* to a freshly rendered one, so `--check` would pass on a
        # file that is not what this script writes. See the matching `newline="\n"` below.
        with _OUTPUT.open(encoding="utf-8", newline="") as f:
            committed = f.read()
        if committed != rendered:
            print(
                f"{_OUTPUT} is stale or hand-edited. Regenerate with:\n"
                f"  uv run python scripts/generate_evidence_pack.py",
                file=sys.stderr,
            )
            return 1
        print(f"{_OUTPUT} matches its sources")
        return 0

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # `newline="\n"` rather than the platform default: this file is a committed submission
    # artifact compared byte-for-byte by `--check`, so it must be identical whether it was
    # generated on Linux or Windows. Without it, `write_text` emits CRLF on Windows and the
    # committed file's bytes depend on whose machine last ran `make evidence`.
    _OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
