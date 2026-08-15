"""Signed policy bundles — spec 05 §5.1-§5.2, T-025.

Here rather than in the PEP or the control plane because **both** need it and neither should
depend on the other: the control plane signs, the PEP verifies, and one definition of what is
signed keeps them from disagreeing. It is pure computation — no clock, no I/O — so it does not
breach this package's contract.

Two things were measured before this was written (spec 05 §5.1):

* `Ed25519PublicKey.verify` **raises `InvalidSignature`**; it does not return `False`. So
  `verify_bundle` raises too. A boolean would invite `if verify(...)` where `if not
  verify(...)` was meant, and the failure of that typo is *accepting every bundle*.
* Ed25519 is deterministic, so one bundle and one key always give one signature. That is why
  signing over `canonical_json` rather than raw bytes matters: two encodings of the same
  bundle must not produce two different signatures.

Rule 1 says never write your own crypto. This file writes none — it decides *what* gets
signed, which is the part a library cannot decide for you.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from agentiam_core.errors import AgentIAMError, ReasonCode
from agentiam_core.hashing import canonical_json

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "BundleSignatureError",
    "PolicyBundle",
    "public_key_from_hex",
    "public_key_to_hex",
    "sign_bundle",
    "signing_payload",
    "verify_bundle",
]


class BundleSignatureError(AgentIAMError):
    """A bundle's signature does not verify against the expected key.

    Carries `POLICY_BUNDLE_STALE`'s sibling meaning rather than its code: a forged bundle is
    not a stale one, and the PEP's response is to *keep the previous bundle* (spec 05 §5.3),
    so this never becomes a per-request reason code. It is a load-time failure.
    """

    reason_code = ReasonCode.POLICY_DENIED


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    """What the policy service publishes and the PEP caches.

    `version` is a **label** an operator chooses and is what lands in
    `DecisionRecord.policy_version`. `serial` is the monotonic integer rollback protection
    compares — string labels do not order (`"bundle-10" < "bundle-9"`), and a rollback defence
    that depends on naming is not a defence (spec 05 §5.2).
    """

    version: str
    cedar_source: str
    serial: int = 0
    entity_schema: str | None = None
    created_at: datetime | None = None


def signing_payload(bundle: PolicyBundle) -> bytes:
    """The exact bytes a signature covers — everything but the signature itself.

    `canonical_json` rather than any convenient encoding, so that re-serializing a bundle in
    transit does not invalidate it. It is the same canonicalization the audit chain uses.
    """
    body: dict[str, Any] = {
        "serial": bundle.serial,
        "version": bundle.version,
        "cedar_source": bundle.cedar_source,
        "entity_schema": bundle.entity_schema,
        "created_at": bundle.created_at,
    }
    return canonical_json(body)


def sign_bundle(bundle: PolicyBundle, private_key: Ed25519PrivateKey) -> bytes:
    """Sign a bundle. Returns the 64-byte detached signature."""
    return private_key.sign(signing_payload(bundle))


def verify_bundle(bundle: PolicyBundle, signature: bytes, public_key: Ed25519PublicKey) -> None:
    """Check a bundle's signature.

    Returns None on success and raises otherwise — never a boolean, for the reason in the
    module docstring.

    Raises:
        BundleSignatureError: The signature does not verify. Covers a forged signature, an
            altered bundle, an empty signature, and a signature from a different key — all
            four measured (spec 05 §5.1).
    """
    try:
        public_key.verify(signature, signing_payload(bundle))
    except InvalidSignature as exc:
        raise BundleSignatureError(
            f"bundle serial {bundle.serial} ({bundle.version!r}) does not verify against the "
            f"configured policy key"
        ) from exc


def public_key_to_hex(public_key: Ed25519PublicKey) -> str:
    """Render a public key as 64 hex characters, which is what goes in a config file."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return public_key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def public_key_from_hex(text: str) -> Ed25519PublicKey:
    """Parse a public key an operator pasted into configuration.

    Raises:
        BundleSignatureError: The text is not 64 hex characters. Raised here rather than
            letting a malformed key surface as *every bundle is forged*, which is the same
            symptom with a very different cause.
    """
    try:
        raw = bytes.fromhex(text.strip())
    except ValueError as exc:
        raise BundleSignatureError(f"policy key is not hex: {text[:16]!r}...") from exc
    if len(raw) != 32:
        raise BundleSignatureError(f"policy key is {len(raw)} bytes; an Ed25519 public key is 32")
    return Ed25519PublicKey.from_public_bytes(raw)
