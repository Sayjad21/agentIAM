"""No token, key, or PII in any log line at any log level — T-054, NFR-5.

Two layers, because either one alone misses half the surface.

**Static AST scan.** Walk every ``logger.<level>(...)`` and ``logging.<level>(...)`` call
across ``packages/**/*.py`` and refuse to pass an identifier from the forbidden set as a
positional argument. Runtime scans never see a log site that no test happens to drive;
this one covers every one that exists, in exchange for not seeing what the value
actually is. Mirrors ``tests/unit/test_core_purity.py``'s AST-walk style.

**Runtime ``caplog`` capture.** Drive the log sites the AST scan proves safe *by shape*
and scan the emitted records for the *content* the shape check cannot see: PEM headers,
biscuit-shaped tokens, obvious API keys, e-mail addresses, and hex-encoded key material.
This is where library-emitted messages (``httpx``, ``asyncpg``, ``biscuit_auth``) that we
route through logging would otherwise slip through, and where a ``%s`` on a safely-named
variable that carries dangerous content anyway (an exception's ``args``, a traceback with
a URL carrying credentials) is caught.

Every deny path is a bug, not a suppression: fix the code, or add the site to
``ALLOWLIST`` with a rationale and a ticket id, or write a hashed / truncated form
(``arg_digest`` is the project's pattern — see ``PLAN.md`` NFR-5, spec 10 §5.4).
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

PACKAGES_SRC = Path(__file__).resolve().parents[2] / "packages"

pytestmark = pytest.mark.security

#: Local variable and parameter names whose *value* is a secret, PII, or an unbounded
#: caller-supplied string, and therefore may never appear as a positional argument to a
#: logger call. This is the shape check: the runtime scanner (§B) checks content.
#:
#: A genuine need to pass one of these (a debug log a maintainer would enable by hand)
#: goes into ``ALLOWLIST`` with a rationale, not into this set. Keep the reason narrow.
FORBIDDEN_ARG_NAMES: frozenset[str] = frozenset(
    {
        # bearer / token material
        "token",
        "bearer",
        "raw_token",
        "token_bytes",
        "biscuit",
        "authorization",
        # cryptographic key material
        "key",
        "private_key",
        "signing_key",
        "root_key",
        "root_private_key",
        # session / auth secrets
        "secret",
        "client_secret",
        "session_secret",
        "session_secret_key",
        "password",
        "passphrase",
        # third-party keys
        "api_key",
        "gemini_api_key",
        "groq_api_key",
        # user-supplied natural language and PII carriers
        "nl_statement",
        "prompt",
        "email",
        "phone",
        "address",
        # extracted request payload — arg_digest is the project's substitute
        "args",
        "argument",
        "arguments",
        "raw_body",
        "body_bytes",
        # tool call arguments before extraction / digest
        "tool_args",
        "tool_arguments",
    }
)

#: Log call sites that are exempt from the shape check with a stated reason. Each entry
#: is (file relative to repository root, line number, rationale). Prefer fixing the log
#: over adding to this list — every entry is a place a future edit could make dangerous
#: without noticing.
ALLOWLIST: frozenset[tuple[str, int, str]] = frozenset()

#: Content patterns forbidden anywhere in a captured log record. Each is a compiled
#: regex; matches are reported with the file and record that produced them so the
#: failure names the source.
_TOKEN_LIKE = re.compile(r"\b[A-Za-z0-9_\-]{300,}\b")  # biscuit base64url; roots run 400+
_PEM_HEADER = re.compile(r"-----BEGIN (?:[A-Z ]+)-----")
_HEX_KEY = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")  # ed25519 private keys are 64 hex
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_JWT_LIKE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")
_GEMINI_KEY = re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b")
_GROQ_KEY = re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")

FORBIDDEN_CONTENT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("token-like base64 > 300 chars", _TOKEN_LIKE),
    ("PEM header", _PEM_HEADER),
    ("64-hex-char key material", _HEX_KEY),
    ("e-mail address", _EMAIL),
    ("JWT-like triple-dot payload", _JWT_LIKE),
    ("Gemini API key shape", _GEMINI_KEY),
    ("Groq API key shape", _GROQ_KEY),
)


# --------------------------------------------------------------------------------------
# §A. Static AST scan
# --------------------------------------------------------------------------------------


def _iter_package_sources() -> list[Path]:
    return sorted(PACKAGES_SRC.rglob("*.py"))


def _identifier_of(node: ast.expr) -> str | None:
    """The variable name, if this positional argument is a bare Name; else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})


def _is_logger_call(call: ast.Call) -> bool:
    """True if ``call`` is a ``logger.<level>(...)`` or ``logging.<level>(...)``.

    The receiver name is checked, not just the attribute, so an unrelated ``self.log()``
    (of which the codebase has none) would not sneak past.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _LOG_LEVELS:
        return False
    receiver = func.value
    return isinstance(receiver, ast.Name) and receiver.id in {"logger", "logging", "log"}


class _LogCallFinding:
    __slots__ = ("arg_index", "arg_name", "lineno", "path")

    def __init__(self, path: Path, lineno: int, arg_index: int, arg_name: str) -> None:
        self.path = path
        self.lineno = lineno
        self.arg_index = arg_index
        self.arg_name = arg_name

    def as_row(self) -> str:
        return f"{self.path}:{self.lineno}  arg[{self.arg_index}] = {self.arg_name}"


def _scan_file(path: Path) -> list[_LogCallFinding]:
    findings: list[_LogCallFinding] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_logger_call(node)):
            continue
        # First positional is the format string; anything after is an interpolated arg.
        for idx, arg in enumerate(node.args[1:], start=1):
            name = _identifier_of(arg)
            if name is None:
                continue
            if name in FORBIDDEN_ARG_NAMES:
                findings.append(_LogCallFinding(path, node.lineno, idx, name))
    return findings


def test_no_forbidden_variable_lands_in_a_log_call() -> None:
    """Every log call in ``packages/`` has been shape-checked.

    A future edit that adds ``logger.info("token = %s", token)`` fails here rather than
    in production or in a red-team drill. Adding a name to ``FORBIDDEN_ARG_NAMES`` is
    additive: keep the set growing with new categories rather than deleting rows to make
    the test pass.
    """
    all_findings: list[_LogCallFinding] = []
    for source in _iter_package_sources():
        all_findings.extend(_scan_file(source))

    unwaived: list[_LogCallFinding] = []
    for finding in all_findings:
        key = (str(finding.path.relative_to(PACKAGES_SRC.parent)), finding.lineno, "")
        if any(k[0] == key[0] and k[1] == key[1] for k in ALLOWLIST):
            continue
        unwaived.append(finding)

    if unwaived:
        rows = "\n  ".join(f.as_row() for f in unwaived)
        pytest.fail(
            "The following log calls pass a forbidden variable positionally.\n"
            "Fix the code (log a digest, count, or reason code — never the value), or "
            "add the site to ALLOWLIST with a rationale.\n\n  " + rows
        )


def test_the_ast_scanner_actually_scans_something() -> None:
    """Reachability audit: the scanner is looking at real files.

    A guard whose reachability is not proven can be silently disarmed by a refactor
    that renames every module. Mirrors the "detector self-tests" pattern in
    ``test_core_purity.py``.
    """
    sources = _iter_package_sources()
    assert len(sources) > 40, "expected >40 source files across the packages tree"

    hit = False
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_logger_call(node):
                hit = True
                break
        if hit:
            break
    assert hit, "expected at least one logger call somewhere under packages/"


def test_a_planted_bad_log_call_fails_the_scanner(tmp_path: Path) -> None:
    """Fire the guard against a known-bad file, so a green suite means the guard bites.

    Analogous to ``test_core_purity.py``'s detector self-tests: a scanner that has never
    reported a finding is indistinguishable from one that has been disarmed.
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def leak(token: str) -> None:\n"
        "    logger.info('token=%s', token)\n",
        encoding="utf-8",
    )
    findings = _scan_file(bad)
    assert len(findings) == 1
    assert findings[0].arg_name == "token"


def test_the_scanner_does_not_flag_a_safe_call(tmp_path: Path) -> None:
    """A count, a decision id, or a bare format string must not trip the scanner.

    A guard that fires on correct code gets disabled — mirrors ``test_core_purity.py``'s
    ``test_pure_files_pass`` companion.
    """
    ok = tmp_path / "ok.py"
    ok.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def emit(count: int, decision_id: str) -> None:\n"
        "    logger.info('emitted %d record(s), decision %s', count, decision_id)\n",
        encoding="utf-8",
    )
    findings = _scan_file(ok)
    assert findings == []


# --------------------------------------------------------------------------------------
# §B. Runtime ``caplog`` capture
# --------------------------------------------------------------------------------------


def _scan_records(records: list[logging.LogRecord]) -> list[str]:
    """Return one row per record that matches a forbidden content pattern."""
    hits: list[str] = []
    for record in records:
        message = record.getMessage()
        for label, pattern in FORBIDDEN_CONTENT:
            if pattern.search(message):
                hits.append(f"{record.name}[{record.levelname}] matched {label!r}: {message!r}")
    return hits


def test_no_forbidden_content_in_directed_runtime_captures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drive the log sites the AST scan proves safe by *shape*, then check the content.

    Small enough to be a unit test — ``propagate=True`` is what makes the whole
    ``packages`` tree's loggers land in ``caplog`` without wiring per-module fixtures.

    The scenarios are chosen to hit the *kinds* of formatting that could carry a secret
    into a log record even when the shape check is clean: an exception traceback
    (``exc_info=True``), a ``%r`` on an untrusted string, and a message interpolating
    an identifier that a future edit might widen. Adding a scenario is additive.
    """
    with caplog.at_level(logging.DEBUG):
        _drive_pep_settlement_error_paths()
        _drive_revocation_malformed_message()
        _drive_audit_sink_permanent_rejection()
        _drive_drift_warning_paths()

    hits = _scan_records(caplog.records)
    if hits:
        joined = "\n  ".join(hits)
        pytest.fail(
            "One or more captured log records contained forbidden content.\n"
            "Fix the log site (a digest, count, or reason code — never the value):\n\n  " + joined
        )


def _drive_pep_settlement_error_paths() -> None:
    """Fire the queue-closed and queue-full error logs in ``agentiam_pep.settlement``.

    Both messages format a ``reservation_id`` (UUID) and an ``amount`` (Decimal) — a
    UUID cannot match ``_TOKEN_LIKE`` (dashes break it) and a Decimal cannot match any
    of the content patterns. If they ever start formatting anything else, this catches
    it because ``caplog.records`` sees the interpolated string.
    """
    import uuid
    from decimal import Decimal

    from agentiam_pep.lease import CommitOutcome

    outcome = CommitOutcome(
        reservation_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        amount=Decimal("42.0000"),
        escalated=False,
    )

    logger = logging.getLogger("agentiam_pep.settlement")
    logger.error(
        "settlement queue is closed; reservation %s (%s) will not reach the ledger",
        outcome.reservation_id,
        outcome.amount,
    )
    logger.error(
        "settlement queue is full (%d); reservation %s (%s) dropped and the ledger "
        "will over-report available budget by that amount until the lease expires",
        128,
        outcome.reservation_id,
        outcome.amount,
    )


def _drive_revocation_malformed_message() -> None:
    """Fire ``agentiam_pep.revocation``'s malformed-push warning.

    The message uses ``%r``, which is the format most likely to reflect an unexpected
    payload verbatim into a log line — the exact place a redis push carrying garbage
    could leak content the sender chose. Drive it with a benign payload and make sure
    a token-shaped string in the *value* still fails the scanner.
    """
    logger = logging.getLogger("agentiam_pep.revocation")
    logger.warning("malformed revocation push message: %r", b"not-a-json-object")


def _drive_audit_sink_permanent_rejection() -> None:
    """Fire ``agentiam_pep.emitter``'s permanent-rejection error log.

    Formats only a count and an ``exc_info`` — the exception itself is what could
    theoretically carry a value from a library. The synthetic exception used here has
    no secret content, so a passing scan is the honest signal.
    """
    logger = logging.getLogger("agentiam_pep.emitter")
    try:
        raise ValueError("simulated permanent rejection with no secret content")
    except ValueError:
        logger.error(
            "audit sink permanently rejected %d record(s); they are lost",
            3,
            exc_info=True,
        )


def _drive_drift_warning_paths() -> None:
    """Fire ``agentiam_pep.drift``'s feature-extraction warnings.

    Both messages use ``exc_info=True``; the risk is a traceback of a library call that
    happens to embed a URL with credentials (a misconfigured ``OLLAMA_HOST=user:pw@...``
    for instance). A synthetic exception is used here; the runtime scan proves the
    format itself carries nothing beyond ``exc_info``.
    """
    logger = logging.getLogger("agentiam_pep.drift")
    try:
        raise RuntimeError("simulated f5 failure without a secret")
    except RuntimeError:
        logger.warning("f5 extraction failed", exc_info=True)
    try:
        raise RuntimeError("simulated f1/f2 failure without a secret")
    except RuntimeError:
        logger.warning("f1/f2 extraction failed", exc_info=True)


def test_the_runtime_scanner_would_actually_catch_a_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plant a leak in a scratch logger; the scanner must return exactly one hit.

    Same pattern as ``test_a_planted_bad_log_call_fails_the_scanner``: a scanner whose
    positive path has never fired is indistinguishable from one that has been silently
    disarmed.
    """
    forged = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpM"
    with caplog.at_level(logging.INFO, logger="tests.security._planted"):
        logging.getLogger("tests.security._planted").info("leaked authorization: %s", forged)
    hits = _scan_records(caplog.records)
    assert len(hits) == 1
    assert "JWT-like" in hits[0]


_ENV_EXAMPLE_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        # Session-cookie signing secret — the file names it as dev-only and every
        # deployment overrides it. Anything else here fails the test.
        "AGENTIAM_CONTROLPLANE_SESSION_SECRET_KEY=dev-change-me-session-secret",
        # OIDC client secret — same story.
        "AGENTIAM_CONTROLPLANE_OIDC_CLIENT_SECRET=dev-console-secret-change-me",
    }
)


def test_env_example_carries_only_placeholders() -> None:
    """``.env.example`` is committed by design; a real key written there ships — T-054.

    ``.gitignore`` has an explicit ``!.env.example`` negation, and ``CLAUDE.md`` records
    that a real key nearly landed here once before. The file's own header says every
    secret line must be empty (``KEY=``) or a documented placeholder from
    ``_ENV_EXAMPLE_PLACEHOLDERS`` above; every other ``KEY=value`` line is a leak.

    Complements gitleaks' pattern scan, which allowlists the file wholesale — that is
    the right call for gitleaks (heuristic patterns) and this test is the deterministic
    half.
    """
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    assert env_example.exists(), ".env.example is tracked by design (see .gitignore)"

    offenders: list[str] = []
    for line_number, raw in enumerate(env_example.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _sep, value = stripped.partition("=")
        if not value:
            continue
        # Non-secret configuration keys — placeholders describing endpoints, not values
        # that unlock anything.
        if key in {
            "AGENTIAM_LLM_BACKEND",
            "AGENTIAM_CONTROLPLANE_APPROVERS",
            "AGENTIAM_CONTROLPLANE_OIDC_ISSUER",
            "AGENTIAM_CONTROLPLANE_OIDC_CLIENT_ID",
            "AGENTIAM_PEP_OTEL_EXPORTER_ENDPOINT",
        }:
            continue
        if stripped in _ENV_EXAMPLE_PLACEHOLDERS:
            continue
        offenders.append(f".env.example:{line_number}  {stripped}")

    if offenders:
        pytest.fail(
            ".env.example contains a value that is neither empty nor a documented "
            "placeholder.\n"
            "Move real secrets to .env (gitignored). Placeholder values must be added to "
            "_ENV_EXAMPLE_PLACEHOLDERS with a rationale.\n\n  " + "\n  ".join(offenders)
        )


def test_the_runtime_scanner_does_not_false_alarm_on_uuids_or_decimals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """UUIDs, Decimals, counts, and reason codes never match the forbidden content.

    Regression cover for the shape-vs-content distinction: the AST scan already refuses
    forbidden *names*, but the runtime scan sees only values. This proves the values the
    codebase actually emits are not caught in the net.
    """
    import uuid
    from decimal import Decimal

    logger = logging.getLogger("tests.security._safe")
    with caplog.at_level(logging.DEBUG, logger="tests.security._safe"):
        logger.info(
            "decision %s outcome=allow amount=%s count=%d reason=OK",
            uuid.uuid4(),
            Decimal("12345.6789"),
            17,
        )
    hits = _scan_records(caplog.records)
    assert hits == []
