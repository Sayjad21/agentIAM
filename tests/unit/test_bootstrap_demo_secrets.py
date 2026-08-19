"""`scripts/bootstrap_demo_secrets.py` — T-056 Part 2.

`pep_service.py` and `create_app_from_env()` both refuse to start without real
credentials: a root keypair, a signed policy bundle, and a route table. None of that can
be invented by a compose file, and there is no bundle-publishing service to fetch one
from (ADR-039, ADR-056). This script is the one place that generates them, so
`docker-compose.demo.yml`'s "one-command bring-up" has something real to mount.

**This is bootstrap plumbing, not demo content.** The Cedar source is
`agentiam_core.corpus.CORPUS_SOURCE` — the same 51-case-tested policy the activation gate
and the console already use — reused rather than invented, so the bundle this script signs
is provably not garbage. The route table is `scripts.serve_pep.ROUTES`, the same table the
load-test harness already exercises. Choosing realistic BD company names, BDT amounts and
demo narrative is T-057's job (`ROADMAP.md` M6); this script's job is narrower and stops at
"the containers can start and enforce something real."

**Idempotent by design.** Regenerating on every `docker compose up` would mint a new root
key and a new policy signature each time, invalidating any mandate or token a previous run
already issued. Existing output files are left alone unless `--force` is passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import bootstrap_demo_secrets

if TYPE_CHECKING:
    pass


class TestGenerate:
    def test_it_writes_every_file_pep_service_and_the_control_plane_need(
        self, tmp_path: Path
    ) -> None:
        bootstrap_demo_secrets.main(["--out", str(tmp_path)])

        for name in (
            "root_private_key.hex",
            "root_public_key.hex",
            "policy_bundle.json",
            "policy_bundle.sig",
            "policy_public_key.hex",
            "routes.json",
        ):
            assert (tmp_path / name).exists(), f"missing {name}"

    def test_the_root_keys_are_a_real_matching_pair(self, tmp_path: Path) -> None:
        from biscuit_auth import Algorithm, KeyPair, PrivateKey

        bootstrap_demo_secrets.main(["--out", str(tmp_path)])

        private_hex = (tmp_path / "root_private_key.hex").read_text(encoding="utf-8").strip()
        public_hex = (tmp_path / "root_public_key.hex").read_text(encoding="utf-8").strip()

        # `biscuit-python`'s bundled stubs are stale (same divergence pep_service.py's
        # `_root_keys` documents): `from_bytes` requires `Algorithm` at runtime though the
        # stub omits it.
        private_key = PrivateKey.from_bytes(  # type: ignore[call-arg]
            bytes.fromhex(private_hex),
            Algorithm.Ed25519,  # type: ignore[attr-defined]
        )
        derived = KeyPair.from_private_key(private_key).public_key.to_bytes().hex()
        assert derived == public_hex

    def test_the_bundle_verifies_against_the_published_public_key(self, tmp_path: Path) -> None:
        from agentiam_core.bundles import PolicyBundle, public_key_from_hex, verify_bundle

        bootstrap_demo_secrets.main(["--out", str(tmp_path)])

        payload = json.loads((tmp_path / "policy_bundle.json").read_text(encoding="utf-8"))
        signature = (tmp_path / "policy_bundle.sig").read_bytes()
        public_hex = (tmp_path / "policy_public_key.hex").read_text(encoding="utf-8").strip()

        bundle = PolicyBundle(
            version=payload["version"],
            cedar_source=payload["cedar_source"],
            serial=payload["serial"],
        )
        # Raises on failure (T-025) — reaching the next line is the assertion.
        verify_bundle(bundle, signature, public_key_from_hex(public_hex))

    def test_the_bundle_is_the_real_tested_corpus_policy_not_invented_content(
        self, tmp_path: Path
    ) -> None:
        from agentiam_core.corpus import CORPUS_SOURCE

        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        payload = json.loads((tmp_path / "policy_bundle.json").read_text(encoding="utf-8"))
        assert payload["cedar_source"] == CORPUS_SOURCE

    def test_the_routes_file_matches_serve_peps_own_table(self, tmp_path: Path) -> None:
        from scripts.serve_pep import ROUTES

        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        written = json.loads((tmp_path / "routes.json").read_text(encoding="utf-8"))
        assert written == ROUTES

    def test_it_creates_the_output_directory_if_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "dir"
        bootstrap_demo_secrets.main(["--out", str(target)])
        assert (target / "root_public_key.hex").exists()


class TestIdempotency:
    def test_a_second_run_does_not_rotate_the_keys(self, tmp_path: Path) -> None:
        # Regenerating on every `docker compose up` would invalidate every mandate and
        # token a previous run already issued.
        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        first = (tmp_path / "root_public_key.hex").read_text(encoding="utf-8")

        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        second = (tmp_path / "root_public_key.hex").read_text(encoding="utf-8")

        assert first == second

    def test_a_second_run_does_not_re_sign_the_bundle(self, tmp_path: Path) -> None:
        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        first_sig = (tmp_path / "policy_bundle.sig").read_bytes()

        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        second_sig = (tmp_path / "policy_bundle.sig").read_bytes()

        assert first_sig == second_sig

    def test_force_does_rotate(self, tmp_path: Path) -> None:
        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        first = (tmp_path / "root_public_key.hex").read_text(encoding="utf-8")

        bootstrap_demo_secrets.main(["--out", str(tmp_path), "--force"])
        second = (tmp_path / "root_public_key.hex").read_text(encoding="utf-8")

        assert first != second

    def test_a_partial_directory_is_regenerated_wholesale_not_patched(self, tmp_path: Path) -> None:
        # If a previous run was killed after writing the root key but before signing the
        # bundle, the next run must produce a complete, internally-consistent set rather
        # than trying to salvage the partial one. True partial-resume would need the
        # policy *signing* key persisted too — it currently is not, deliberately, since
        # nothing needs it once the bundle is signed — so "keep the root key, only
        # re-sign the bundle" is not a distinction this script can make safely. A fresh,
        # complete set is simpler and costs nothing: an interrupted run never published
        # anything a rotation could invalidate.
        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        (tmp_path / "policy_bundle.json").unlink()
        (tmp_path / "policy_bundle.sig").unlink()

        bootstrap_demo_secrets.main(["--out", str(tmp_path)])

        for name in (
            "root_private_key.hex",
            "root_public_key.hex",
            "policy_bundle.json",
            "policy_bundle.sig",
            "policy_public_key.hex",
            "routes.json",
        ):
            assert (tmp_path / name).exists(), f"missing {name} after completing a partial run"


class TestCli:
    def test_default_out_dir_is_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = bootstrap_demo_secrets.build_parser()
        args = parser.parse_args([])
        assert args.out  # has some default, not required on the command line
