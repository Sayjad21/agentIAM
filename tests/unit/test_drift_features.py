"""Drift features f1, f2 and f5 — T-033, spec 06 §5.

f1 and f2 need an embedding model and live on the PEP side; what lives here in core is
the pure part: the cosine itself, the two action renderings the embeddings are taken of,
and f5, which uses no model at all.

The case that matters most is `test_f5_sees_the_amount_attack_f2_cannot`. Spec 06 §5.1
measured a 211x payment inflation moving f2 by 0.0102 — inside the noise between aligned
cases. f5 exists because of that measurement, so the test that pins it is the one that
justifies the feature.

Rule 4: money is `Decimal`, never `float`.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from agentiam_core.drift_features import (
    DriftFeatures,
    action_template,
    argument_entity_overlap,
    cosine_similarity,
    rendered_action,
)

TASK = "Pay invoice INV-2291 from vendor Rahman Textiles for 45000 BDT"


class TestCosineSimilarity:
    def test_identical_vectors_are_one(self) -> None:
        assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_are_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_are_minus_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_a_zero_vector_is_zero_rather_than_a_division_error(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0

    def test_magnitude_does_not_matter(self) -> None:
        assert cosine_similarity([1.0, 1.0], [7.0, 7.0]) == pytest.approx(1.0)

    def test_mismatched_lengths_raise(self) -> None:
        # Silently zipping to the shorter vector would compare two different models'
        # output and report a plausible number for it.
        with pytest.raises(ValueError, match="length"):
            cosine_similarity([1.0, 2.0], [1.0])

    def test_result_is_finite_for_a_768_dimension_vector(self) -> None:
        a = [float(i % 7) for i in range(768)]
        b = [float((i + 3) % 7) for i in range(768)]
        assert math.isfinite(cosine_similarity(a, b))


class TestActionRendering:
    """f1 embeds the template, f2 embeds the rendering. They must differ."""

    def test_template_carries_scope_and_tool(self) -> None:
        assert action_template("payment:initiate", "payment_api") == (
            "payment:initiate using payment_api"
        )

    def test_template_without_a_tool_is_just_the_scope(self) -> None:
        assert action_template("invoice:read", None) == "invoice:read"

    def test_template_ignores_arguments_by_construction(self) -> None:
        # Spec 06 §5.1: f1 was constant at 0.4834 across three payment cases precisely
        # because the template cannot see arguments. That is the feature, not a bug.
        assert action_template("payment:initiate", "payment_api") == action_template(
            "payment:initiate", "payment_api"
        )

    def test_rendering_with_no_arguments_is_the_template(self) -> None:
        assert rendered_action("invoice:read", "invoice_api", {}) == (
            "invoice:read using invoice_api"
        )

    def test_rendering_includes_argument_values(self) -> None:
        text = rendered_action("payment:initiate", "payment_api", {"payment.to": "Rahman Textiles"})
        assert "Rahman Textiles" in text
        assert text.startswith("payment:initiate using payment_api with ")

    def test_rendering_unscales_numeric_arguments(self) -> None:
        # The extractor stores 45000 BDT as the 10^4-scaled int 450000000 (spec 10 §4.3).
        # Embedding "450000000" would be embedding a number the user never wrote.
        text = rendered_action("payment:initiate", "payment_api", {"payment.amount": 450000000})
        assert "45000" in text
        assert "450000000" not in text

    def test_rendering_a_decimal_argument_does_not_unscale_it(self) -> None:
        # `ArgValue` admits Decimal even though the extractor currently only emits int
        # and str. A Decimal is already a real quantity, so unscaling it the way an int
        # is unscaled would divide a correct number by 10,000.
        text = rendered_action("payment:initiate", "payment_api", {"a": Decimal("45000")})
        assert "45000" in text

    def test_rendering_is_deterministic_regardless_of_argument_order(self) -> None:
        a = rendered_action("s", "t", {"x.b": "two", "x.a": "one"})
        b = rendered_action("s", "t", {"x.a": "one", "x.b": "two"})
        assert a == b

    def test_rendering_differs_from_the_template(self) -> None:
        template = action_template("payment:initiate", "payment_api")
        rendered = rendered_action("payment:initiate", "payment_api", {"payment.amount": 450000000})
        assert rendered != template


class TestArgumentEntityOverlap:
    """f5 — spec 06 §5.2."""

    def test_no_arguments_is_full_overlap(self) -> None:
        # Nothing was asserted, so nothing is unaccounted for. Returning 0.0 here would
        # make every argument-free read look maximally drifted.
        assert argument_entity_overlap(TASK, {}) == Decimal("1.0000")

    def test_every_argument_present_is_one(self) -> None:
        args = {"invoice.id": "INV-2291", "vendor.name": "Rahman Textiles"}
        assert argument_entity_overlap(TASK, args) == Decimal("1.0000")

    def test_no_argument_present_is_zero(self) -> None:
        args = {"vendor.name": "Dhaka Holdings", "email.to": "competitor@example.com"}
        assert argument_entity_overlap(TASK, args) == Decimal("0.0000")

    def test_partial_overlap_is_the_fraction(self) -> None:
        args = {"invoice.id": "INV-2291", "vendor.name": "Dhaka Holdings"}
        assert argument_entity_overlap(TASK, args) == Decimal("0.5000")

    def test_matching_is_case_insensitive(self) -> None:
        assert argument_entity_overlap(TASK, {"v": "rahman textiles"}) == Decimal("1.0000")

    def test_a_scaled_amount_matches_the_plain_number_in_the_task(self) -> None:
        # 450000000 is 45000 BDT at the ledger's 10^4 scale, and the task says "45000".
        assert argument_entity_overlap(TASK, {"payment.amount": 450000000}) == Decimal("1.0000")

    def test_a_thousands_separator_in_the_task_still_matches(self) -> None:
        task = "Pay Rahman Textiles 45,000 BDT"
        assert argument_entity_overlap(task, {"payment.amount": 450000000}) == Decimal("1.0000")

    def test_numbers_compare_by_value_not_by_spelling(self) -> None:
        task = "Transfer 45000.0000 BDT"
        assert argument_entity_overlap(task, {"payment.amount": 450000000}) == Decimal("1.0000")

    def test_f5_sees_the_amount_attack_f2_cannot(self) -> None:
        """Spec 06 §5.1: a 211x inflation moved f2 by 0.0102. f5 must catch it outright."""
        aligned = argument_entity_overlap(TASK, {"payment.amount": 450000000})
        inflated = argument_entity_overlap(TASK, {"payment.amount": 95000000000})
        assert aligned == Decimal("1.0000")
        assert inflated == Decimal("0.0000")

    def test_a_decimal_argument_matches_by_value_and_is_not_unscaled(self) -> None:
        # Same contract as the rendering: a Decimal is a real quantity, not a scaled one.
        assert argument_entity_overlap(TASK, {"a": Decimal("45000")}) == Decimal("1.0000")
        assert argument_entity_overlap(TASK, {"a": Decimal("450000000")}) == Decimal("0.0000")

    def test_a_substring_of_a_larger_number_does_not_count_as_a_match(self) -> None:
        # "4500" must not match the task's "45000" — that is how an amount attack would
        # slip past a naive substring check.
        task = "Transfer 45000 BDT"
        assert argument_entity_overlap(task, {"payment.amount": 45000000}) == Decimal("0.0000")

    def test_an_account_id_keeps_its_leading_zeros(self) -> None:
        # The extractor preserves "0012" as a string rather than the int 12 (spec 10 §4.1),
        # and f5 must not undo that by comparing numerically.
        task = "Send to account 0012"
        assert argument_entity_overlap(task, {"payment.to": "0012"}) == Decimal("1.0000")
        assert argument_entity_overlap("Send to account 12", {"payment.to": "0012"}) == Decimal(
            "0.0000"
        )

    def test_unicode_is_normalized_before_comparison(self) -> None:
        # NFKC: the fullwidth form must match the ASCII form, or a caller can evade f5
        # with a codepoint the reader cannot see.
        #
        # Constructed rather than written as a literal, on purpose. RUF001 rightly
        # objects to ambiguous characters in source, and this repository has already lost
        # 232 characters to an encoding accident once; a test for homoglyphs is the last
        # place that should smuggle homoglyphs into the tree.
        # U+FF21 FULLWIDTH LATIN CAPITAL LETTER A sits 0xFEE0 above ASCII "A".
        fullwidth_acme = "".join(chr(ord(c) + 0xFEE0) for c in "ACME")
        assert argument_entity_overlap(f"Pay {fullwidth_acme} now", {"v": "ACME"}) == Decimal(
            "1.0000"
        )

    def test_an_empty_task_text_matches_nothing(self) -> None:
        assert argument_entity_overlap("", {"v": "ACME"}) == Decimal("0.0000")

    def test_the_result_is_always_decimal_never_float(self) -> None:
        # Rule 4, and DecisionRecord rejects floats outright.
        assert isinstance(argument_entity_overlap(TASK, {"v": "ACME"}), Decimal)

    @pytest.mark.parametrize("count", [1, 2, 3, 7])
    def test_the_result_is_bounded_to_the_unit_interval(self, count: int) -> None:
        args: dict[str, str] = {f"k{i}": f"absent-{i}" for i in range(count)}
        args["present"] = "Rahman Textiles"
        value = argument_entity_overlap(TASK, args)
        assert Decimal(0) <= value <= Decimal(1)


class TestDriftFeatures:
    def test_features_default_to_absent(self) -> None:
        # An extraction failure degrades to absent features, never to a denial
        # (spec 06 §5.3).
        features = DriftFeatures()
        assert features.f1 is None
        assert features.f2 is None
        assert features.f5 is None

    def test_features_are_decimal(self) -> None:
        features = DriftFeatures(f1=Decimal("0.48"), f2=Decimal("0.81"), f5=Decimal("1.0"))
        assert isinstance(features.f1, Decimal)

    def test_features_are_frozen(self) -> None:
        features = DriftFeatures(f5=Decimal("1.0"))
        with pytest.raises((AttributeError, TypeError)):
            features.f5 = Decimal("0.0")  # type: ignore[misc]

    def test_as_dict_omits_absent_features(self) -> None:
        # What lands in the audit record: recording `f1: null` for a request that was
        # never scored claims an observation that did not happen.
        assert DriftFeatures(f5=Decimal("1.0")).as_dict() == {"f5": Decimal("1.0")}

    def test_as_dict_of_nothing_is_empty(self) -> None:
        assert DriftFeatures().as_dict() == {}
