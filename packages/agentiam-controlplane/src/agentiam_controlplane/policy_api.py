"""`GET /v1/policy/bundles/...` — what a PEP fetches — APICTA-01, spec 05 §5.7.3.

Two routes, both unauthenticated in the same way `GET /v1/revocations` is: bundles are
**integrity-protected, not confidential**. A PEP verifies every bundle it fetches against the
bundle key's public half, so serving one to anybody gains an attacker nothing they could use;
what it does reveal is the policy text, which spec 05 §8 records as a known limitation.

The body is `agentiam_core.bundles.bundle_to_wire` — the one definition both sides share — plus
a `provenance` object that is *not* signed and that a PEP ignores: who activated it, which
serial a rollback re-published, and the digest of the English it was compiled from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from agentiam_controlplane.db import policy_bundles as bundle_store
from agentiam_core.bundles import bundle_to_wire

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["build_router"]


def _serialize(row: bundle_store.PolicyBundleRow) -> dict[str, Any]:
    return bundle_to_wire(bundle_store.bundle_of(row), row.signature) | {
        "provenance": {
            "activated_by": row.activated_by,
            "source_serial": row.source_serial,
            "nl_digest": row.nl_digest,
        }
    }


def build_router(*, session_factory: async_sessionmaker[AsyncSession]) -> APIRouter:
    """The two read routes, bound to `session_factory`."""
    router = APIRouter(prefix="/v1/policy/bundles", tags=["policy"])

    @router.get("/current")
    async def current_bundle() -> JSONResponse:
        """The newest bundle. 404 before anything has been published or imported."""
        async with session_factory() as session:
            row = await bundle_store.current(session)
        if row is None:
            return JSONResponse({"detail": "no policy bundle has been published"}, status_code=404)
        return JSONResponse(_serialize(row))

    @router.get("/{serial}")
    async def bundle_by_serial(serial: int) -> JSONResponse:
        """One bundle by serial."""
        async with session_factory() as session:
            row = await bundle_store.get(session, serial)
        if row is None:
            return JSONResponse({"detail": f"no bundle has serial {serial}"}, status_code=404)
        return JSONResponse(_serialize(row))

    return router
