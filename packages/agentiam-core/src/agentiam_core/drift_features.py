"""Drift features — T-033, spec 06 §5.

`PLAN.md` §6.6 defines six features feeding a calibrated classifier. T-034 (the labelled
dataset) and T-035 (the classifier) are deferred, so nothing consumes a feature vector this
cycle; f1, f2 and f5 are computed and recorded for the deferred research path. f3, f4 and f6
need per-task history the PEP deliberately does not hold — ADR-036.

What lives here is the pure half:

* `cosine_similarity` — the arithmetic behind f1 and f2, given vectors somebody else fetched.
* `action_template` / `rendered_action` — the two strings f1 and f2 are embeddings *of*. The
  distinction is the whole reason they are two features: the template cannot see arguments
  by construction, so f1 is a scope signal and f2 is an argument signal (spec 06 §5.1).
* `argument_entity_overlap` — f5 entire. It needs no model.

Fetching embeddings is I/O and lives in `agentiam_pep.drift`, per the core purity rule
(`PLAN.md` §5).

**f5 is not a nicety.** Spec 06 §5.1 measured a 211x payment inflation moving f2 by 0.0102 —
inside the noise between aligned cases. Embeddings are near-blind to numeric magnitude, so
the only feature that can see an amount attack is the symbolic one.

Rule 4: money is `Decimal`, never `float`.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "SCALE",
    "DriftFeatures",
    "action_template",
    "argument_entity_overlap",
    "cosine_similarity",
    "rendered_action",
]

#: Numeric args arrive 10^4-scaled from the extractor (spec 10 §4.3), exactly like budgets.
#: Verified against `extract()`: every `int` in `Extraction.args` is scaled, and every
#: non-numeric arg stays a `str` — so unscaling every int needs no extra declaration.
SCALE: Final = 10**4

#: What `RequestContext.args` admits, mirrored from the extractor.
ArgValue = Decimal | int | str

#: Numbers as a human writes them: optional thousands separators, optional decimal part.
_NUMBER_RE: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")

_QUANTUM: Final = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class DriftFeatures:
    """One request's feature vector. Absent means *not computed*, never *zero*.

    An extraction failure degrades to absent features rather than to a denial
    (spec 06 §5.3), so `None` has to stay distinguishable from a genuine 0.0 — a request
    where every argument was foreign scores f5 = 0.0, and that is a real observation.
    """

    f1: Decimal | None = None
    f2: Decimal | None = None
    f5: Decimal | None = None

    def as_dict(self) -> dict[str, Decimal]:
        """The features that were actually computed, for the audit record.

        Absent features are omitted rather than serialized as null: recording `f1: null`
        would claim an observation that never happened.
        """
        pairs = (("f1", self.f1), ("f2", self.f2), ("f5", self.f5))
        return {name: value for name, value in pairs if value is not None}


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine of the angle between two vectors, or 0.0 if either has no direction.

    Raises:
        ValueError: The vectors differ in length. Zipping to the shorter one would
            silently compare two different models' output and report a plausible
            number for it.
    """
    if len(a) != len(b):
        raise ValueError(f"vectors differ in length: {len(a)} != {len(b)}")

    # `fsum` rather than `sum`: 768 terms of accumulated rounding is exactly the case
    # pairwise summation exists for, and the cost is negligible beside the network hop
    # that produced the vectors.
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(math.fsum(x * x for x in a))
    norm_b = math.sqrt(math.fsum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        # A zero vector has no direction, so the angle is undefined. 0.0 is the neutral
        # answer; raising would turn a degenerate embedding into a failed request.
        return 0.0
    return dot / (norm_a * norm_b)


def action_template(scope: str, tool: str | None) -> str:
    """The action *without* argument values — what f1 embeds.

    Constant for a given route, which is exactly the point: f1 measures how well the
    scope alone matches the task, and cannot be moved by the arguments.
    """
    return f"{scope} using {tool}" if tool else scope


def rendered_action(scope: str, tool: str | None, args: Mapping[str, ArgValue]) -> str:
    """The action *with* argument values substituted — what f2 embeds.

    Arguments are sorted by key so the rendering, and therefore the cache key and the
    embedding, do not depend on dict insertion order. Numeric args are unscaled first:
    embedding `450000000` would be embedding a number the operator never wrote.
    """
    template = action_template(scope, tool)
    if not args:
        return template

    parts = [f"{_short_key(key)}={_render_value(args[key])}" for key in sorted(args)]
    return f"{template} with {', '.join(parts)}"


def argument_entity_overlap(task_text: str, args: Mapping[str, ArgValue]) -> Decimal:
    """f5 — the fraction of argument values that appear in the task text (spec 06 §5.2).

    Empty `args` scores 1.0: nothing was asserted, so nothing is unaccounted for.
    Returning 0.0 there would make every argument-free read look maximally drifted.

    Strings match case-folded and NFKC-normalized, so a fullwidth homoglyph cannot evade
    the check. Numbers match by **value**, so the ledger's 10^4 scaling and a thousands
    separator do not produce a spurious mismatch — and, equally, `4500` does not match a
    task that says `45000`.
    """
    if not args:
        return Decimal("1.0000")

    haystack = _fold(task_text)
    numbers = _numbers_in(task_text)

    matched = sum(1 for value in args.values() if _appears(value, haystack, numbers))
    return (Decimal(matched) / Decimal(len(args))).quantize(_QUANTUM)


# -- internals ------------------------------------------------------------------------


def _fold(text: str) -> str:
    """NFKC-normalize and case-fold, so comparison ignores width, form and case."""
    return unicodedata.normalize("NFKC", text).casefold()


def _numbers_in(text: str) -> frozenset[Decimal]:
    """Every number in the text, by value, with thousands separators removed."""
    found: set[Decimal] = set()
    for match in _NUMBER_RE.finditer(unicodedata.normalize("NFKC", text)):
        try:
            found.add(Decimal(match.group().replace(",", "")))
        except InvalidOperation:  # pragma: no cover - the regex cannot produce this
            continue
    return frozenset(found)


def _unscale(value: int) -> Decimal:
    return Decimal(value) / SCALE


def _plain(value: Decimal) -> str:
    """Format without scientific notation or trailing zeros — `45000`, not `4.5E+4`."""
    return format(value.normalize(), "f")


def _short_key(key: str) -> str:
    """`payment.amount` renders as `amount`; the namespace is noise to an embedding."""
    return key.rsplit(".", 1)[-1]


def _render_value(value: ArgValue) -> str:
    if isinstance(value, bool):  # pragma: no cover - the extractor never emits bools
        return str(value)
    if isinstance(value, int):
        return _plain(_unscale(value))
    if isinstance(value, Decimal):
        return _plain(value)
    return value


def _appears(value: ArgValue, haystack: str, numbers: frozenset[Decimal]) -> bool:
    """Whether one argument value shows up in the task text.

    Numeric args compare against the numbers found in the text, by value. String args
    compare as folded substrings — which is what keeps `"0012"` from matching a task
    that says `12`.
    """
    if isinstance(value, bool):  # pragma: no cover - the extractor never emits bools
        return False
    if isinstance(value, int):
        return _unscale(value) in numbers
    if isinstance(value, Decimal):
        return value in numbers
    folded = _fold(value)
    return bool(folded) and folded in haystack
