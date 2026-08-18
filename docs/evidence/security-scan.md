# Security scanning — T-054

The submission's evidence pack (`PLAN.md` §14) has to carry a security-scan result and
an SBOM. This page is where those live, alongside the JSON SBOM at
[`sbom.json`](./sbom.json).

**What runs, where.**

| Tool | Scope | Runs in |
|---|---|---|
| `bandit` | Python source under `packages/`, `scripts/` | `.github/workflows/ci.yml` `security-scan`, `make security` |
| `pip-audit` | The resolved lockfile via `uv export` | `.github/workflows/ci.yml` `security-scan`, `make security` |
| `trivy fs` | Whole tree — vulns, misconfigs, secrets | `.github/workflows/ci.yml` `security-scan` |
| `gitleaks` | Working tree + full history | `.pre-commit-config.yaml`, `.github/workflows/ci.yml` `security-scan` |
| Log secret-scanner | `logger.<level>(...)` sites in `packages/` — static AST scan and directed `caplog` capture | `make check`, `make security`, `.github/workflows/ci.yml` `security-scan` (`tests/security/test_secret_scanning.py`) |
| SBOM | Resolved venv, CycloneDX 1.5 JSON | `scripts/generate_sbom.py`, committed to `docs/evidence/sbom.json`; CI asserts `--check` |

**Rule of the road.** All five must be clean, or the waiver must be documented here
with a rationale. A silent `# nosec` in code without a matching entry in this file, or
a `.trivyignore` line without a rationale, is treated the same as a failing scan.

---

## Bandit — `bandit -c pyproject.toml -r packages scripts`

**Result:** clean (0 issues).

**Waivers, applied globally in `pyproject.toml` `[tool.bandit]`:**

| Test id | Rationale |
|---|---|
| `B101` (`assert_used`) | Every `assert` in `packages/` is a load-bearing invariant carrying `# noqa: S101` for ruff. Deployment never uses `python -O`, so the "assertions get stripped" concern does not apply. |
| `B105` (`hardcoded_password_string`) | False positive on `ReasonCode` enum members from `PLAN.md` §6.9 whose values start with `TOKEN_`. Ruff carries the twin `# noqa: S105` waiver on the same file. |
| `B106` (`hardcoded_password_funcarg`) | `scripts/run_load_test.py` names `password="agentiam"` on the local `PostgresContainer` for one throwaway run. Not a production credential. Ruff carries the twin `# noqa: S106`. |

**Inline `# nosec Bxxx` waivers** (grep `# nosec` for the current list):

| Location | Test id | Rationale |
|---|---|---|
| `scripts/run_load_test.py:30` (import) | `B404` | The script's job is to spawn its own PEP and tool subprocesses; `subprocess` is the correct primitive. |
| `scripts/run_load_test.py:118` | `B310` | `urllib.request.urlopen` on a fixed `http://127.0.0.1:PORT/healthz` probe; scheme is not caller-controlled. |
| `scripts/run_load_test.py:159, 168, 184, 247` | `B603` | Every `subprocess.Popen` call passes a fixed `argv` list, never a shell string, and every argument is either `sys.executable`, a repository path, or an integer port. |
| `scripts/generate_sbom.py:22, 40` | `B404`, `B603` | Same shape: fixed-argv wrappers for `uv export` and `cyclonedx-py`. |

Every entry is a place a future edit could hide an unsafe subprocess call; the shape
of the waiver (fixed argv, no shell) is the invariant that would need to be checked
if any of these grows.

---

## pip-audit — `pip-audit --disable-pip -r <uv-export> --strict`

**Result:** clean at T-054's initial run (0 known vulnerabilities across the resolved
lock's ~130 distribution requirements).

**Waivers:** none. `--strict` is enabled so a *skipped* advisory (e.g. an OS-package
advisory that lands in the DB without a fix version) fails CI. If a future
non-actionable advisory appears, add a `--ignore-vuln GHSA-...` flag here with a dated
rationale.

---

## trivy — `aquasecurity/trivy-action@0.24.0`, `scan-type: fs`

**Result:** initial run planned for the first CI execution after this ticket lands.
Configuration:

- `severity: HIGH,CRITICAL` — bugbot-style noise (LOW/MEDIUM) is out of scope for
  `PLAN.md`'s "clean or waived" bar.
- `scanners: vuln,misconfig,secret` — one step covers Python deps, `docker-compose.yml`
  / GitHub Actions syntax, and committed secret patterns.
- Waivers land in [`.trivyignore`](../../.trivyignore) — currently empty. Add a CVE
  per line with a `# Reason:` comment; do not delete this file when it is empty, since
  its absence would silently disable the ignore file mechanism.

---

## gitleaks — `.gitleaks.toml`

**Result:** clean against the working tree; historical scan runs on every CI push
(`fetch-depth: 0`).

**Waivers** documented in [`.gitleaks.toml`](../../.gitleaks.toml):

| Path / stopword | Rationale |
|---|---|
| `.env.example` | Tracked by design (`.gitignore` has `!.env.example`). Only carries placeholders; `test_env_example_carries_only_placeholders` in `tests/security/test_secret_scanning.py` is the deterministic guard. |
| `deploy/keycloak/realm-export.json` | Ships two demo user ids and a dev-only client secret named in the file's own header. |
| `TOKEN_*` reason codes | Closed enum values from `PLAN.md` §6.9; lexically token-shaped but semantically labels. |
| `dev-change-me-session-secret`, `dev-console-secret-change-me` | Documented placeholders in `.env.example`. |

---

## Log secret-scanning — NFR-5

**Result:** all eight tests in `tests/security/test_secret_scanning.py` pass.

**What it does:**

- **AST layer:** enumerates every `logger.<level>(...)` and `logging.<level>(...)` call
  in `packages/**/*.py` and refuses a positional argument whose *variable name* is in
  `FORBIDDEN_ARG_NAMES` (tokens, keys, session secrets, API keys, `nl_statement`,
  `prompt`, `args`, `body_bytes`, etc.). Mirrors `tests/unit/test_core_purity.py`'s
  static-AST-walk style. `ALLOWLIST` names the exceptions with a rationale each.
- **Runtime layer:** drives the log sites shape-check accepts, captures every level
  through `caplog`, and scans each record for forbidden *content* — PEM headers,
  biscuit-shaped tokens, 64-hex-char key material, e-mails, JWTs, Gemini/Groq API-key
  shapes. Catches what a `%s` on a safely-named variable could still emit (a URL with
  embedded credentials from a library traceback, for example).
- **`.env.example` guard:** every non-comment, non-empty `KEY=value` line is either a
  documented placeholder or a public endpoint. Adding a real value fails this test
  before commit, before push, and before CI.

**The one real finding this ticket fixed:** `agentiam_controlplane.nl_compiler.
compiler.compile_nl_to_policy` was logging the user's full natural-language statement
verbatim at INFO. Replaced with a `sha256[:16]` digest and the raw length — the same
shape as `arg_digest` for the same reason (spec 10 §5.4, `PLAN.md` NFR-5).
`test_compile_nl_to_policy_does_not_log_the_statement_verbatim` (`tests/unit/
test_nl_compiler.py`) pins that fix, and the static AST scanner rejects any future edit
that reintroduces the pattern.

---

## SBOM — `docs/evidence/sbom.json`

CycloneDX 1.5 JSON, produced by `cyclonedx-py environment --output-reproducible` over
the venv resolved from `uv.lock`. Regenerated by
[`scripts/generate_sbom.py`](../../scripts/generate_sbom.py); CI runs the same script
without `--write` and fails if the committed file is stale.

At T-054 the SBOM lists ~136 components — the transitive closure of the pinned
`pyproject.toml`, including the dev dependencies (bandit, pip-audit, cyclonedx-bom,
pytest and friends) that are only ever installed in CI and local development, since
the SBOM's job is to describe *the environment tests run in*, not a production image.

Reproduce locally:

```bash
make sbom              # writes docs/evidence/sbom.json
make security          # runs bandit + pip-audit + `python scripts/generate_sbom.py`
```

---

## Reproducing the whole thing

```bash
make security            # everything uv-installable
make check               # runs the secret-scanning tests via make test
# and in CI, additionally:
# - trivy fs (via aquasecurity/trivy-action)
# - gitleaks (via gitleaks/gitleaks-action)
```
