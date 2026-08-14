r"""Canonical serialization and the audit hash chain.

Three things in AgentIAM depend on two semantically identical values producing byte-identical
output: the intent hash bound into every token, `arg_digest` on decision records, and the
hash chain that makes the audit ledger tamper-evident (`PLAN.md` §6.8).

The chain is the demanding one. Verification recomputes hashes over stored records, so any
non-determinism — a dict iteration order, a Unicode normalization form, a float's repr —
makes verification fail on honest data. A tamper detector that reports false positives gets
switched off, which leaves you worse off than having none.

Rules, all of them chosen to remove a degree of freedom:

* object keys sorted by code point, no insertion-order dependence
* no whitespace
* UTF-8 output, non-ASCII left as characters rather than ``\\uXXXX`` escapes
* every string NFC-normalized, keys included
* ``Decimal`` rendered as a fixed 4-place string — exact, and never a float
* ``datetime`` rendered as RFC 3339 UTC with a ``Z`` suffix
* sets rendered as sorted arrays
* floats, naive datetimes, and unknown types rejected rather than coerced

This module performs no I/O and reads no clock.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final
from uuid import UUID

from agentiam_core.errors import CanonicalizationError

#: Decimal places money and budget quantities are held to. Matches ``NUMERIC(20,4)``.
DECIMAL_PLACES: Final = 4

_QUANTUM: Final = Decimal(1).scaleb(-DECIMAL_PLACES)


def _normalize(text: str) -> str:
    """NFC-normalize so visually identical strings hash identically."""
    return unicodedata.normalize("NFC", text)


def _encode_str(value: str) -> str:
    """Encode a string as a JSON string literal, escaping only what JSON requires."""
    out = ['"']
    for ch in _normalize(value):
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _encode_decimal(value: Decimal) -> str:
    """Render a Decimal at exactly ``DECIMAL_PLACES``, as a string.

    A string, not a JSON number, because JSON numbers are floats to most parsers and this
    is money. Quantizing means ``1.5`` and ``1.5000`` produce identical bytes.
    """
    if not value.is_finite():
        raise CanonicalizationError(f"cannot canonicalize non-finite Decimal: {value}")
    try:
        quantized = value.quantize(_QUANTUM)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the scale check below
        raise CanonicalizationError(f"Decimal out of representable range: {value}") from exc
    if quantized != value:
        raise CanonicalizationError(
            f"Decimal {value} exceeds the fixed scale of {DECIMAL_PLACES} places; "
            f"rounding money silently is how ledgers stop balancing"
        )
    return _encode_str(f"{quantized:.{DECIMAL_PLACES}f}")


def _encode_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise CanonicalizationError(
            f"naive datetime {value!r} has no unambiguous instant; attach a timezone"
        )
    return _encode_str(value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _encode(value: Any) -> str:  # noqa: ANN401 - deliberately accepts arbitrary input
    # bool before int: bool is a subclass of int and would otherwise render as 0/1.
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        raise CanonicalizationError(
            f"float {value!r} is not exactly representable; use Decimal or int"
        )
    if isinstance(value, Decimal):
        return _encode_decimal(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _encode_str(value)
    if isinstance(value, datetime):
        return _encode_datetime(value)
    if isinstance(value, UUID):
        return _encode_str(str(value))
    if isinstance(value, Enum):
        return _encode(value.value)
    if isinstance(value, Mapping):
        items = sorted((_normalize(str(k)), v) for k, v in value.items())
        return "{" + ",".join(f"{_encode_str(k)}:{_encode(v)}" for k, v in items) + "}"
    if isinstance(value, frozenset | set):
        # Sets have no order, so impose one: sort by encoded form, which is total.
        return "[" + ",".join(sorted(_encode(v) for v in value)) + "]"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        # Sequence order is meaningful and preserved.
        return "[" + ",".join(_encode(v) for v in value) + "]"
    raise CanonicalizationError(f"no canonical encoding for {type(value).__name__}")


def canonical_json(value: Any) -> bytes:  # noqa: ANN401 - deliberately accepts arbitrary input
    """Serialize `value` to canonical JSON bytes.

    Deterministic across dict ordering and Unicode normalization form (P-13, P-14).

    Args:
        value: Any JSON-shaped structure of the supported types.

    Returns:
        UTF-8 encoded canonical JSON.

    Raises:
        CanonicalizationError: For floats, naive datetimes, over-precise decimals, or any
            type without a defined encoding.
    """
    return _encode(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """SHA-256 of `data` as 64 lowercase hex characters."""
    return hashlib.sha256(data).hexdigest()


def hash_object(value: Any) -> str:  # noqa: ANN401 - deliberately accepts arbitrary input
    """SHA-256 over the canonical JSON of `value`."""
    return sha256_hex(canonical_json(value))


def chain_hash(prev_hash: str | None, record: Any) -> str:  # noqa: ANN401
    """Compute an audit chain link.

    ``record_n.record_hash = sha256(canonical_json({prev, record}))``. Binding the previous
    hash *inside* the hashed structure is what makes the chain tamper-evident: altering any
    earlier record changes every hash after it (P-16).

    Args:
        prev_hash: The preceding record's hash, or None for the genesis record.
        record: The record body.

    Returns:
        The new record's hash, 64 lowercase hex characters.
    """
    return hash_object({"prev": prev_hash, "record": record})
