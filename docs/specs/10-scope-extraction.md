# Spec 10 — HTTP Scope and Argument Extraction

**Status:** accepted · **Ticket:** T-020 · **Implements:** `PLAN.md` §8, §9 T-020
**Depends on:** [`02-caveat-language.md`](02-caveat-language.md), [`09-decision-record.md`](09-decision-record.md)
**Consumed by:** T-020, T-021, T-022, T-042, T-051
**Resolves:** `02-caveat-language.md` §10 Q1 — *the `arg` path vocabulary and its extraction rules*

> Step 1 of the pipeline, and the only step that touches an untrusted wire format. Everything
> after it reasons about a `RequestContext`; this document is how an HTTP request becomes one.
>
> The interesting content is not the mapping table. It is §5: **the extractor and the upstream
> must agree on what the request says.** If they disagree, a caveat is enforced against a value
> that never executes, and every guarantee downstream is decoration. That failure is
> demonstrated in §5.1 and refused in §5.2.

---

## 1. What extraction produces

Exactly the four fields of `RequestContext` that come from the wire, plus the digest:

| Field | Source |
|---|---|
| `operation` | The **scope**, from the route mapping (§3) |
| `tool` | The **tool id**, from the route mapping (§3) |
| `args` | The extracted arguments, flat, scalar-valued (§4) |
| `arg_digest` | `hash_object(args)` over the canonical form (§7) |

`current_depth`, `request_intent` and `now` do **not** come from the request. Depth is computed
from the token's block count, intent is bound in the token, and `now` is the verifier's clock —
all three per `01-token-format.md` §7. **Nothing the agent sends may set them**, which is the
same rule ADR-005 settled for the token layer.

Extraction is a pure function of `(method, path, query string, headers, body bytes)` and a
configuration. It performs no I/O and reads no clock.

---

## 2. Fail closed, in both directions

Two failure modes, and they get opposite treatments because they mean opposite things.

| Situation | Outcome | Why |
|---|---|---|
| The route is **not mapped** | Deny, `MALFORMED_REQUEST` (configurable, deny by default) | An unmapped route is an unreviewed route. Allowing it means every new upstream endpoint ships unguarded |
| The request is **ambiguous** — §5 | Deny, `MALFORMED_REQUEST` | The PEP cannot determine what the upstream will act on, so it cannot authorize it |
| A mapped **argument is absent** | Extract nothing for that path; **do not deny** | `02-caveat-language.md` §3.2: an `arg` fact is legitimately optional, and `ArgPredicate` compiles to `reject if` so it is vacuous when absent |

The third row is the one that looks wrong and is not. An absent argument is not an ambiguous
one: the caveat language already decided that `arg` facts are optional and that a predicate over
an absent argument does not fire. Denying here would break `02-caveat-language.md` §3.2 and make
every `ArgPredicate` a *de facto* required-field check.

---

## 3. Route mapping

A list of rules, matched **in order, first match wins**. Order is explicit so that a specific
rule can precede a general one without a precedence algorithm to reason about.

```json
{
  "routes": [
    { "method": "POST", "path": "/payments",
      "scope": "payment:initiate", "tool": "payment_api",
      "args": { "payment.amount": "body.amount",
                "payment.to":     "body.recipient.account_id" } },

    { "method": "GET",  "path": "/invoices/{id}",
      "scope": "invoice:read", "tool": "invoice_api",
      "args": { "invoice.id": "path.id" } },

    { "method": "GET",  "path": "/invoices",
      "scope": "invoice:read", "tool": "invoice_api",
      "args": { "invoice.limit": "query.limit" } }
  ],
  "default": { "action": "deny" }
}
```

`action` is `deny` or `allow_unmapped`.

**JSON rather than YAML**, though YAML would read better. `pyyaml` is present in the lockfile
only as a transitive dependency of `uvicorn[standard]`, and depending on it directly is a new
direct dependency — which `ENGINEERING-RULES` requires deliberating rather than absorbing.
`json` is stdlib, the structure is identical, and the loader is three lines. The extractor's own
contract is a plain mapping, so a deployment that wants YAML can parse it itself and pass the
result in; nothing in this document depends on the file format.

**Path patterns** use Starlette's `compile_path`, which the gateway already depends on.
Measured: `/invoices/{id}` compiles to `^/invoices/(?P<id>[^/]+)$`, so a parameter never spans a
`/`; `{path:path}` compiles to `(?P<path>.*)` and does span. Both are permitted; `:path` is the
only converter that can match an empty or multi-segment value, so a rule using it must be
ordered last among rules sharing a prefix.

`method` matches case-sensitively against the uppercase HTTP method. `ANY` matches every method
and exists so a catch-all deny rule can be written explicitly.

The `scope` value is compared against the token's granted scopes as an opaque string. This
document does not constrain the scope vocabulary; `PLAN.md` §2 does.

---

## 4. The `arg` path vocabulary

An argument path in a caveat (`arg("payment.amount", $v)`) is an **opaque label chosen by the
policy author**. The mapping in §3 binds each label to a **source expression** naming where the
value comes from. Two different vocabularies, deliberately:

* The **caveat** side is stable, human-meaningful, and outlives any URL. `payment.amount` still
  means the same thing after the upstream renames its field.
* The **source** side is a wire detail and changes with the upstream.

Keeping them separate is what lets a token minted today survive an upstream refactor. It also
means a mapping change can silently alter what a caveat constrains — §8 requires the mapping
version in the decision record for exactly that reason.

### 4.1 Source expressions

| Form | Reads | Notes |
|---|---|---|
| `path.<name>` | A path parameter from the matched pattern | Percent-decoded — §5.3 |
| `query.<name>` | A query-string parameter | Refused if repeated — §5.2 |
| `body.<dotted.path>` | A field of a JSON object body | Refused if the path is ambiguous — §5.2 |
| `header.<name>` | A request header, lowercased name | Refused if repeated |

Any form may carry a **`:number` suffix** — `query.limit:number`, `body.amount:number` — which
declares that the value is numeric and must be scaled (§4.3). Without it, the value is a string.

The type is **declared, never guessed.** Inferring it from the text would mean an identifier
that happens to be all digits silently becomes a number: an `account_id` of `"0012"` would
extract as `12`, and a caveat comparing it as a string would never match the value the upstream
uses. Path, query and header values are always text on the wire, so there is nothing to infer
from; and a JSON body can legitimately carry an amount as either `25.5` or `"25.5"` depending on
the client's language. The policy author knows which fields are quantities. Nothing else does.

A `:number` source whose value will not parse as a finite decimal is a denial, not a fallback to
string — a caveat that expects to compare a quantity must not silently start comparing text.

`body.<dotted.path>` is **JSONPath-lite**: dot-separated object keys only. No wildcards, no
filters, no recursive descent, no array indexing.

That is a deliberate refusal to take a dependency. The full JSONPath grammar has no single
normative specification and its implementations disagree on exactly the cases an attacker
chooses; `ENGINEERING-RULES` requires deliberation before a new dependency, and an ambiguous
query language sitting between the attacker and the authorization decision is the wrong place to
inherit someone else's edge cases. Dotted keys are resolvable in a dozen lines with no
ambiguity. If array indexing is ever needed, it is added here first, with tests.

A key containing a literal `.` is unreachable by this grammar. That is accepted, and §9 records
it.

### 4.2 Value types

`RequestContext.args` is `dict[str, Decimal | int | str]` — flat and scalar. An extracted value
must be a JSON string, number, or boolean; **objects and arrays are not extracted**, and a
source expression resolving to one is a mapping error, denied as `MALFORMED_REQUEST`.

A `:number` source yields a scaled integer (§4.3). Every other source yields a `str`, NFC-
normalized; a JSON number read through a non-`:number` source becomes its exact decimal text.

Numbers are read as `Decimal` **from their original text**, never through `float`. `PLAN.md`
NFR-10 and rule 6 prohibit floats anywhere near money, and `json.loads` produces a float by
default — so the body is parsed with `parse_float=Decimal` *and* `parse_int=Decimal`, and no
extracted number passes through a binary float at any point.

### 4.3 Numeric scaling

`02-caveat-language.md` §4.6 requires numeric `arg` facts to be scaled by 10⁴ so that one
comparison rule covers every numeric term. The scaling rules:

| Input | Result |
|---|---|
| A number with ≤ 4 decimal places | Scaled to an exact integer |
| A number with > 4 decimal places | **Denied**, `MALFORMED_REQUEST` |
| `NaN`, `Infinity`, `-Infinity` | **Denied**, `MALFORMED_REQUEST` |
| Scientific notation (`1e3`) | Accepted; `Decimal` handles it exactly |

Measured: `Decimal("0.00005") * 10**4` is `0.5`, not an integer. Rounding it would mean the
enforced value is not the requested one, in the direction of whoever chose the rounding mode —
so it is refused instead. Four places is the domain everywhere else in this system (money is
`NUMERIC(20,4)`, `01-token-format.md` §3.1), and a payment specified to five is not a payment
this system can talk about.

`NaN` deserves its own line: every comparison against it is false, so a `reject if` predicate
over `NaN` never fires and the caveat silently passes. Refusing it is not tidiness.

---

## 5. Agreement with the upstream — the part that matters

### 5.1 The failure

The PEP authorizes a request and then forwards **the original bytes**. It does not rewrite the
query string or the body. So two parsers see the request: ours, and the upstream's. Where they
disagree about what the request *says*, the PEP enforces a caveat against a value that never
executes.

**Measured**, for the query string `amount=1&amount=999999`:

| Parser | Value of `amount` |
|---|---|
| `dict(starlette.datastructures.QueryParams(...))` | `999999` — **last** |
| `urllib.parse.parse_qsl(...)[0]` | `1` — **first** |
| Go `net/http` `Form.Get`, Java `getParameter` | **first** |
| PHP | **last** |

So a caveat `amount <= 5000` checked against `1` passes, and a Go upstream executes `999999`.
Nothing downstream can detect this: the token was valid, the caveat was satisfied, the decision
record is honest about what *it* saw. The same applies to duplicate JSON object keys —
`json.loads('{"amount": 1, "amount": 999999}')` yields `999999`, silently.

This is TM-26, and it is the same shape as TM-24: a string that means one thing where it is
checked and another where it is used.

### 5.2 The refusal

**The extractor never picks a winner.** Where a source expression could resolve to more than one
value, the request is denied with `MALFORMED_REQUEST`.

* A **query parameter** named by any mapping rule that appears more than once → deny.
* A **JSON object** containing the same key twice, anywhere along a mapped `body.` path → deny.
  Duplicates are detectable: `json.loads(..., object_pairs_hook=...)` sees every pair before the
  dict collapses them, measured.
* A **header** named by any mapping rule that appears more than once → deny.

Choosing first-wins or last-wins would be choosing a parser to agree with, and the upstream is
not ours to choose. Refusing is the only option that is correct against every upstream, and a
legitimate client does not send `amount` twice.

The check applies **only to mapped names**. A repeated query parameter nobody constrains is not
ambiguous in any way that matters, and denying it would break ordinary clients for nothing.

### 5.3 Normalization

Two more places where the two parsers can disagree, both settled by making our view match what
the upstream will do:

* **Percent-encoding.** Measured: `compile_path` matching `/invoices/a%2Fb` yields the raw
  `a%2Fb`. The upstream will decode it to `a/b`. Path and query values are therefore
  percent-decoded before use, so a caveat compares the string the upstream acts on.
* **Unicode.** String values are NFC-normalized, the same rule `hashing.py` already applies, so
  two visually identical arguments cannot produce two different digests or two different caveat
  outcomes.

Decoding is applied **once**. A value that is still percent-encoded after one decode
(`a%252Fb` → `a%2Fb`) is passed through as-is rather than decoded again: repeated decoding is
its own smuggling primitive, and one pass is what a conforming upstream does.

---

## 6. Body handling, and what it costs

Extraction from `body.` paths requires the body, and reading it conflicts with something T-018
promised.

**Measured:** `Request.json()` buffers the whole body; a subsequent `request.stream()` replays
it from Starlette's cache and yields the original bytes exactly. The reverse order fails —
`stream()` then `json()` raises `RuntimeError: Stream consumed`. So extraction must happen
**before** forwarding, and it holds the body in memory while it does.

T-018's docstring says *"bodies stream in both directions. Nothing is buffered, so a large
upload or a slow event stream costs the PEP a constant amount of memory."* That remains true for
routes with no `body.` mapping, and becomes false for routes that have one. The honest statement
is now conditional, and ADR-023 records the change rather than leaving T-018's claim standing.

The bound: a route with a `body.` mapping reads at most `max_extract_body_bytes` (default
1 MiB). A larger body on such a route is denied with `MALFORMED_REQUEST` rather than buffered —
an unbounded read here would be TM-14 (control-plane denial of service) reintroduced at the
gateway. Routes without a `body.` mapping stream unchanged and are not subject to the cap.

A body that is not valid JSON, or whose `content-type` is not a JSON media type, yields no
`body.` values. It is **not** denied — see §2's third row; a form-encoded upload to a route whose
caveats do not constrain its body is a legitimate request.

---

## 7. `arg_digest`

`arg_digest = hash_object(args)`, the canonical JSON hash from `hashing.py`, computed over the
extracted `args` map **after** normalization and scaling.

The digest is over the extracted arguments, not the raw body. Two consequences worth stating:

* It is **stable** against anything the mapping does not read — reordered JSON keys, whitespace,
  unmapped fields. Two requests that this system authorizes identically digest identically.
* It is **not** a body integrity check. It cannot be, because the PEP authorizes what it
  extracted, and that is the honest thing to record. `PLAN.md` NFR-5 requires the digest instead
  of the arguments so that a decision record carries no PII; it is a correlation handle, not
  evidence of what the upstream received.

---

## 8. What goes in the decision record

`mapping_version` — a hash of the loaded route configuration — is recorded on every decision.

A mapping change can silently alter what an existing caveat constrains: repointing
`payment.amount` from `body.amount` to `body.total` changes the meaning of every token in
circulation carrying a predicate on that path, without any token changing. Recording the version
is what makes that visible afterwards. It is the same reasoning as `policy_version`.

---

## 9. Known limitations

Stated rather than discovered later.

| # | Limitation | Bound |
|---|---|---|
| 1 | A JSON key containing a literal `.` is unreachable | The mapping fails to resolve, so the arg is absent, so predicates over it are vacuous. Fails **open** for that one path — the only such case in this document, and it requires the upstream to use dotted keys |
| 2 | No array indexing | A caveat cannot constrain `items[0].amount`. Deliberate (§4.1); revisited when a demo workflow needs it |
| 3 | The digest is over extracted args, not the body | §7. A field nobody mapped is not in the digest and not authorized |
| 4 | Ambiguity refusal covers mapped names only | An unmapped repeated parameter is forwarded as sent. Correct — nothing constrains it — but it means the PEP is not a general request sanitizer |
| 5 | `max_extract_body_bytes` is a per-request cap, not a concurrent-memory bound | `max_connections` (100) × 1 MiB is the worst case. Recorded for T-053's load profile |

---

## 10. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Whether a repeated *unmapped* parameter should be denied when the route's token carries any `ArgPredicate` at all — stricter, and defensible | T-051 |
| 2 | Whether `mapping_version` should be signed like a policy bundle (T-025), so a mapping cannot be swapped without detection | T-025 |
