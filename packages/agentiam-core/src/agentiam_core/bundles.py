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

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from agentiam_core.errors import AgentIAMError, ReasonCode
from agentiam_core.hashing import canonical_json

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "BundleSignatureError",
    "BundleWireError",
    "PolicyBundle",
    "bundle_file_payload",
    "bundle_from_file",
    "bundle_from_wire",
    "bundle_to_wire",
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
    #: The resource catalogue the `cedar_source` is written against — `{tool_id: {attribute:
    #: value}}`, left opaque here because the entity model belongs to the PEP and this
    #: package may not import it. `None` means the bundle carries none, which is not the same
    #: as carrying an empty one: an empty catalogue is an assertion that no tool has
    #: attributes, and `_UNKNOWN_TOOL`'s safe defaults then apply to every resource.
    #:
    #: **Inside the signature on purpose.** `resource.sensitivity` is an authorization input
    #: — the shipped corpus forbids critical resources to non-seniors on exactly that
    #: attribute — so a catalogue mounted beside the bundle rather than inside it is an
    #: authorization layer anyone with disk access can rewrite, which is the threat
    #: `verify_bundle` exists to close. Shipping the two together also means a policy and the
    #: attributes it reads cannot drift apart: one serial names both.
    tools: Mapping[str, Mapping[str, Any]] | None = None


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
        "tools": None if bundle.tools is None else dict(bundle.tools),
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


class BundleWireError(AgentIAMError):
    """A bundle fetched from the control plane is not in the shape `bundle_to_wire` writes.

    Raised before verification, because a payload that cannot be read back into the exact
    fields that were signed cannot be verified at all. A PEP treats it as it treats a bad
    signature: refuse it, count it, keep serving what it has (spec 05 §5.3).
    """

    reason_code = ReasonCode.POLICY_DENIED


def bundle_to_wire(bundle: PolicyBundle, signature: bytes) -> dict[str, Any]:
    """The JSON object the control plane serves for a bundle — spec 05 §5.7.3.

    Every signed field, plus the detached signature in base64. `created_at` is ISO 8601 with
    its offset: canonical JSON refuses a naive instant, so a timestamp that lost its offset in
    transit could never verify.
    """
    return {
        "serial": bundle.serial,
        "version": bundle.version,
        "cedar_source": bundle.cedar_source,
        "entity_schema": bundle.entity_schema,
        "created_at": None if bundle.created_at is None else bundle.created_at.isoformat(),
        "tools": None if bundle.tools is None else dict(bundle.tools),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def bundle_from_wire(data: Mapping[str, Any]) -> tuple[PolicyBundle, bytes]:
    """Read back what `bundle_to_wire` wrote. Verification is the caller's next step.

    Strict on purpose: every field must be present with its own type. A lenient reader that
    defaulted a missing `tools` to `None` would turn "the catalogue was lost in transit" into
    "this bundle has no catalogue", which verifies as a different bundle rather than failing.

    Raises:
        BundleWireError: A field is missing, mistyped, or unreadable.
    """
    if not isinstance(data, dict):
        raise BundleWireError("a bundle on the wire must be a JSON object")

    def field(name: str) -> object:
        if name not in data:
            raise BundleWireError(f"bundle field {name!r} is missing")
        return data[name]

    serial = field("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        raise BundleWireError(f"bundle field 'serial' must be a non-negative integer: {serial!r}")
    version = field("version")
    if not isinstance(version, str):
        raise BundleWireError("bundle field 'version' must be a string")
    cedar_source = field("cedar_source")
    if not isinstance(cedar_source, str):
        raise BundleWireError("bundle field 'cedar_source' must be a string")
    entity_schema = field("entity_schema")
    if entity_schema is not None and not isinstance(entity_schema, str):
        raise BundleWireError("bundle field 'entity_schema' must be a string or null")
    tools = field("tools")
    if tools is not None and not isinstance(tools, dict):
        raise BundleWireError("bundle field 'tools' must be an object or null")

    raw_created = field("created_at")
    created_at: datetime | None = None
    if raw_created is not None:
        if not isinstance(raw_created, str):
            raise BundleWireError("bundle field 'created_at' must be an ISO 8601 string or null")
        try:
            created_at = datetime.fromisoformat(raw_created)
        except ValueError as exc:
            raise BundleWireError(f"bundle field 'created_at' is not ISO 8601: {exc}") from exc
        if created_at.tzinfo is None:
            raise BundleWireError("bundle field 'created_at' has no UTC offset")

    raw_signature = field("signature")
    if not isinstance(raw_signature, str):
        raise BundleWireError("bundle field 'signature' must be a base64 string")
    try:
        signature = base64.b64decode(raw_signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BundleWireError(f"bundle field 'signature' is not base64: {exc}") from exc

    bundle = PolicyBundle(
        version=version,
        cedar_source=cedar_source,
        serial=serial,
        entity_schema=entity_schema,
        created_at=created_at,
        tools=tools,
    )
    return bundle, signature


def bundle_file_payload(bundle: PolicyBundle) -> dict[str, Any]:
    """The JSON a deployment's boot bundle file holds: the wire form without the signature.

    The signature lives beside it in its own file (`AGENTIAM_PEP_POLICY_BUNDLE_SIG_PATH`). One
    format for the file and the wire means one reader, so the PEP and the control plane cannot
    disagree about which fields were signed — they did once, over `created_at` (APICTA-01).
    """
    payload = bundle_to_wire(bundle, b"")
    del payload["signature"]
    return payload


def bundle_from_file(payload: Mapping[str, Any], signature: bytes) -> PolicyBundle:
    """Read a boot bundle file back. Verification is the caller's next step.

    Raises:
        BundleWireError: The file is not in the `bundle_file_payload` shape.
    """
    if not isinstance(payload, dict):
        raise BundleWireError("a bundle file must hold a JSON object")
    bundle, _ = bundle_from_wire(payload | {"signature": base64.b64encode(signature).decode()})
    return bundle
