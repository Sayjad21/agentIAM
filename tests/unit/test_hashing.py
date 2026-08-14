"""Canonical serialization and hashing (`agentiam_core.hashing`).

The audit chain, the intent hash, and `arg_digest` all rest on canonical JSON producing
identical bytes for semantically identical input. If it does not, chain verification
fails on honest data and the tamper-evidence claim collapses.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agentiam_core.errors import CanonicalizationError
from agentiam_core.hashing import canonical_json, chain_hash, hash_object, sha256_hex


class TestCanonicalJson:
    def test_key_order_does_not_matter(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_nested_key_order_does_not_matter(self) -> None:
        left = {"outer": {"z": [{"q": 1, "p": 2}], "a": 3}}
        right = {"outer": {"a": 3, "z": [{"p": 2, "q": 1}]}}
        assert canonical_json(left) == canonical_json(right)

    def test_list_order_is_significant(self) -> None:
        """Lists are sequences. Reordering one changes meaning, unlike dict keys."""
        assert canonical_json([1, 2]) != canonical_json([2, 1])

    def test_output_is_utf8_bytes_without_whitespace(self) -> None:
        out = canonical_json({"a": 1, "b": "x"})
        assert isinstance(out, bytes)
        assert b" " not in out
        assert out == b'{"a":1,"b":"x"}'

    def test_non_ascii_is_not_escaped(self) -> None:
        r"""Bengali text survives as UTF-8 rather than \uXXXX escapes (EC-T16)."""
        out = canonical_json({"name": "প্যাকেজিং"})
        assert "প্যাকেজিং".encode() in out

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("500000"), b'"500000.0000"'),
            (Decimal("0"), b'"0.0000"'),
            (Decimal("0.0001"), b'"0.0001"'),
            (Decimal("-12.5"), b'"-12.5000"'),
            (Decimal("1E+3"), b'"1000.0000"'),
        ],
    )
    def test_decimal_serializes_to_fixed_scale_string(
        self, value: Decimal, expected: bytes
    ) -> None:
        """Money is exact and must not become a float in transit."""
        assert canonical_json(value) == expected

    def test_decimals_equal_in_value_serialize_identically(self) -> None:
        assert canonical_json(Decimal("1.5")) == canonical_json(Decimal("1.5000"))

    def test_decimal_exceeding_scale_is_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="scale"):
            canonical_json(Decimal("0.00001"))

    def test_float_is_rejected(self) -> None:
        """A float anywhere in an audited structure is a bug, not a coercion."""
        with pytest.raises(CanonicalizationError, match="float"):
            canonical_json({"amount": 1.5})

    def test_datetime_serializes_as_utc_rfc3339(self) -> None:
        ts = datetime(2026, 8, 14, 11, 45, 0, tzinfo=UTC)
        assert canonical_json(ts) == b'"2026-08-14T11:45:00Z"'

    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="timezone"):
            canonical_json(datetime(2026, 8, 14, 11, 45, 0))

    def test_sets_serialize_as_sorted_arrays(self) -> None:
        assert canonical_json(frozenset({"b", "a"})) == canonical_json(frozenset({"a", "b"}))
        assert canonical_json({"c", "a", "b"}) == b'["a","b","c"]'

    def test_unsupported_type_is_rejected(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonical_json(object())

    def test_non_finite_decimal_is_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="non-finite"):
            canonical_json(Decimal("NaN"))

    def test_infinite_decimal_is_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="non-finite"):
            canonical_json(Decimal("Infinity"))

    def test_uuid_serializes_as_a_string(self) -> None:
        from uuid import UUID

        value = UUID("01234567-89ab-cdef-0123-456789abcdef")
        assert canonical_json(value) == b'"01234567-89ab-cdef-0123-456789abcdef"'

    def test_str_enum_serializes_as_its_value(self) -> None:
        """A StrEnum is a str, so it takes the string branch — same result either way."""
        from agentiam_core.errors import ReasonCode

        assert canonical_json(ReasonCode.TOKEN_EXPIRED) == b'"TOKEN_EXPIRED"'

    def test_non_str_enum_serializes_as_its_value(self) -> None:
        """An Enum whose value is not a str needs the dedicated branch."""
        import enum

        class Priority(enum.Enum):
            HIGH = 1
            LOW = 2

        assert canonical_json(Priority.HIGH) == b"1"
        assert canonical_json({"p": Priority.LOW}) == b'{"p":2}'

    def test_bool_is_not_treated_as_int(self) -> None:
        """Bool subclasses int; without an explicit branch True would render as 1."""
        assert canonical_json(True) == b"true"
        assert canonical_json(False) == b"false"
        assert canonical_json(1) == b"1"

    def test_none_renders_as_null(self) -> None:
        assert canonical_json(None) == b"null"

    def test_control_characters_are_escaped(self) -> None:
        assert canonical_json('a\nb\tc"d\\e') == b'"a\\nb\\tc\\"d\\\\e"'
        assert canonical_json("\x00") == b'"\\u0000"'
        assert canonical_json("\r") == b'"\\r"'
        assert canonical_json("\x7f") == b'"\\u007f"'

    def test_tuples_are_sequences(self) -> None:
        assert canonical_json((1, 2)) == canonical_json([1, 2])


class TestUnicodeNormalization:
    """NFC normalization, so visually identical strings hash identically (P-14)."""

    def test_nfc_and_nfd_normalize_to_the_same_bytes(self) -> None:
        composed = unicodedata.normalize("NFC", "é")
        decomposed = unicodedata.normalize("NFD", "é")
        assert composed != decomposed  # different code points
        assert canonical_json(composed) == canonical_json(decomposed)

    def test_normalization_applies_to_keys_too(self) -> None:
        composed = unicodedata.normalize("NFC", "café")
        decomposed = unicodedata.normalize("NFD", "café")
        assert canonical_json({composed: 1}) == canonical_json({decomposed: 1})

    def test_bengali_normalization_is_stable(self) -> None:
        text = "ঢাকা প্যাকেজিং লিমিটেড"
        assert canonical_json(unicodedata.normalize("NFD", text)) == canonical_json(text)


class TestHashing:
    def test_sha256_hex_is_64_lowercase_hex_chars(self) -> None:
        digest = sha256_hex(b"")
        assert len(digest) == 64
        assert digest == digest.lower()
        assert digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_hash_object_is_order_independent(self) -> None:
        assert hash_object({"a": 1, "b": 2}) == hash_object({"b": 2, "a": 1})

    def test_hash_object_detects_any_change(self) -> None:
        base = {"seq": 1, "amount": Decimal("100.0000")}
        changed = {"seq": 1, "amount": Decimal("100.0001")}
        assert hash_object(base) != hash_object(changed)

    def test_chain_hash_depends_on_both_inputs(self) -> None:
        rec = {"seq": 1}
        a = chain_hash(None, rec)
        b = chain_hash("00" * 32, rec)
        assert a != b
        assert chain_hash("00" * 32, rec) == b

    def test_chain_hash_genesis_accepts_none(self) -> None:
        assert len(chain_hash(None, {"seq": 0})) == 64

    def test_chain_detects_a_single_record_mutation(self) -> None:
        """P-16 in miniature: rebuilding the chain over tampered data diverges."""
        records = [{"seq": i, "v": i * 10} for i in range(5)]

        def build(recs: list[dict[str, int]]) -> list[str]:
            out: list[str] = []
            prev: str | None = None
            for r in recs:
                prev = chain_hash(prev, r)
                out.append(prev)
            return out

        original = build(records)
        tampered = [dict(r) for r in records]
        tampered[2]["v"] = 999
        assert build(tampered)[-1] != original[-1]
