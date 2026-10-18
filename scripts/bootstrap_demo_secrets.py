"""Generate the credentials `docker-compose.demo.yml` mounts into the app containers.

`agentiam_controlplane.app.create_app_from_env()` and `scripts.pep_service.
ServiceSettings.from_env()` both refuse to start without real credentials — a root
keypair and a signed policy bundle, per ADR-056 — and nothing in this repository
publishes one (ADR-039: no bundle-publishing service exists). A compose file cannot
invent a signature. This script is the one place that generates the whole set, so
"one-command demo bring-up" has something real to mount rather than a stub.

**Bootstrap plumbing, not demo content.** The signed policy is
`agentiam_core.corpus.CORPUS_SOURCE` — the same 51-case-tested Cedar source the
activation gate and the Cedar authoring console already use — reused rather than
invented, so what gets signed is provably not garbage. The route table is
`scripts.serve_pep.ROUTES`, the same table T-053's load-test harness already exercises.
Choosing realistic BD company names, BDT amounts and an actual demo narrative is T-057's
job (`ROADMAP.md` M6); this script stops at "the containers can start and enforce
something real."

**Idempotent.** Regenerating on every `docker compose up` would mint a new root key and
re-sign the bundle on every restart, invalidating any mandate or token a previous run
already issued. A run completes the output set if it is partial (e.g. a previous run was
killed mid-way) but never rotates an existing, complete one unless `--force` is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Every file a complete bootstrap directory has. Checked as a set, so a directory
#: missing even one file (partial run, or a stray unrelated file dropped in) is
#: completed rather than trusted.
_FILES: Final = (
    "root_private_key.hex",
    "root_public_key.hex",
    "policy_bundle.json",
    "policy_bundle.sig",
    "policy_public_key.hex",
    "bundle_signing_key.hex",
    "routes.json",
    "role_assignments.json",
)

#: What the organization says each demo agent is, keyed by delegation path (root-first,
#: `/`-separated) because an `agent_id` alone is the delegating parent's word — see
#: `agentiam_core.datalog.chain_identity`.
#:
#: Both payer lineages — `agt-payer`'s and `agt-treasury`'s — are `senior` and nothing else
#: is. That is not a convenience to keep payments green: `payment_api` is `sensitivity:
#: "critical"` in the catalogue, the corpus bundle forbids critical resources to non-seniors,
#: and these six are exactly the agents the demo entrusts with `payment:initiate`.
#: `agt-doc-reader`, `agt-negotiator` and the read-only agents below them are *not* senior,
#: so the forbid is live rather than disarmed — which is the difference between this and
#: setting `AGENTIAM_PEP_DEFAULT_ROLE=senior` and calling the catalogue wired.
#:
#: `agt-subcontractor` is senior too, and its payment is still refused: it sits at depth 3 and
#: the bundle permits payments only at `principal.depth <= 2`. Leaving it unassigned would
#: refuse the same call for the *wrong* reason — the sensitivity forbid, shadowing the depth
#: rule that beat is built to show (`scripts/seed_demo.py`, `_TRAFFIC`'s last line).
#:
#: Every path here carries the same role as its ancestors, which
#: `pep_service._refuse_widening_roles` requires: an ancestor can mint any descendant path,
#: so a descendant that outranks it would be an escalation the PEP could not see.
ROLE_ASSIGNMENTS: Final[dict[str, str]] = {
    "agt-payer": "senior",
    "agt-payer/agt-settlement": "senior",
    "agt-payer/agt-settlement/agt-subcontractor": "senior",
    "agt-treasury": "senior",
    "agt-treasury/agt-reconciler": "senior",
    "agt-treasury/agt-bulk-buyer": "senior",
    # `scripts/demo_concurrency.py`: three siblings that only ever pay, one level down.
    "agt-supplier-a": "senior",
    "agt-supplier-b": "senior",
    "agt-supplier-c": "senior",
}

DEFAULT_OUT: Final = _REPO_ROOT / "deploy" / "demo-secrets"


def _is_complete(out: Path) -> bool:
    return not _missing(out)


def _missing(out: Path) -> set[str]:
    return {name for name in _FILES if not (out / name).exists()}


def generate(out: Path) -> None:
    """Write every credential file into `out`. Pure filesystem side effect, no network."""
    from agentiam_core.tokens import generate_keypair

    out.mkdir(parents=True, exist_ok=True)

    root = generate_keypair()
    (out / "root_private_key.hex").write_text(root.private_key.to_bytes().hex(), encoding="utf-8")
    (out / "root_public_key.hex").write_text(root.public_key.to_bytes().hex(), encoding="utf-8")

    _write_policy(out)
    _write_routes(out)
    _write_role_assignments(out)


def _write_routes(out: Path) -> None:
    """The route table, from `serve_pep.ROUTES`. Derived, so rewriting it rotates nothing."""
    from scripts.serve_pep import ROUTES

    (out / "routes.json").write_text(json.dumps(ROUTES), encoding="utf-8")


def _write_policy(out: Path) -> None:
    """The policy half: a bundle-signing key, the boot bundle signed with it, its public half.

    The key is **kept** (`bundle_signing_key.hex`, mode 0600) because the control plane signs
    every bundle after this one (APICTA-01, spec 05 §5.7.2). It used to be discarded once the
    boot bundle was signed, which is why no second bundle could ever be published. It is a
    different key from the root token key: one changes policy, the other mints identities.
    """
    from datetime import UTC, datetime

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    from agentiam_core.bundles import (
        PolicyBundle,
        bundle_file_payload,
        public_key_to_hex,
        sign_bundle,
    )
    from agentiam_core.corpus import CORPUS_SOURCE, CORPUS_TOOLS

    signing_key = Ed25519PrivateKey.generate()
    key_path = out / "bundle_signing_key.hex"
    key_path.write_text(
        signing_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex(),
        encoding="utf-8",
    )
    key_path.chmod(0o600)

    # `tools=` is inside the signature, so the catalogue the policy reads cannot be edited on
    # disk without the bundle failing to verify. Without it every resource fell back to
    # `_UNKNOWN_TOOL` and the bundle's two resource-attribute rules were inert in the
    # deployment while passing in CI — TODO item 29.
    bundle = PolicyBundle(
        version="demo-1",
        cedar_source=CORPUS_SOURCE,
        serial=1,
        tools=CORPUS_TOOLS,
        created_at=datetime.now(UTC).replace(microsecond=0),
    )
    # The file is the shared wire form minus the signature, read back by the one shared reader
    # (`agentiam_core.bundles.bundle_from_file`) on both the PEP and the control plane.
    (out / "policy_bundle.json").write_text(
        json.dumps(bundle_file_payload(bundle)), encoding="utf-8"
    )
    (out / "policy_bundle.sig").write_bytes(sign_bundle(bundle, signing_key))
    (out / "policy_public_key.hex").write_text(
        public_key_to_hex(signing_key.public_key()), encoding="utf-8"
    )


def _write_role_assignments(out: Path) -> None:
    """The role map is configuration, not a credential — rewriting it rotates nothing."""
    (out / "role_assignments.json").write_text(json.dumps(ROLE_ASSIGNMENTS), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without touching disk."""
    parser = argparse.ArgumentParser(
        prog="bootstrap_demo_secrets",
        description="Generate the root keypair and signed policy bundle the demo stack needs.",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Output directory (default: {DEFAULT_OUT.relative_to(_REPO_ROOT)})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if a complete set already exists. Rotates the root key and "
        "the policy signature, invalidating any token or mandate minted under the old ones.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Generate the credential set unless one already exists. Returns the exit code."""
    args = build_parser().parse_args(argv)
    out = Path(args.out)

    if _is_complete(out) and not args.force:
        # Refreshed anyway, so an agent added to the demo tree — or a route added to the
        # table, as APICTA-06's MCP rules were — reaches an existing secrets volume without
        # rotating the keys every token was minted under.
        _write_routes(out)
        _write_role_assignments(out)
        print(f"{out}: already bootstrapped (use --force to rotate); routes, role map refreshed")
        return 0

    if _missing(out) == {"bundle_signing_key.hex"} and not args.force:
        # Every volume bootstrapped before APICTA-01: complete except for the key nothing used
        # to keep. Replacing the policy half alone keeps the root key, and so every token the
        # seeder minted; regenerating wholesale would invalidate them all.
        _write_policy(out)
        _write_routes(out)
        _write_role_assignments(out)
        print(f"{out}: added the bundle-signing key and re-signed the boot bundle; root key kept")
        return 0

    generate(out)
    print(f"wrote {len(_FILES)} files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
