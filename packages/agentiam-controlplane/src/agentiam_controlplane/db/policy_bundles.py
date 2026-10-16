"""The bundle store: publish, roll back, import, read — APICTA-01, spec 05 §5.7.3.

The control plane is where policy is *published*; every PEP verifies what it receives
against the bundle key's public half (spec 05 §5.1). Four rules shape this module:

* **The serial comes from a sequence, before signing.** The signature covers the serial, so
  `nextval`, build, sign and insert happen in one transaction. `max(serial) + 1` would hand
  two concurrent activations the same serial.
* **Persist, then publish.** The event goes out only after the commit, and a broker failure
  is logged and swallowed — the row is the truth and PEPs pull it regardless (spec 05 §5.7.4,
  the rule `db/revocations.py` follows for the same reason).
* **The catalogue is carried forward.** An activation supplies Cedar; the new bundle takes
  the current bundle's `tools`, so a policy and the attributes it reads stay one artifact.
* **A rollback is a new serial.** Re-sending an old serial would be refused by every PEP as a
  rollback attack (spec 05 §5.2), correctly.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final, Protocol

from sqlalchemy import func, select, text

from agentiam_controlplane.db.models import PolicyBundleRow
from agentiam_core.bundles import PolicyBundle, bundle_from_file, sign_bundle, verify_bundle

if TYPE_CHECKING:
    from datetime import datetime

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

    from agentiam_controlplane.settings import PolicyPublishingSettings

logger = logging.getLogger(__name__)

__all__ = [
    "CHANNEL",
    "BundleNotFound",
    "PolicyBundleRow",
    "PolicyPublisher",
    "RedisPolicyPublisher",
    "bundle_of",
    "current",
    "get",
    "history",
    "import_initial",
    "import_initial_from",
    "publish",
    "republish",
]

#: Where PEPs listen for "a newer bundle exists" (spec 05 §5.7.4).
CHANNEL: Final = "agentiam:policy"

#: Who an imported deployment bundle is recorded as activated by.
_BOOTSTRAP: Final = "bootstrap"


class BundleNotFound(LookupError):
    """No bundle with that serial exists."""


class PolicyPublisher(Protocol):
    """Announces a newly published serial. A failure is tolerated, never fatal."""

    async def publish(self, *, serial: int) -> None:
        """Tell PEPs a newer bundle exists."""
        ...


class RedisPolicyPublisher:
    """Publishes `{"event": "policy:reload", "serial": n}` on `CHANNEL`."""

    def __init__(self, client: Redis) -> None:
        """Hold a shared client; nothing here owns its lifecycle."""
        self._client = client

    async def publish(self, *, serial: int) -> None:
        """Announce one serial. Raises on a Redis error; `publish()` below catches it."""
        await self._client.publish(
            CHANNEL, json.dumps({"event": "policy:reload", "serial": serial})
        )


# --- reading ----------------------------------------------------------------------------


def bundle_of(row: PolicyBundleRow) -> PolicyBundle:
    """The signed fields of a row, exactly as they were signed."""
    return PolicyBundle(
        version=row.version,
        cedar_source=row.cedar_source,
        serial=row.serial,
        entity_schema=row.entity_schema,
        created_at=row.created_at,
        tools=row.tools,
    )


async def current(session: AsyncSession) -> PolicyBundleRow | None:
    """The bundle with the highest serial, or `None` before anything is published."""
    stmt = select(PolicyBundleRow).order_by(PolicyBundleRow.serial.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get(session: AsyncSession, serial: int) -> PolicyBundleRow | None:
    """One bundle by serial."""
    return await session.get(PolicyBundleRow, serial)


async def history(session: AsyncSession, *, limit: int = 20) -> list[PolicyBundleRow]:
    """The most recent bundles, newest first."""
    stmt = select(PolicyBundleRow).order_by(PolicyBundleRow.serial.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars())


# --- writing ----------------------------------------------------------------------------


async def publish(
    session: AsyncSession,
    *,
    cedar_source: str,
    signing_key: Ed25519PrivateKey,
    activated_by: str,
    now: datetime,
    nl_digest: str | None = None,
    publisher: PolicyPublisher | None = None,
) -> PolicyBundleRow:
    """Sign and store `cedar_source` as the next bundle, then announce it.

    The caller has already run the activation gate (spec 05 §5.5); this does not re-run the
    corpus, because the store is not where policy correctness is decided.

    Args:
        session: A fresh session; this opens and commits its own transaction.
        cedar_source: The policy, already corpus-tested.
        signing_key: The bundle key's private half.
        activated_by: The session principal. Never a request-body field.
        now: The bundle's `created_at`, inside the signature.
        nl_digest: `sha256[:16]` of the English it was compiled from, when there was one.
        publisher: Where to announce the serial. `None` leaves PEPs to their pull.
    """
    async with session.begin():
        previous = await current(session)
        row = await _insert(
            session,
            cedar_source=cedar_source,
            tools=previous.tools if previous is not None else None,
            entity_schema=previous.entity_schema if previous is not None else None,
            signing_key=signing_key,
            activated_by=activated_by,
            now=now,
            nl_digest=nl_digest,
        )
    await _announce(row.serial, publisher)
    return row


async def republish(
    session: AsyncSession,
    *,
    source_serial: int,
    signing_key: Ed25519PrivateKey,
    activated_by: str,
    now: datetime,
    publisher: PolicyPublisher | None = None,
) -> PolicyBundleRow:
    """Roll back by publishing an earlier bundle's policy and catalogue as a new serial.

    Raises:
        BundleNotFound: No bundle has `source_serial`.
    """
    async with session.begin():
        source = await get(session, source_serial)
        if source is None:
            raise BundleNotFound(f"no bundle has serial {source_serial}")
        row = await _insert(
            session,
            cedar_source=source.cedar_source,
            tools=source.tools,
            entity_schema=source.entity_schema,
            signing_key=signing_key,
            activated_by=activated_by,
            now=now,
            source_serial=source_serial,
        )
    await _announce(row.serial, publisher)
    return row


async def import_initial(
    session: AsyncSession,
    *,
    bundle: PolicyBundle,
    signature: bytes,
    public_key: Ed25519PublicKey,
    now: datetime,
) -> PolicyBundleRow | None:
    """Store the deployment's boot bundle as the first row, if the store is empty.

    Every PEP boots from this bundle, so the control plane's serials must continue past it:
    a new bundle numbered 1 would be refused by all of them as a rollback. Verified first,
    because a bundle no PEP would accept is a misconfiguration to surface at startup.

    Returns:
        The imported row, or `None` when the store already holds bundles.

    Raises:
        BundleSignatureError: The bundle does not verify against `public_key`.
    """
    verify_bundle(bundle, signature, public_key)
    async with session.begin():
        held = await session.scalar(select(func.count()).select_from(PolicyBundleRow))
        if held:
            return None
        row = PolicyBundleRow(
            serial=bundle.serial,
            version=bundle.version,
            cedar_source=bundle.cedar_source,
            entity_schema=bundle.entity_schema,
            tools=dict(bundle.tools) if bundle.tools is not None else None,
            created_at=bundle.created_at,
            signature=signature,
            activated_by=_BOOTSTRAP,
            activated_at=now,
        )
        session.add(row)
        await session.flush()
        # `setval(.., n, true)`: the next `nextval` returns n + 1.
        await session.execute(
            text("SELECT setval('policy_bundle_serial', :serial, true)"),
            {"serial": max(bundle.serial, 1)},
        )
    return row


async def import_initial_from(
    session: AsyncSession, settings: PolicyPublishingSettings, *, now: datetime
) -> PolicyBundleRow | None:
    """`import_initial` from the files `bootstrap_demo_secrets.py` writes.

    Read with the same `bundle_from_file` the PEP boots with, so the two cannot disagree about
    which fields were signed.

    Returns `None` when no initial bundle is configured or the store already holds bundles.

    Raises:
        BundleWireError: The file is not in the shared shape.
        BundleSignatureError: The bundle does not verify.
    """
    if settings.initial_bundle_path is None or settings.initial_bundle_sig_path is None:
        return None
    signature = settings.initial_bundle_sig_path.read_bytes()
    payload: dict[str, Any] = json.loads(settings.initial_bundle_path.read_text(encoding="utf-8"))
    return await import_initial(
        session,
        bundle=bundle_from_file(payload, signature),
        signature=signature,
        public_key=settings.signing_key.public_key(),
        now=now,
    )


# --- internals --------------------------------------------------------------------------


async def _insert(
    session: AsyncSession,
    *,
    cedar_source: str,
    tools: dict[str, Any] | None,
    entity_schema: str | None,
    signing_key: Ed25519PrivateKey,
    activated_by: str,
    now: datetime,
    nl_digest: str | None = None,
    source_serial: int | None = None,
) -> PolicyBundleRow:
    serial = int(await session.scalar(text("SELECT nextval('policy_bundle_serial')")))
    bundle = PolicyBundle(
        version=f"bundle-{serial}",
        cedar_source=cedar_source,
        serial=serial,
        entity_schema=entity_schema,
        created_at=now,
        tools=tools,
    )
    row = PolicyBundleRow(
        serial=serial,
        version=bundle.version,
        cedar_source=cedar_source,
        entity_schema=entity_schema,
        tools=tools,
        created_at=now,
        signature=sign_bundle(bundle, signing_key),
        activated_by=activated_by,
        activated_at=now,
        source_serial=source_serial,
        nl_digest=nl_digest,
    )
    session.add(row)
    await session.flush()
    return row


async def _announce(serial: int, publisher: PolicyPublisher | None) -> None:
    if publisher is None:
        return
    try:
        await publisher.publish(serial=serial)
    except Exception:
        # The bundle is committed; PEPs that missed the event still pull it.
        logger.warning("policy bundle %d persisted but not announced", serial, exc_info=True)
