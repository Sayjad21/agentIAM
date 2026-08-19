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
    "routes.json",
)

DEFAULT_OUT: Final = _REPO_ROOT / "deploy" / "demo-secrets"


def _is_complete(out: Path) -> bool:
    return all((out / name).exists() for name in _FILES)


def generate(out: Path) -> None:
    """Write every credential file into `out`. Pure filesystem side effect, no network."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from agentiam_core.bundles import PolicyBundle, public_key_to_hex, sign_bundle
    from agentiam_core.corpus import CORPUS_SOURCE
    from agentiam_core.tokens import generate_keypair
    from scripts.serve_pep import ROUTES

    out.mkdir(parents=True, exist_ok=True)

    root = generate_keypair()
    (out / "root_private_key.hex").write_text(root.private_key.to_bytes().hex(), encoding="utf-8")
    (out / "root_public_key.hex").write_text(root.public_key.to_bytes().hex(), encoding="utf-8")

    signing_key = Ed25519PrivateKey.generate()
    bundle = PolicyBundle(version="demo-1", cedar_source=CORPUS_SOURCE, serial=1)
    signature = sign_bundle(bundle, signing_key)

    (out / "policy_bundle.json").write_text(
        json.dumps(
            {
                "version": bundle.version,
                "cedar_source": bundle.cedar_source,
                "serial": bundle.serial,
            }
        ),
        encoding="utf-8",
    )
    (out / "policy_bundle.sig").write_bytes(signature)
    (out / "policy_public_key.hex").write_text(
        public_key_to_hex(signing_key.public_key()), encoding="utf-8"
    )

    (out / "routes.json").write_text(json.dumps(ROUTES), encoding="utf-8")


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
        print(f"{out}: already bootstrapped (use --force to rotate)")
        return 0

    generate(out)
    print(f"wrote {len(_FILES)} files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
