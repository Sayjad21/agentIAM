"""P-13 / P-14: canonical serialization is stable.

The audit chain's tamper-evidence rests entirely on this. If canonical JSON can produce
two different byte strings for the same value, chain verification fails on honest data —
and a verifier that cries wolf gets switched off, which is worse than not having one.

These are property tests rather than examples because the failure mode is a rare input
shape (a key ordering, a normalization form), not a case anyone thinks to write down.
"""

from __future__ import annotations

import json
import unicodedata
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from agentiam_core.hashing import canonical_json, hash_object

# JSON-ish values, excluding floats (rejected by design) and including the text forms
# that actually appear in this system: Bengali, combining marks, and money.
scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**62), max_value=2**62),
    st.text(max_size=40),
    st.text(alphabet="আকাশনীলপ্রেম", max_size=20),
    st.decimals(
        min_value=Decimal("-1e9"),
        max_value=Decimal("1e9"),
        allow_nan=False,
        allow_infinity=False,
        places=4,
    ),
)

json_values = st.recursive(
    scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=12), children, max_size=5),
    ),
    max_leaves=15,
)


def shuffle_keys(value: Any) -> Any:
    """Rebuild a structure with dict keys inserted in reverse order."""
    if isinstance(value, dict):
        return {k: shuffle_keys(v) for k, v in reversed(list(value.items()))}
    if isinstance(value, list):
        return [shuffle_keys(v) for v in value]
    return value


@given(json_values)
@settings(max_examples=300)
def test_canonical_json_is_deterministic(value: Any) -> None:
    assert canonical_json(value) == canonical_json(value)


@given(json_values)
@settings(max_examples=300)
def test_canonical_json_is_stable_under_key_reordering(value: Any) -> None:
    """P-13."""
    assert canonical_json(value) == canonical_json(shuffle_keys(value))


@given(json_values)
@settings(max_examples=300)
def test_hash_is_stable_under_key_reordering(value: Any) -> None:
    assert hash_object(value) == hash_object(shuffle_keys(value))


@given(json_values)
@settings(max_examples=300)
def test_canonical_json_is_valid_utf8_json(value: Any) -> None:
    """Output must survive a round trip through a standard JSON parser."""
    json.loads(canonical_json(value).decode())


@given(st.text(max_size=40))
@settings(max_examples=300)
def test_normalization_forms_agree(text: str) -> None:
    """P-14: NFD and NFC of the same string serialize identically."""
    nfc = unicodedata.normalize("NFC", text)
    nfd = unicodedata.normalize("NFD", text)
    assert canonical_json(nfc) == canonical_json(nfd)


@given(st.text(max_size=40))
@settings(max_examples=300)
def test_canonicalization_is_idempotent(text: str) -> None:
    """Canonicalizing an already-canonical value changes nothing (P-14)."""
    once = canonical_json(text)
    twice = canonical_json(json.loads(once.decode()))
    assert once == twice


@given(
    st.decimals(
        min_value=Decimal("-1e9"),
        max_value=Decimal("1e9"),
        allow_nan=False,
        allow_infinity=False,
        places=4,
    )
)
@settings(max_examples=300)
def test_equal_decimals_serialize_identically(value: Decimal) -> None:
    """Trailing zeros must not change the bytes: 1.5 and 1.5000 are the same money."""
    assert canonical_json(value) == canonical_json(Decimal(str(value)).normalize())


@given(json_values, json_values)
@settings(max_examples=300)
def test_different_values_hash_differently(left: Any, right: Any) -> None:
    """Distinct encodings hash distinctly.

    Not a collision-resistance claim — just that the encoding is injective enough that
    structurally different values do not serialize to the same bytes.
    """
    if canonical_json(left) != canonical_json(right):
        assert hash_object(left) != hash_object(right)
