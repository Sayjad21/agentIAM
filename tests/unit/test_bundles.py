"""Signed policy bundles — spec 05 §5.1, T-025.

Rule 1 says never write your own crypto, and this module writes none. What it decides is
*what gets signed*, which is the part a library cannot decide — and getting that wrong is how
a signature ends up covering something other than what is enforced.

`TestVerifyRaises` is the class that matters. `cryptography` raises rather than returning a
boolean, and `verify_bundle` keeps that: a boolean API invites `if verify(...)` where `if not
verify(...)` was meant, and the failure of that typo is accepting every bundle.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentiam_core.bundles import (
    BundleSignatureError,
    PolicyBundle,
    public_key_from_hex,
    public_key_to_hex,
    sign_bundle,
    signing_payload,
    verify_bundle,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
SOURCE = 'permit(principal, action == Action::"invoice:read", resource);'


def a_bundle(**over: object) -> PolicyBundle:
    base: dict[str, object] = {
        "version": "2026-08-15.1",
        "cedar_source": SOURCE,
        "serial": 1,
        "entity_schema": None,
        "created_at": NOW,
    }
    return PolicyBundle(**(base | over))  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


class TestRoundTrip:
    def test_a_signed_bundle_verifies(self, key: Ed25519PrivateKey) -> None:
        bundle = a_bundle()
        verify_bundle(bundle, sign_bundle(bundle, key), key.public_key())

    def test_the_signature_is_64_bytes(self, key: Ed25519PrivateKey) -> None:
        assert len(sign_bundle(a_bundle(), key)) == 64

    def test_signing_is_deterministic(self, key: Ed25519PrivateKey) -> None:
        """Ed25519 has no nonce to vary, so one bundle and one key give one signature."""
        assert sign_bundle(a_bundle(), key) == sign_bundle(a_bundle(), key)


class TestWhatIsSigned:
    def test_the_payload_is_canonical(self, key: Ed25519PrivateKey) -> None:
        """Two constructions of the same bundle must sign identically.

        Otherwise re-serializing a bundle in transit invalidates a perfectly good signature,
        and the first person to hit it concludes the signing is broken.
        """
        assert signing_payload(a_bundle()) == signing_payload(a_bundle())

    def test_the_signature_does_not_cover_itself(self, key: Ed25519PrivateKey) -> None:
        """A signature over a structure containing the signature cannot be computed."""
        assert b"signature" not in signing_payload(a_bundle())

    @pytest.mark.parametrize(
        "field",
        ["version", "cedar_source", "serial", "entity_schema", "created_at"],
    )
    def test_every_field_is_covered(self, field: str, key: Ed25519PrivateKey) -> None:
        """A field outside the signature is a field an attacker can edit freely.

        `serial` in particular: leaving it out would let a rollback be presented under a
        valid signature, which is the whole attack §5.2 defends against.
        """
        altered = {
            "version": "tampered",
            "cedar_source": "permit(principal, action, resource);",
            "serial": 99,
            "entity_schema": '{"x": 1}',
            "created_at": datetime(2020, 1, 1, tzinfo=UTC),
        }[field]
        original = a_bundle()
        signature = sign_bundle(original, key)
        with pytest.raises(BundleSignatureError):
            verify_bundle(a_bundle(**{field: altered}), signature, key.public_key())


class TestVerifyRaises:
    """Spec 05 §5.1 — four tamper shapes, all measured, all raising."""

    def test_a_flipped_signature_bit(self, key: Ed25519PrivateKey) -> None:
        bundle = a_bundle()
        signature = sign_bundle(bundle, key)
        broken = bytes([signature[0] ^ 1]) + signature[1:]
        with pytest.raises(BundleSignatureError):
            verify_bundle(bundle, broken, key.public_key())

    def test_an_empty_signature(self, key: Ed25519PrivateKey) -> None:
        with pytest.raises(BundleSignatureError):
            verify_bundle(a_bundle(), b"", key.public_key())

    def test_a_signature_from_another_key(self, key: Ed25519PrivateKey) -> None:
        other = Ed25519PrivateKey.generate()
        with pytest.raises(BundleSignatureError):
            verify_bundle(a_bundle(), sign_bundle(a_bundle(), other), key.public_key())

    def test_it_returns_none_rather_than_true(self, key: Ed25519PrivateKey) -> None:
        """So `if verify_bundle(...)` cannot be written as a working-looking mistake."""
        bundle = a_bundle()
        # mypy knows this returns None; the assertion is for a human reading the test.
        verify_bundle(bundle, sign_bundle(bundle, key), key.public_key())

    def test_the_error_names_the_bundle(self, key: Ed25519PrivateKey) -> None:
        """An operator holding a rejected bundle needs to know which one."""
        other = Ed25519PrivateKey.generate()
        with pytest.raises(BundleSignatureError, match=r"2026-08-15\.1"):
            verify_bundle(a_bundle(), sign_bundle(a_bundle(), other), key.public_key())


class TestKeyEncoding:
    def test_a_key_round_trips_through_hex(self, key: Ed25519PrivateKey) -> None:
        """64 hex characters is what an operator pastes into configuration."""
        text = public_key_to_hex(key.public_key())
        assert len(text) == 64
        bundle = a_bundle()
        verify_bundle(bundle, sign_bundle(bundle, key), public_key_from_hex(text))

    def test_surrounding_whitespace_is_tolerated(self, key: Ed25519PrivateKey) -> None:
        """Config files and copy-paste both add it."""
        text = public_key_to_hex(key.public_key())
        assert public_key_from_hex(f"  {text}\n") is not None

    def test_a_non_hex_key_is_rejected_as_a_key_problem(self) -> None:
        """Not as *every bundle is forged*, which is the same symptom with a different cause."""
        with pytest.raises(BundleSignatureError, match="not hex"):
            public_key_from_hex("obviously not a key")

    @pytest.mark.parametrize("length", [16, 31, 33, 64])
    def test_a_wrong_length_key_is_rejected(self, length: int) -> None:
        with pytest.raises(BundleSignatureError, match="32"):
            public_key_from_hex("ab" * length)
