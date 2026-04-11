# ADR 0001: Code-Reading Capability for Argus Review

**Status**: Accepted  
**Date**: 2026-04-12  
**Issue**: 392fyc/Argus#9  

---

## Context

Argus reviews PRs using the diff and PR metadata. `patch_suggestion_format.py` already
drops findings whose `relevant_file` / `end_line` cannot be mapped into the diff, but a
narrower false-positive pattern remains: Argus sometimes suggests mitigations referencing
file paths or function names that are not part of the change (e.g., "add error handling
in `utils/helpers.py`" when that file is untouched). This erodes reviewer trust over time.

Four options were considered for extending Argus's awareness beyond the diff.

---

## Decision Drivers

- Low operational overhead (no new infrastructure)
- Minimal latency impact on the review path
- Implementable without restructuring the existing monkey-patch pipeline
- False-negative rate for suppression should be acceptable (better to show an uncertain finding than suppress a valid one)

---

## Options Evaluated

### Option 1 — File existence check
Verify that any file referenced in a finding exists in the repository.

- **Pro**: Simple, zero latency overhead.
- **Con**: Does not validate whether the referenced file is relevant to the change.
  A file can exist in the repo but have nothing to do with the diff.

### Option 2 — Diff-scope intersection ✅ (chosen)
Check whether referenced identifiers (file paths, function/class names extracted from
finding text) appear in the PR diff's changed lines (`+` prefix). Suppress or downgrade
findings where no referenced identifier has any intersection with the diff.

**Scope as implemented**: intersection is computed against `+` diff lines only (not
import graphs or transitive call sites). This is a deliberate narrowing from the broader
"files directly touched" framing — import-graph traversal belongs to a future iteration
once the baseline false-positive rate is measured.

- **Pro**: Catches the primary failure mode without embedding or sub-calls. ~100–150 lines
  in a new `argus_code_filter.py` module, called once per review event before the results
  are assembled. Keeps the change isolated from the existing monkey-patch chain.
- **Con**: Common identifiers (e.g. `e`, `data`, `result`) can produce false negatives
  (unrelated findings pass the intersection test). Identifier extraction is heuristic,
  not AST-based. Does not detect cross-file side-effects.

### Option 3 — Embedding similarity search
Embed all findings and retrieve top-k relevant code chunks via vector search.

- **Pro**: Higher recall for semantically related code.
- **Con**: Requires a persistent vector store and an embedding model call per review.
  Adds latency on a per-finding basis. Disproportionate operational overhead for a
  post-hoc validation step on a single-container deployment. Out of scope for M1.x.

### Option 4 — LLM sub-call with full file context
For each finding, fetch the full file and ask a sub-LLM to validate relevance.

- **Pro**: Higher per-finding precision than heuristic intersection.
- **Con**: Multiplies token cost and latency per review. Introduces a new failure cascade
  (sub-call errors propagate into the review result). Does not inherently handle multi-file
  relationships, which is the primary motivating problem.

---

## Decision

**Option 2: diff-scope intersection on `+` lines only.**

A new `argus_code_filter.py` module will:
1. Extract referenced identifiers from each finding's text (file-path regex + word-boundary
   heuristics for function/class names — no AST parsing).
2. Build a "touched identifier set" from `+` diff lines (changed file paths + tokens
   matching identifier patterns).
3. Suppress findings where the referenced identifier has zero overlap with the touched set.
4. Log suppressed findings: `[Argus] finding suppressed: no diff intersection`.

The filter is called as a standalone pass after findings are generated and before they are
assembled into the review body. It does **not** touch the inline comment assembly, footer
counts, or review body builder — those remain unchanged. The suppression list is passed as
a filtered finding set.

**Implementation estimate**: ~100–150 lines in `argus_code_filter.py` + ~5 lines of call
site wiring in `patch_suggestion_format.py`. Not ~50 lines — the extraction + set-build
logic is non-trivial.

---

## Consequences

**Positive**
- Reduces false-positive rate for findings referencing code unrelated to the change.
- Zero change to the review prompt, LLM call, or monkey-patch chain structure.
- Isolated in a new module — easy to disable if the heuristic proves too aggressive.

**Negative / Risks**
- **False negatives for suppression**: short or common identifiers (single letters, generic
  names) will match incidentally, allowing unrelated findings through. Acceptable at this
  stage; measure false-negative rate after first 10 reviews.
- **Identifier collision**: if a finding mentions `e` or `data`, intersection will always
  be non-empty. Mitigate by requiring minimum identifier length (≥4 chars) in the filter.
- **Maintainability**: the existing patch chain in `patch_suggestion_format.py` already
  layers `apply_patch()`, `patched_run()`, incremental filtering, deduplication, and
  doc-PR suppression. Adding another module increases cognitive load. Keep `argus_code_filter.py`
  self-contained with clear input/output types to limit coupling.

**Out of scope for this ADR**
- Import-graph traversal (transitive call sites)
- Embedding-based retrieval (Option 3)
- Sub-LLM validation (Option 4)
- AST-based identifier extraction

---

## References

- Issue: 392fyc/Argus#9
- M1 merged: PR #7 (narrow ESCALATE), PR #11 (nit-loop deadlock fix)  
- M2 next: 392fyc/Argus#5 (structured event schema)
- `patch_suggestion_format.py`: existing diff-mapping logic at lines 1194–1201
