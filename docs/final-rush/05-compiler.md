# 05 — The NL→Cedar compiler, local-first

**The problem in one line.** The headline 27/30 (90%) was measured on hosted
`gemini-flash-lite-latest` (resolved to `gemini-3.5-flash-lite`), n = 30, against a dataset the
project wrote. The local number (43%) was inflated by dataset prompts pasted into the system
prompt and must not be cited. There is no template fallback (T-031), so with no internet and no
model the compiler simply fails. And activation never reached the PEP (fixed by
[`01-policy.md`](01-policy.md)).

**Decision already made (2026-10-16).** Local inference is the default; hosted inference is an
optional backend. Both must be measured, and the default must work on a 6 GB laptop GPU.

**Rules that stay in force** (from the compiler's history; read before touching
`nl_compiler/`):
- `evaluate_compiler.py --validate` must score 30/30 (or N/N) with no model before any
  measurement is worth running.
- Never put dataset prompts in `_SYSTEM_PROMPT`.
  `test_the_prompt_does_not_contain_any_evaluation_prompt` enforces this.
- Never log the operator's sentence verbatim; digest and length only.
- Iterating on the dataset is free; measuring costs GPU time or quota.

---

### T-031 — Template fallback for the NL compiler

**Size** M · **Lane** compiler · **GPU** — · **J**

**Why.** Deferred since M6; `PLAN.md` calls it "demo insurance — the compiler must never hang on
stage". Failure drill F-2 has had no implementation the whole time.

**Read.** `docs/PLAN.md` T-031; `nl_compiler/compiler.py`; `nl_compiler/dataset.json`;
`docs/DEMO.md` F-1, F-2; `tests/chaos/test_ch08_ollama_down.py` (it currently asserts the
fallback's absence, and must flip).

**Probe first.** Classify the 30 dataset cases by policy shape by hand (role restriction, amount
ceiling, time window, approver requirement, scope ban, and so on) and count how many shapes
cover how many cases. Pick the ten shapes that cover the most.

**Deliverables.**
- `nl_compiler/templates.py`: ten parameterized policy shapes, each a Cedar template plus a
  deterministic extractor for its slots (role names, amounts with currency, tools, times).
  Extraction by explicit patterns, no model.
- Fallback order: model → (on failure, timeout, unparseable output, or confidence below a
  threshold) templates → (no template matches) a clarifying question listing the supported
  shapes.
- The result panel says which path produced the policy ("compiled by local model" /
  "matched template: amount ceiling").
- Template output goes through the same parse, auto-test and corpus gate as model output.
- `test_ch08_ollama_down.py` updated: with Ollama down, the compiler answers from templates.

**Acceptance criteria.**
- [ ] With no model reachable, every dataset case whose shape is covered compiles, parses and
      passes its expectations (report the count).
- [ ] An uncovered sentence produces a clarifying question, never a guess.
- [ ] Response time with the model down < 1 s.
- [ ] `docs/DEMO.md` F-2 rewritten to the real behaviour.

---

### T-097 — Local model bake-off for 6 GB GPUs, with constrained decoding

**Size** M · **Lane** compiler · **GPU** 6 · **J**

**Why.** The local default must be chosen by measurement on the team's actual hardware, and its
number must be honest (no prompt leakage). The reference agent (T-086) needs a tool-calling model
too; one model for both would simplify the demo laptop.

**Read.** `nl_compiler/llm.py`, `ollama_client.py`; `scripts/evaluate_compiler.py`; ADR-033,
ADR-038, ADR-040; `docs/STATUS.md` gap 19a.

**Probe first.** Run `evaluate_compiler.py --validate` and confirm 30/30. Confirm the system prompt
contains no dataset prompt. Record the Ollama version and the GPU's free VRAM with nothing loaded.

**Deliverables.**
- A bake-off of at least four models that fit in 6 GB at 4-bit, for example
  `qwen2.5:7b-instruct-q4_0`, `qwen2.5-coder:7b` at Q4, `llama3.1:8b` at Q4, and a 3B model
  for the 4 GB card. Pull exact tags and record them.
- For each: with and without JSON-schema constrained output (Ollama `format`), report passed,
  wrong, unparseable, asked-for-clarification, errors, latency median and p95, VRAM, cold-start
  time. Three runs each; report variance.
- The same models scored on a 20-call tool-calling check for T-086 (malformed-call rate).
- `docs/benchmarks/compiler.md` generated from the run, with a `--check` mode.
- The chosen default set in `llm.py` with an ADR; the hosted figure kept beside it, labelled.

**Acceptance criteria.**
- [ ] Every figure states the model tag, quantization, Ollama version, hardware and run count.
- [ ] The chosen model runs on the 6 GB laptop with the console, Postgres, Redis and the PEP all
      up at the same time (VRAM headroom recorded).
- [ ] `STATUS.md` gap 19a updated with the local figure; the inflated 43% marked "do not cite".

---

### T-098 — Compiler dataset 30 → 150 with a held-out split

**Size** M · **Lane** compiler · **GPU** 6 · —

**Why.** n = 30, written by the same people who tuned the prompt, is a weak instrument
(`research/gap-analysis.md` G-5). A larger set with a held-out split that nobody looks at during
prompt work makes the number defensible.

**Read.** `nl_compiler/dataset.json` and its validation logic; `research/related-work.md` on
LACE, NLAC and U-XACML (how they evaluate).

**Depends on.** T-097.

**Deliverables.**
- 150 cases covering every policy shape in the entity model, including deliberately ambiguous
  sentences whose correct output is a clarifying question.
- A fixed **held-out** split of 50, stored separately, used only for the final number; the
  evaluation script refuses to print held-out results without a `--final` flag.
- Every case self-validating (`--validate` scores the reference policy with no model).
- Results with Wilson 95% confidence intervals, local and hosted.

**Acceptance criteria.**
- [ ] `--validate` 150/150.
- [ ] Held-out results reported once, with the model and prompt hash frozen beforehand and the
      hash recorded in the results file.

---

### T-099 — English sentence enforced on the next PEP call, end to end

**Size** M · **Lane** compiler / policy · **GPU** — · **J**

**Why.** The claim on stage is "the judge writes a policy in English, and the very next call is
governed by it". After T-072 the plumbing exists; this ticket proves the full path and makes it
fast enough to show.

**Read.** T-071 API; T-072; `nl_compiler/compiler.py`; `console/templates/nl_results.html`.

**Depends on.** T-072, T-031.

**Deliverables.**
- The compile → test → diff → activate flow in the console publishes through T-071, with
  provenance `compiled` and the sentence's digest.
- The result panel shows the new serial and, within a second or two, "acknowledged by N/N PEPs"
  (from T-073).
- An e2e test: compile "Only senior agents may initiate payments over 1,000" (template path,
  so CI needs no model) → activate → a non-senior payer's next payment of 1,500 is denied with
  `POLICY_DENIED` naming the new policy, and 500 still succeeds.
- `docs/DEMO.md` beat 5 and `research/gap-analysis.md` beat 5 updated.

**Acceptance criteria.**
- [ ] The e2e test passes in CI with no model and no internet.
- [ ] Time from "Activate" to enforcement at the PEP measured and shown (target < 2 s).
- [ ] A-26 (an injection aimed at the compiler) re-run through the full path: the gate still
      refuses a policy that fails the corpus.
