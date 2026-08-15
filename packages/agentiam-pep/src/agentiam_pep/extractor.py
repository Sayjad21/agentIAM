"""HTTP scope and argument extraction — step 1 of the pipeline.

Implements [`docs/specs/10-scope-extraction.md`](../../../../docs/specs/10-scope-extraction.md).

This is the only step that reads an untrusted wire format. Everything after it reasons about
a `RequestContext`; this module is how an HTTP request becomes one.

**The load-bearing part is ambiguity refusal (TM-26).** The gateway forwards the original
bytes, so two parsers read every request: ours and the upstream's. Measured for
`amount=1&amount=999999`, Starlette's `dict(QueryParams)` yields `999999` while Go's
`Form.Get` and Java's `getParameter` yield `1` — so a caveat `amount <= 5000` can be checked
against one value while the upstream executes the other, and nothing downstream can tell.
Picking a winner would mean picking an upstream to agree with. This module refuses instead.

Purity: no network, no database, no clock. `extract()` is a pure function of the request
bytes and a `RouteTable`. It lives in `agentiam-pep` rather than `agentiam-core` because it
depends on Starlette's path compiler, and core admits no such dependency.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from collections.abc import Mapping  # runtime: used in isinstance checks below
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from starlette.routing import compile_path

from agentiam_core.errors import ReasonCode
from agentiam_core.hashing import hash_object
from agentiam_core.models import DriftMode

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

#: Numeric `arg` facts are scaled by 10⁴, exactly like budgets, so one Datalog comparison rule
#: covers every numeric term in the language (spec 02 §4.6).
SCALE: Final = 10**4

#: Decimal places the scaling admits. A value finer than this is refused rather than rounded —
#: rounding would enforce a number the caller did not request (spec 10 §4.3).
PLACES: Final = 4

#: Default cap on a body read for extraction. Only applies to routes that map a `body.` source;
#: an unbounded read here would reintroduce TM-14 at the gateway (spec 10 §6).
DEFAULT_MAX_EXTRACT_BODY_BYTES: Final = 1 << 20

_SOURCE_KINDS: Final = frozenset({"path", "query", "body", "header"})
_NUMBER_SUFFIX: Final = ":number"

#: What `RequestContext.args` admits.
ArgValue = Decimal | int | str


class ExtractionError(Exception):
    """Extraction failed, with the reason code the decision record will carry.

    Every raise is a deny. There is no partial extraction: a request the PEP cannot read
    unambiguously is a request it cannot authorize.
    """

    def __init__(self, reason: ReasonCode, detail: str) -> None:
        """Carry the reason code alongside the message."""
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class _Object(dict[str, Any]):
    """A JSON object that remembers which of its keys arrived more than once.

    `json.loads` collapses duplicates silently — measured, `{"amount": 1, "amount": 999999}`
    parses to `{'amount': 999999}`. `object_pairs_hook` sees both pairs before the collapse,
    which is the only reason TM-26's refusal is implementable at all.
    """

    __slots__ = ("duplicates",)

    duplicates: frozenset[str]


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> _Object:
    seen: dict[str, Any] = {}
    duplicates: set[str] = set()
    for key, value in pairs:
        if key in seen:
            duplicates.add(key)
        seen[key] = value
    obj = _Object(seen)
    obj.duplicates = frozenset(duplicates)
    return obj


@dataclass(frozen=True, slots=True)
class Source:
    """Where one argument comes from, and whether it is a quantity.

    `numeric` is declared in the mapping, never inferred from the text. Inferring it would
    turn an `account_id` of `"0012"` into `12` and quietly stop a string caveat from matching
    the value the upstream uses (spec 10 §4.1).
    """

    kind: str
    keys: tuple[str, ...]
    numeric: bool

    @property
    def name(self) -> str:
        """The single parameter name, for every kind but `body`."""
        return self.keys[0]


def _parse_source(expression: str) -> Source:
    numeric = expression.endswith(_NUMBER_SUFFIX)
    body = expression[: -len(_NUMBER_SUFFIX)] if numeric else expression

    kind, _, rest = body.partition(".")
    if kind not in _SOURCE_KINDS:
        raise ValueError(
            f"unknown source kind {kind!r} in {expression!r}; "
            f"expected one of {sorted(_SOURCE_KINDS)}"
        )
    if not rest:
        raise ValueError(f"source {expression!r} names no field")

    if kind == "body":
        keys = tuple(rest.split("."))
        if any(not key for key in keys):
            raise ValueError(f"body path {rest!r} has an empty segment")
    elif kind == "header":
        keys = (rest.lower(),)
    else:
        keys = (rest,)
        if "." in rest:
            raise ValueError(f"{kind} source {rest!r} must name a single parameter")

    return Source(kind=kind, keys=keys, numeric=numeric)


@dataclass(frozen=True, slots=True)
class RouteRule:
    """One (method, path) → (scope, tool, arguments) mapping."""

    method: str
    path: str
    scope: str
    tool: str
    sources: tuple[tuple[str, Source], ...]
    regex: re.Pattern[str]
    drift_mode: DriftMode = DriftMode.STRICT

    @property
    def needs_body(self) -> bool:
        """Whether extraction must read the body — the only case that buffers it."""
        return any(source.kind == "body" for _, source in self.sources)

    def matches(self, method: str, path: str) -> re.Match[str] | None:
        """Return the path match if this rule applies, else None."""
        if self.method != "ANY" and self.method != method:
            return None
        return self.regex.match(path)


@dataclass(frozen=True, slots=True)
class Extraction:
    """What step 1 hands to step 2."""

    scope: str
    tool: str
    args: dict[str, ArgValue]
    arg_digest: str
    drift_mode: DriftMode = DriftMode.STRICT


@dataclass(frozen=True, slots=True)
class RouteTable:
    """The loaded mapping. Built once at startup; `extract()` only reads it."""

    rules: tuple[RouteRule, ...]
    deny_unmapped: bool
    max_extract_body_bytes: int
    mapping_version: str

    @classmethod
    def from_file(cls, path: Path) -> RouteTable:
        """Load a route table from a JSON file.

        JSON rather than YAML because `pyyaml` is in the lockfile only as a transitive
        dependency of `uvicorn[standard]`, and depending on it directly is a new direct
        dependency — which `ENGINEERING-RULES` requires deliberating rather than absorbing
        (spec 10 §3). A deployment that prefers YAML can parse it and call `from_config`.

        Raises:
            ValueError: If the file is not valid JSON, or any rule is malformed.
        """
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(config, Mapping):
            raise ValueError(f"{path} must contain a JSON object at the top level")
        return cls.from_config(config)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> RouteTable:
        """Validate and compile a route configuration.

        Every error a mapping can contain is raised here rather than on the first request
        that touches the broken rule — a misconfigured route that denies at 3am is a much
        worse way to learn about a typo.

        Raises:
            ValueError: If any rule is malformed.
        """
        raw_rules = config.get("routes", [])
        if not isinstance(raw_rules, list):
            raise ValueError("`routes` must be a list")

        rules: list[RouteRule] = []
        for index, raw in enumerate(raw_rules):
            rules.append(cls._compile_rule(index, raw))

        default = config.get("default", {})
        action = default.get("action", "deny") if isinstance(default, Mapping) else "deny"
        if action not in {"deny", "allow_unmapped"}:
            raise ValueError(f"default.action must be 'deny' or 'allow_unmapped', got {action!r}")

        max_body = config.get("max_extract_body_bytes", DEFAULT_MAX_EXTRACT_BODY_BYTES)
        if not isinstance(max_body, int) or isinstance(max_body, bool) or max_body <= 0:
            raise ValueError("max_extract_body_bytes must be a positive integer")

        return cls(
            rules=tuple(rules),
            deny_unmapped=action == "deny",
            max_extract_body_bytes=max_body,
            mapping_version=_mapping_version(rules, action, max_body),
        )

    @staticmethod
    def _compile_rule(index: int, raw: object) -> RouteRule:
        where = f"routes[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{where} must be a mapping")

        method = raw.get("method", "")
        path = raw.get("path", "")
        scope = raw.get("scope", "")
        tool = raw.get("tool", "")
        raw_drift = raw.get("drift_mode", "strict")
        try:
            drift_mode = DriftMode(raw_drift)
        except ValueError as exc:
            raise ValueError(f"{where}.drift_mode must be one of off, log_only, strict") from exc

        for field, value in (("method", method), ("path", path), ("scope", scope), ("tool", tool)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{where}.{field} must be a non-empty string")
        assert isinstance(method, str)  # noqa: S101 - narrowing for mypy after the loop
        assert isinstance(path, str)  # noqa: S101
        assert isinstance(scope, str)  # noqa: S101
        assert isinstance(tool, str)  # noqa: S101

        if not path.startswith("/"):
            raise ValueError(f"{where}.path must start with '/'")

        regex, _format, converters = compile_path(path)
        param_names = frozenset(converters)

        raw_args = raw.get("args", {})
        if not isinstance(raw_args, Mapping):
            raise ValueError(f"{where}.args must be a mapping")

        sources: list[tuple[str, Source]] = []
        for label, expression in raw_args.items():
            if not isinstance(label, str) or not label:
                raise ValueError(f"{where}.args has a non-string label")
            if not isinstance(expression, str):
                raise ValueError(f"{where}.args[{label!r}] must be a string")
            try:
                source = _parse_source(expression)
            except ValueError as exc:
                raise ValueError(f"{where}.args[{label!r}]: {exc}") from exc
            if source.kind == "path" and source.name not in param_names:
                raise ValueError(
                    f"{where}.args[{label!r}] reads path parameter {source.name!r}, "
                    f"which {path!r} does not define (defines {sorted(param_names)})"
                )
            sources.append((label, source))

        return RouteRule(
            method=method.upper(),
            path=path,
            scope=scope,
            tool=tool,
            sources=tuple(sources),
            regex=regex,
            drift_mode=drift_mode,
        )


def _mapping_version(rules: Sequence[RouteRule], action: str, max_body: int) -> str:
    """A hash of the loaded mapping, recorded on every decision.

    Repointing `payment.amount` from `body.amount` to `body.total` changes what every token in
    circulation constrains, without any token changing. This is what makes that visible after
    the fact — the same reasoning as `policy_version` (spec 10 §8).
    """
    shape = {
        "default": action,
        "max_extract_body_bytes": max_body,
        "routes": [
            {
                "method": rule.method,
                "path": rule.path,
                "scope": rule.scope,
                "tool": rule.tool,
                "args": {
                    label: f"{s.kind}.{'.'.join(s.keys)}{_NUMBER_SUFFIX if s.numeric else ''}"
                    for label, s in sorted(rule.sources)
                },
            }
            for rule in rules
        ],
    }
    return hash_object(shape)


def _normalize(text: str) -> str:
    """NFC, so two visually identical arguments cannot digest differently."""
    return unicodedata.normalize("NFC", text)


def _decode_once(text: str) -> str:
    """Percent-decode exactly one pass.

    `compile_path` leaves a matched parameter encoded — measured, `/invoices/a%2Fb` yields
    `a%2Fb` — while the upstream will read `a/b`. One pass makes our view match. Repeated
    decoding is its own smuggling primitive, so `a%252Fb` stays `a%2Fb` (spec 10 §5.3).
    """
    return urllib.parse.unquote(text)


def _to_scaled(value: object, label: str) -> int:
    """Render a declared-numeric value as a scaled integer, or refuse it."""
    if isinstance(value, bool):
        raise ExtractionError(
            ReasonCode.MALFORMED_REQUEST, f"argument {label!r} is a boolean, not a quantity"
        )
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ExtractionError(
            ReasonCode.MALFORMED_REQUEST, f"argument {label!r} is not a number"
        ) from exc

    if not number.is_finite():
        # Every comparison against NaN is false, so a `reject if` predicate over it never
        # fires and the caveat silently passes. Refusing is not tidiness.
        raise ExtractionError(
            ReasonCode.MALFORMED_REQUEST, f"argument {label!r} is not a finite number"
        )

    scaled = number * SCALE
    if scaled != scaled.to_integral_value():
        raise ExtractionError(
            ReasonCode.MALFORMED_REQUEST,
            f"argument {label!r} has more than {PLACES} decimal places",
        )
    return int(scaled)


def _to_text(value: object, label: str) -> str:
    """Render a value read through a non-numeric source."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return _normalize(value)
    raise ExtractionError(
        ReasonCode.MALFORMED_REQUEST,
        f"argument {label!r} resolves to {type(value).__name__}, which is not a scalar",
    )


def _collect_query(query_string: str) -> dict[str, list[str]]:
    """Parse the query string keeping *every* value, so repetition stays visible."""
    collected: dict[str, list[str]] = {}
    for key, value in urllib.parse.parse_qsl(query_string, keep_blank_values=True):
        collected.setdefault(key, []).append(value)
    return collected


def _collect_headers(headers: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {}
    for name, value in headers:
        collected.setdefault(name.lower(), []).append(value)
    return collected


def _one_of(collected: Mapping[str, list[str]], name: str, label: str, what: str) -> str | None:
    """Return the single value for `name`, or refuse if there is more than one (TM-26)."""
    values = collected.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise ExtractionError(
            ReasonCode.MALFORMED_REQUEST,
            f"{what} {name!r} appears {len(values)} times and is constrained by "
            f"argument {label!r}; the upstream would choose one and this gateway cannot "
            f"know which",
        )
    return values[0]


def _resolve_body(root: object, keys: tuple[str, ...], label: str) -> object | None:
    """Walk a dotted path through a parsed JSON object, refusing ambiguity on the way."""
    current = root
    for key in keys:
        if not isinstance(current, dict):
            return None
        if isinstance(current, _Object) and key in current.duplicates:
            raise ExtractionError(
                ReasonCode.MALFORMED_REQUEST,
                f"JSON key {key!r} appears more than once on the path to argument "
                f"{label!r}; the upstream would choose one and this gateway cannot know which",
            )
        if key not in current:
            return None
        current = current[key]
    return current


def _parse_body(body: bytes | None) -> object | None:
    """Parse a JSON body, or return None if it is absent or not JSON.

    Not a denial: spec 10 §2 keeps `arg` facts optional, and a form-encoded upload to a route
    whose caveats do not constrain its body is a legitimate request.

    `parse_float` and `parse_int` are both `Decimal` so that no extracted number ever passes
    through a binary float — rule 6, and the reason `0.1 + 0.2` is not this system's problem.
    """
    if not body:
        return None
    try:
        parsed: object = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_pairs_hook,
            parse_float=Decimal,
            parse_int=Decimal,
        )
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed


def extract(
    table: RouteTable,
    *,
    method: str,
    path: str,
    query_string: str = "",
    headers: Iterable[tuple[str, str]] = (),
    body: bytes | None = None,
) -> Extraction:
    """Turn an HTTP request into the scope, tool and arguments the pipeline needs.

    Args:
        table: The loaded route mapping.
        method: The HTTP method, uppercase.
        path: The request path, with the proxy prefix already stripped.
        query_string: The raw query string, without a leading `?`.
        headers: Request headers as (name, value) pairs; names may be any case.
        body: The request body, or None. Only read for routes mapping a `body.` source.

    Returns:
        The extracted scope, tool, arguments and digest.

    Raises:
        ExtractionError: If the route is unmapped and the default is deny, if the request is
            ambiguous (TM-26), or if a declared-numeric argument will not scale.
    """
    matched: tuple[RouteRule, re.Match[str]] | None = None
    for rule in table.rules:
        found = rule.matches(method.upper(), path)
        if found is not None:
            matched = (rule, found)
            break

    if matched is None:
        if table.deny_unmapped:
            raise ExtractionError(
                ReasonCode.MALFORMED_REQUEST,
                f"no route mapping for {method.upper()} {path}; an unmapped route is an "
                f"unreviewed route",
            )
        return Extraction(scope="", tool="", args={}, arg_digest=hash_object({}))

    rule, match = matched
    path_params = {k: _normalize(_decode_once(v)) for k, v in match.groupdict().items()}
    query = _collect_query(query_string)
    header_map = _collect_headers(headers)

    parsed_body: object | None = None
    if rule.needs_body and body is not None:
        if len(body) > table.max_extract_body_bytes:
            raise ExtractionError(
                ReasonCode.MALFORMED_REQUEST,
                f"body is {len(body)} bytes, over the {table.max_extract_body_bytes} byte "
                f"extraction limit for a route that constrains its body",
            )
        parsed_body = _parse_body(body)

    args: dict[str, ArgValue] = {}
    for label, source in rule.sources:
        raw: object | None
        if source.kind == "path":
            raw = path_params.get(source.name)
        elif source.kind == "query":
            value = _one_of(query, source.name, label, "query parameter")
            raw = _normalize(value) if value is not None else None
        elif source.kind == "header":
            value = _one_of(header_map, source.name, label, "header")
            raw = _normalize(value) if value is not None else None
        else:
            raw = _resolve_body(parsed_body, source.keys, label)

        if raw is None:
            # Absent, not ambiguous. `ArgPredicate` compiles to `reject if`, so a predicate
            # over an absent argument is vacuous (spec 02 §3.2). Denying here would make
            # every argument caveat a required-field check.
            continue

        args[label] = _to_scaled(raw, label) if source.numeric else _to_text(raw, label)

    return Extraction(
        scope=rule.scope,
        tool=rule.tool,
        args=args,
        arg_digest=hash_object(args),
        drift_mode=rule.drift_mode,
    )
