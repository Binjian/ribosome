# Processing and Recursion Refactoring in `DOMClass.textualize`

## 1. Executive Summary

This document reviews the AST summarization pipeline triggered by `DOMClass.textualize` in `ribosome.core.dom.model` (line 534) and its underlying recursive worker `summarize_node_async` in `ribosome.core.dom.summary` (line 277).

The `textualize` method is designed to traverse the hierarchical AST produced by `reorg`, generate LLM summaries for nodes (paragraphs, images, tables, sections), populate each node's `"s"` field, synthesize a whole-document summary, and transition the document into the `semantics_json` state required for downstream vector embedding via `embed_doc`.

The original audit uncovered critical flaws in **state management**, **dual-tree parallel traversal**, **recursive parameter dropping**, **progress counting**, and **uncontrolled LLM call explosion on leaf tokens**. This document outlines the problems and the architectural refactoring to resolve them.

> **Review status (2026-10-08)**: Phases 1–6 are present in the current notebooks and generated modules. Sections 2–3 and 5–6 retain implementation history; they are not instructions to reapply those changes. The next implementation work is §7. Historical fixes do not establish complete action coverage, bounded task allocation, transactional publication, or repeatable live-model output. This review changes documentation only.

---

## 2. Comprehensive Problem Analysis

### 2.1. State Machine Disconnection: `semantics_json` Left Unpopulated
The standard `DOMClass` processing lifecycle follows:
$$\text{raw\_markdown} \xrightarrow{\text{setup}} \text{raw\_json} \xrightarrow{\text{reorg}} \text{ast\_json} \xrightarrow{\text{textualize}} \text{semantics\_json} \xrightarrow{\text{embed\_doc}} \text{embed\_json}$$

In the original implementation of `textualize` and its helper `_finalize_summary_ast_async`:
```python
serialized = self._dump_json(ast_dict)
self.ast_json = serialized  # <-- Overwrites ast_json
return serialized
```
- It assigned the summarized tree back to `self.ast_json`.
- It **never set `self.semantics_json`**, leaving it as `None`.
- It never wrote out `self.semantics_json_file`.
- Consequently, when downstream `embed_doc()` was called:
  ```python
  if not self.semantics_json:
      raise ValueError("semantics_json content is empty. Cannot embed the content.")
  ```
  It failed immediately. The document lifecycle was fundamentally broken.

### 2.2. Brittle Dual-Tree Parallel Traversal with `jsoncfg`
`textualize` loaded two trees:
1. `config_ast = jsoncfg.load_config(str(self.ast_json_file))` (disk-backed `ConfigNode` tree)
2. `ast_dict = json.loads(self.ast_json)` (in-memory standard Python dict)

It then performed lockstep parallel recursion in `summary_node_pair_async`:
```python
for (_, cvalue), (key, value) in zip(config_node, node.items()):
```
This had severe vulnerabilities:
- **Positional Key Pairing**: `zip(config_node, node.items())` assumes identical dictionary key insertion order. If any key was added, removed, or ordered differently (such as `"s"` added during prior runs, or `"subnodes"`), the two iterators lose synchronization and pair mismatched AST structures.
- **Memory vs. Disk Drift**: If `self.ast_json` in memory was modified without saving to `self.ast_json_file`, the parallel trees diverged in shape, leading to type assertion errors during traversal.
- **Extreme Fragility for a Minor Logging Feature**: The entire `jsoncfg` dependency throughout the recursion was used exclusively to retrieve `jsoncfg.node_location(config_node).line` for log printouts.

### 2.3. Parameter Dropping in `textualize` and During Recursion
- In `textualize`:
  - `action = self._require_action(action)` resolved `action`, but **never passed it** to `summary_node_pair_async`.
  - `self.leaf_min_len`, `self.min_len`, and `self.lang` configured on the `DOMClass` instance were completely omitted, causing `summary_node_pair_async` to default to `leaf_min_len=0, min_len=None, lang='zh'`.
- In `summary_node_pair_async`:
  - Even if `action` was passed to the root call, **none of the recursive calls** (lines 329, 336, 358) passed `action=action`. The transform was applied once at the root block list and dropped for all child nodes.

### 2.4. Immutable Progress Counters
`table_count` and `section_count` were passed as Python `int` primitives by value. Inside `_record_summary_progress`:
```python
if node_type == "Table":
    table_count += 1
elif node_type == "Section":
    section_count += 1
```
Because integers are immutable in Python, modifying the local parameter did not update the caller's scope, the parent recursion levels, or `self`. Every table was logged as table 1, and every section was logged as section 1.

### 2.5. Recursive LLM Call Explosion on Leaf Nodes
Because `textualize` did not pass `leaf_min_len`, it defaulted to `0`. At line 366:
```python
elif isinstance(node, (str, int, float, bool)):
    node = await _summarize_leaf_async(str(node), leaf_min_len=leaf_min_len, ...)
```
With `threshold = 0`, every leaf text token (individual words in `Str` nodes) triggered an LLM call. These were then repeatedly re-summarized at the inline list, paragraph, content, section, and document levels, resulting in hundreds of redundant LLM calls.

---

## 3. Architecture & Refactoring Solution

> **Status**: the code below is the shape landed by the original refactoring plus Phases 1–4 of §6. Phase 6 then removed the dual-tree `jsoncfg` walk described in §3.3 and renamed the worker, so `textualize` now calls `summarize_node_async(node=blocks, root_path=…, client=…, model=…, file_path=…, table_count=…, section_count=…, path="$.blocks", action=…, leaf_min_len=…, min_len=…, lang=…)` and receives the summarized node directly rather than a `(config_node, node)` tuple.

### 3.1. Complete State Finalization in `_finalize_summary_ast_async`
Update `_finalize_summary_ast_async` to set:
```python
self.semantics_json = serialized
self.ast_json = serialized  # Preserve for backward compatibility with existing tests
if self.semantics_json_file:
    self.semantics_json_file.write_text(serialized, encoding="utf-8")
```
This ensures `embed_doc()` can seamlessly consume `self.semantics_json`.

> **Residual debt**: dual-writing the summarized tree to both `semantics_json` and `ast_json` re-blurs the pipeline state machine this refactoring is meant to enforce, and `ast_json` is updated only in memory (not on disk). See §5.2. *(Resolved by Phase 4: `semantics_json` is the sole output.)*

### 3.2. Pass Instance Settings & Propagate `action`
In `textualize`:
```python
_, blocks = await summary_node_pair_async(
    config_node=config_blocks,
    node=blocks,
    root_path=self.root_path,
    client=self.ollama_client,
    model=self.ollama_model,
    file_path=self.file_path,
    table_count=self.table_count,
    section_count=self.section_count,
    action=action,
    leaf_min_len=self.leaf_min_len,
    min_len=self.min_len,
    lang=self.lang,
)
```
In `summary_node_pair_async`:
Pass `action=action` in all recursive calls (lines 329, 336, 358) so that caller transformations are applied to all visited nodes throughout the tree.

### 3.3. Key-Aware Safe ConfigNode Access
Replace positional zipping `zip(config_node, node.items())` with key-aware access:
```python
for key, value in list(node.items()):
    if key in {"t", "s"}:
        continue
    cvalue = config_node[key] if hasattr(config_node, "__getitem__") and jsoncfg.node_exists(config_node[key]) else None
```
This eliminates key-misalignment bugs when keys appear in different orders or when non-standard keys are present.

> **Superseded by Phase 6**: the whole key-aware pairing layer is gone. There is no `ConfigNode` tree to align keys against — the recursion walks the in-memory dict only and carries a `path` string for diagnostics.

### 3.4. Cumulative Progress Tracking
Pass mutable per-counter dictionaries through the recursion so increments survive across stack frames:
```python
# In textualize:
table_counter = {"count": self.table_count}
section_counter = {"count": self.section_count}
...
self.table_count = table_counter["count"]
self.section_count = section_counter["count"]
```
`_record_summary_progress` increments `counter["count"]` in place, so progress logging accurately tracks cumulative table and section counts across the AST.

> **Residual debt**: the landed signature is `table_count: int | dict` with a legacy `int` fallback branch, but the only remaining caller passes dicts. The int path is dead weight. See §5.1. *(Resolved by Phase 1: the signature is `dict` only, and under Phase 5's gather the counters stay safe because `_record_summary_progress` has no `await` between read and write.)*

---

## 4. Verification and Test Matrix

Tests live inline in the `# | hide` cells of the two notebooks (`nbdev-test`), plus `tests/` for the integration-level behavior (`pytest`).

1. **Lifecycle Consistency**:
   - `test_finalize_summary_ast_async_updates_summary_and_cache` asserts semantic output, exact artifact bytes, and preservation of a populated input AST and its file.
   - `test_embed_doc_embeds_semantics_and_writes_artifact` verifies forwarding of seeded semantics to a mocked embedder and persistence of its output; the lifecycle integration below verifies actual textualize output.
   - `tests/test_textualize_lifecycle.py` adds an actual offline `textualize()` → `embed_doc()` integration using fake provider responses and an ephemeral Chroma collection, plus empty-document, nondefault-setting forwarding, and rerun artifact/cumulative-counter checks. Atomic failure publication and per-run counters remain open in §7.3.
2. **Action Propagation**:
   - `test_summarize_node_async_threads_action_through_every_depth` passes an `action` that records every visited node and asserts the exact traversal order across section → paragraph → list-item scalar → nested-dict, covering recursive argument forwarding on that fixture. It does not cover scalars or nested lists handled by the list branch, nor scalar dict values; see §7.1.
   - `textualize` resolves `action` through `_require_action` and threads it into the root call; `summarize_node_async` re-passes `action=action` at all three recursive sites.
3. **Counter Accumulation** (for §3.4):
   - `test_record_summary_progress_prints_expected_labels` (dict counters, key-path logging), plus `test_textualize_updates_blocks_and_document_summary` snapshots the cumulative counters written back onto the document.
4. **Key-Misalignment Safety** (for §3.3): **superseded by Phase 6.** With `jsoncfg` gone there is only one tree, so the memory-vs-disk mismatch cannot be constructed. The Phase-2 regression test (`test_summary_node_pair_async_tolerates_dict_child_missing_from_config`) was deleted along with the failure mode it defended against. The structural replacement is `test_summarize_node_async_reports_key_path_in_errors`, which asserts errors name the failing node type and key path.
5. **Concurrency** (for §5.7):
   - `test_summarize_node_async_gathers_siblings_concurrently` proves sibling subtrees are in flight simultaneously and that gathered output preserves the sequential ordering; `test_summarize_leaf_async_caps_llm_fanout_with_semaphore` proves the fan-out stays under `RIBOSOME_SUMMARY_CONCURRENCY`.
6. **Regression Safety**:
   - `pytest tests/` and offline notebook tests for both notebooks must pass; report the collected count from each run rather than retaining a historical count. Equal serialized output with a deterministic mock is covered by `test_textualize_rerun_is_idempotent`, and error wrapping by `test_textualize_wraps_unexpected_errors_with_context`.

---

## 5. Post-Implementation Review: Residual Issues

A follow-up audit of the landed refactoring (commit `372e1c2`) confirmed all five fixes work as designed, but surfaced the following residual problems, ordered by severity.

> **Status**: the six original residual items were addressed by the landed phases; new gaps are recorded in §7. §5.1 and §5.6 → Phase 1; §5.5 → Phase 3; §5.2 → Phase 4; §5.3 → Phase 6; §5.7 → Phase 5. §5.4 was addressed by Phase 2 and then made unreachable by Phase 6. Per-item notes are on §5.2–§5.4 and §5.7; the analysis text is left as written at audit time.

### 5.1. Dead `int` Fallback in Counter Plumbing
`summary_node_pair_async` and `_record_summary_progress` accept `table_count: int | dict` / `section_count: int | dict` and branch on `isinstance(..., dict)` (see `ribosome/core/dom/summary.py`, `_record_summary_progress` and the top of `summary_node_pair_async`). The sole caller (`textualize`) always passes `{"count": ...}` dicts, so the `int` branches are unreachable compatibility shims. They double the code paths in `_record_summary_progress` for no benefit.

### 5.2. `ast_json` Dual-Write Re-Blurs the State Machine
`_finalize_summary_ast_async` writes the summarized tree to **both** `self.semantics_json` and `self.ast_json`, but only persists `semantics_json_file` — `ast_json_file` on disk keeps the pre-summary tree. Consequences:
- The clean lifecycle `ast_json → semantics_json` (§2.1) is again conflated: two fields hold the same summarized content.
- On a **second** `textualize` run, `config_ast` is loaded from the stale `ast_json_file` while in-memory `ast_json` contains injected `"s"` fields — precisely the memory-vs-disk drift §2.2 diagnosed. The key-aware lookup (§3.3) tolerates the mismatch rather than eliminating it.

> **Resolved (Phases 4 and 6)**: `ast_json` is no longer re-written by finalization, and the second bullet's `config_ast` cannot exist at all now that the recursion is single-tree. Rerunning `textualize()` is covered by `test_textualize_rerun_is_idempotent`.

### 5.3. `jsoncfg` Retained Solely for Log Line Numbers
As noted in §2.2, the entire `jsoncfg` parallel tree exists only to print `jsoncfg.node_location(config_node).line` in progress logs. The key-aware fix (§3.3) makes the dual-tree traversal *safe*, but the fragility class remains: any future drift between memory and disk shapes must be defensively handled at every recursion site. Removing `jsoncfg` from the recursion entirely (logging node type / key path / index instead of line numbers) would delete the whole problem class plus a dependency, at the cost of less precise log locations.

> **Resolved (Phase 6)**: the recursion is a single-tree walk; `_config_node_line` is deleted and errors/logs carry node type plus a key path (`$.blocks[0].c[1]`) via `_node_error`. Neither `summary.py` nor `model.py` imports `jsoncfg` anymore, though the dependency itself stays in `pyproject.toml` because `core.dom.utils` (`_config_error`), `core.dom.tree`, and `process.semantics` still import it.

### 5.4. Asymmetric Tolerance of Missing Config Keys
The key-aware lookup handles a missing key by setting `cvalue = None`, but downstream handling is inconsistent:
- **List-valued children**: tolerated — `citems = cvalue if isinstance(cvalue, ConfigJSONArray) else [None] * len(value)`, and the list branch of the recursion accepts `config_node=None`.
- **Dict-valued children**: **crashes** — the recursion is entered with `cvalue=None`, where the check `if not isinstance(config_node, ConfigJSONObject): raise _config_error(...)` fires immediately.

A dict-valued key present in the in-memory AST but absent from `ast_json_file` raises, while the analogous list case silently proceeds. The behavior should be consistent (recommendation: tolerate both, matching the list path).

> **Superseded (Phase 6)**: fixed by elimination rather than by alignment — there is no `ast_json_file` config tree left to be missing a key from.

### 5.5. `leaf_min_len=0` Default Remains at the API Boundary
`summary_node_pair_async` still declares `leaf_min_len: int = 0` and `min_len: Optional[int] = None`. The LLM-call explosion of §2.5 is only fixed along the `textualize` path (which passes `self.leaf_min_len = 100`); any direct caller of `summary_node_pair_async` (tests, notebooks, other modules) silently gets the pathological default again. The default should align with `DOMClass.leaf_min_len` (100) — or the parameter should be required.

### 5.6. Dead Condition in `_finalize_summary_ast_async`
```python
dict_summary = [ast_dict["title"]] + dict_summary
ast_dict["summary"] = (
    ""
    if not ast_dict["blocks"] or dict_summary == []
    else ...
)
```
`dict_summary` always contains at least the title, so `dict_summary == []` is unreachable; the condition reduces to `not ast_dict["blocks"]`.

### 5.7. Sequential LLM Awaits
Every level of the recursion awaits children one at a time (`for ... : await summary_node_pair_async(...)`). Sibling summaries are independent and could be gathered with `asyncio.gather`, which would materially reduce wall-clock time now that §2.5's redundant calls are eliminated. (Note: counters in §3.4 are incremented inside `_record_summary_progress` after each child completes, so gather requires no counter changes, but log ordering becomes nondeterministic.)

> **Resolved (Phase 5)**: list-valued children, list items, and `_summarize_list_async` items are gathered. `asyncio.gather` preserves result order, so child result order is preserved. Equal output is established for deterministic mocks, not for live model responses or callbacks with shared mutable state. Fan-out is bounded by a per-event-loop semaphore — see §6 Phase 5.

---

## 6. Implementation Plan and Status (Residual Issues)

All six phases are landed. `summary_node_pair_async` was renamed to `summarize_node_async` in Phase 6, so the earlier phase descriptions still referencing the old name are historical.

**Workflow note**: this is an nbdev project. The source of truth is the notebooks `nbs/01.core.dom.summary.ipynb` and `nbs/01.core.dom.model.ipynb`; `ribosome/core/dom/summary.py` and `ribosome/core/dom/model.py` are generated. All edits happen in the notebooks, followed by `nbdev_export`, then the test suite.

### Phase 1 — Mechanical cleanups (no behavior change) ✅ implemented
Addresses §5.1, §5.6. Files: `nbs/01.core.dom.summary.ipynb`, `nbs/01.core.dom.model.ipynb`.

1. Change signatures to `table_count: dict` / `section_count: dict` in `summary_node_pair_async` and `_record_summary_progress`; delete the `isinstance(..., dict)` branches and the `{"count": ...}` re-wrapping at the top of `summary_node_pair_async`. `textualize` already passes dicts — no caller changes needed.
2. In `_finalize_summary_ast_async`, replace `if not ast_dict["blocks"] or dict_summary == []` with `if not ast_dict["blocks"]`.

### Phase 2 — Consistent missing-config tolerance ✅ implemented, then superseded by Phase 6
Addresses §5.4. File: `nbs/01.core.dom.summary.ipynb`.

In the dict-valued-child branch of `summary_node_pair_async`, when `cvalue` is not a `ConfigJSONObject`, skip the recursive call and summarize the child with `config_node=None` tolerance — i.e., relax the guard at the top of the dict branch from a hard `raise` to accepting `None` (mirroring the list branch, which already accepts `config_node=None`). Add a regression test: in-memory AST containing a dict-valued key absent from `ast_json_file` must not raise.

### Phase 3 — Safe defaults at the API boundary ✅ implemented
Addresses §5.5. File: `nbs/01.core.dom.summary.ipynb`.

Change `summary_node_pair_async` defaults to `leaf_min_len: int = 100` (aligned with `DOMClass.leaf_min_len`) and keep `min_len: Optional[int] = None` (None is a valid "no minimum" for non-leaf summaries). Audit existing direct callers in `tests/` and notebooks for reliance on the old `0` default.

### Phase 4 — State hygiene: single-write finalization ✅ implemented
Addresses §5.2. Files: `nbs/01.core.dom.model.ipynb`, `tests/test_dom_behavior.py`.

1. In `_finalize_summary_ast_async`, remove `self.ast_json = serialized`; `semantics_json` becomes the sole output of `textualize`.
2. Update any tests that read `dom.ast_json` after `textualize()` to read `dom.semantics_json` instead.
3. Add a rerun-idempotency test: calling `textualize()` twice in a row succeeds and produces identical `semantics_json`.

### Phase 5 — Sibling parallelization ✅ implemented
Addresses §5.7. File: `nbs/01.core.dom.summary.ipynb`.

Replace per-child `await` loops with `children = await asyncio.gather(*(summary_node_pair_async(...) for ...))`. Defer until Phases 1–4 land, since gather interacts with any in-flight mutation of shared state; verify summary output is unchanged (order-preserving gather) and only log interleaving differs.

**What landed**:
- Three gather sites: list-valued dict children, the list branch (via a local `_summarize_item` coroutine), and `_summarize_list_async`. Dict-valued keys stay sequential — a single child has nothing to fan out over — and so do scalar dict values. Long scalar values can issue LLM calls; their length is not constrained by the traversal.
- **Bounded fan-out**: `_SUMMARY_CONCURRENCY = int(os.getenv("RIBOSOME_SUMMARY_CONCURRENCY", "8"))`, enforced by `_get_summary_semaphore()`. The semaphore is keyed by running event loop in a `WeakKeyDictionary` to avoid sharing a loop-bound semaphore across loops when acquisition has to wait, and the notebooks' `run_async` test helper creates a fresh loop per call.
- Acquisition is placed **only** around the two network touchpoints — `get_summary_response_async` in `_summarize_leaf_async` and `get_image_summary_async` in `_summarize_image_node_async` — never around a nested summarizer. That avoids acquiring the same semaphore around parent and child operations. It bounds text calls in the legacy `summary_node_async` path and document finalization through `_summarize_leaf_async`; legacy image calls use `get_image_summary_response_async` directly and bypass this limiter.
- Counter safety needs no change: `_record_summary_progress` is synchronous, so read-increment-write is atomic with respect to the event loop.
- Failure behavior needs follow-up: gathered siblings can continue after one child raises, so requests and mutations may outlive the failed traversal. `with_async_context` passes existing `ValueError` instances through and wraps other `Exception` instances. Treat sibling cleanup as a correctness requirement (§7.2), not a cosmetic logging issue; an unretrieved-task warning is not an established consequence of these gather sites.
- **Not bounded**: the orphaned `get_leaf_summary_async` / `get_list_summary_async` pair still calls `get_summary_response_async` directly, bypassing the semaphore. Nothing in the `textualize` path reaches it — the list helper calls the leaf helper; both remain exported public entry points — so it is left as-is along with the legacy `summary_node_async` pair.

### Phase 6 — `jsoncfg` removal ✅ implemented
Addresses §5.3. Files: `nbs/01.core.dom.summary.ipynb`, `nbs/01.core.dom.model.ipynb`, `tests/test_summary_subnodes.py`.

Decide whether to drop `jsoncfg` from the recursion: replace line-number logging with node-type + key-path context in `_record_summary_progress` and `_config_error`, remove `config_ast` loading from `textualize`, and simplify `summary_node_pair_async` to a single-tree walk. **Tradeoff**: loses source line numbers in logs; gains elimination of the entire dual-tree fragility class and the `jsoncfg` dependency in this module. Recommend deferring until Phase 4's rerun-idempotency guarantees are in place, since single-tree walking makes §5.2-class drift impossible by construction.

**What landed**:
- **Renamed** `summary_node_pair_async` → `summarize_node_async` (it no longer walks a pair). It now returns the node itself instead of a `(config_node, node)` tuple, and takes a `path: str = "$"` key-path accumulator (`$.blocks`, `$.blocks[0].c[1]`) in place of `config_node`.
- `_node_error(message, path, node=None)` replaces `_config_error` inside this module, emitting `Node type: … / At path: …`. Every existing message substring (`Node does not have a 't' key`, `Invalid image caption format`, `Invalid image node structure`, `Invalid image link: …`, `Unsupported node type: …`, `Error summarizing node`, `Error summarizing image node`) was preserved, so `tests/test_dom_behavior.py` needed no edits. `_config_error` and `with_async_context` remain exported from `core.dom.utils` untouched.
- `_config_node_line` and its test are deleted; `jsoncfg` / `ConfigJSON*` imports are gone from the summary and model notebooks.
- `textualize` no longer reads the AST from disk (finalization still writes `semantics_json_file`): `config_ast = jsoncfg.load_config(str(self.ast_json_file))`, the `ConfigJSONObject` guard, and the `ast_json_file` precondition are removed. The legacy `summary_nodewline_main` lost its `jsoncfg` usage but still requires `ast_json_file` (it passes the path through as `file_path`), so it remains disk-coupled.
- The Phase-2 regression test `test_summary_node_pair_async_tolerates_dict_child_missing_from_config` is deleted with the failure mode it covered; `test_summarize_node_async_reports_key_path_in_errors` replaces it as the locating-errors test.
- Output equivalence is asserted, not assumed: `test_summarize_node_async_summarizes_dict_list_and_scalar_paths` keeps the exact expected strings from before the rewrite.
- Rewriting the recursion by hand exposed the one §4 gap that had never been test-pinned, so `test_summarize_node_async_threads_action_through_every_depth` was added (§4 item 2).
- The legacy `summary_node_async` (cell `acbcc743`, backing `summary_node_main`) is now a near-homonym of the new name and was deliberately left alone — removing that pair is a separate follow-up.

### Verification (after each phase)
1. `nbdev_export` and confirm the generated `.py` diffs match the notebook edits.
2. Run the full suite: `pytest tests/` — no regressions.
3. Targeted checks per phase:
   - Phase 2: dict-key-missing-from-config regression test passes. *(Removed in Phase 6 — see §4 item 4.)*
   - Phase 3: a direct call to `summary_node_pair_async` without `leaf_min_len` performs no LLM calls on short leaf tokens (mock client, assert call count).
   - Phase 4: `textualize()` → `embed_doc()` still succeeds; double-`textualize` idempotency test passes.
   - Phase 5: `test_summarize_node_async_gathers_siblings_concurrently` observes `max_inflight > 1`; `test_summarize_leaf_async_caps_llm_fanout_with_semaphore` observes `max_inflight <= RIBOSOME_SUMMARY_CONCURRENCY`.
   - Phase 6: `rg -n "jsoncfg|config_node|_config_node_line" ribosome/core/dom/summary.py ribosome/core/dom/model.py` returns nothing, and `rg -n "summary_node_pair_async" nbs` returns nothing (the only stale mentions left in the repo are inside this document's historical sections and the self-contained `tests/validation_script*.py` mock scripts, which define their own local fakes and are not collected by pytest).
4. §4's test matrix has been refreshed against the landed test names.
5. Not yet run: a live-document smoke test (`textualize()` on `assets/SN024002/SN024002.md` against a real Ollama/DashScope endpoint) to confirm the wall-clock win and read the new `… Summarize section: N at $.blocks[i].c[j]` log lines.



## 7. Next Implementation Plan (Open)

The following findings come from the current `summarize_node_async`, its helpers, and `DOMClass.textualize` / `_finalize_summary_ast_async`. Implement in notebooks, export generated modules, and add regression coverage alongside the existing offline tests. These steps are proposed work, not completed fixes.

### 7.1. Define and enforce traversal contracts — priority 1

**Observed gaps**:
- `action` runs at entry to `summarize_node_async`, but the list branch directly returns scalar items without invoking it. List-valued dict children invoke the worker on each item without invoking it on the list itself. Scalar dict values also bypass it. Image internals and ignored node types are intentionally short-circuited. The existing action test exercises only one successful route.
- `_summarize_image_node_async` hardcodes `lang='zh'` even when `textualize` passes another language.
- `_summarize_leaf_async` uses `min_len` whenever it is non-`None`, overriding `leaf_min_len` for every call. With the DOM defaults (100 and 300), the effective threshold is 300 throughout. The comment describing `min_len` as a minimum output length is inaccurate: it controls input eligibility.
- Recursive scalar/list handling can replace numeric and boolean content with strings, strip text, and replace long scalar payloads with generated summaries. Preserving `ast_json` alone does not establish preservation of structure or content in `semantics_json`.

**Implementation**:
1. Specify callback scope before changing traversal. Recommended contract: one synchronous callback per traversed value, including lists and scalar fields, excluding discriminator `t` and generated `s`; explicitly document image and ignored-node boundaries. Centralize recursion so each covered value passes through the same callback entry exactly once. Define supported replacements and validate them before aggregation. Require callbacks to avoid order-dependent shared state; concurrency does not promise a global visitation order.
2. Pass `lang` into the image helper and underlying image request. Preserve the default for direct callers.
3. Separate leaf input eligibility from aggregate input eligibility with explicit helper arguments. Document inclusive boundaries (`len(text) >= threshold`) and empty-input behavior; require nonnegative integer thresholds. Do not describe an input threshold as a minimum generated-summary length.
4. Preserve semantic AST payloads by default and write summaries to `s`. Audit downstream consumers before changing legacy coercion behavior; if coercion is required, document that contract and test it explicitly. Keep intentional callback transformations distinct from automatic summarization.

**Acceptance tests**: callback counts and replacements on nested lists, scalar dict fields, booleans, `None`, empty containers, dict children, and `subnodes`; compare coverage rather than concurrent completion order. Verify image and text language forwarding. Test thresholds at 99/100/101 and 299/300/301 with independent settings and an empty input. Compare all non-summary fields against the input with an identity action, using long text and a deterministic fake responder. Retain invalid-node key-path assertions.

### 7.2. Bound work and finish sibling cleanup before returning — priority 1

**Observed gaps**: every gather site eagerly schedules its entire input. The semaphore bounds active requests, not allocated tasks or retained subtrees. `RIBOSOME_SUMMARY_CONCURRENCY` is parsed at import; malformed values fail module import, negative values fail semaphore creation, and zero can block requests indefinitely. A failed gather does not establish that sibling operations have stopped.

**Implementation**:
1. Validate the configured concurrency as a positive integer with a clear configuration error before starting work. Document its scope: one shared request budget per event loop, across documents and clients.
2. Introduce an ordered bounded-map helper for sibling work and use it at all three gather sites. Choose and document a task/window bound as well as the request bound. Avoid recursive parent operations holding request permits while waiting for children; validate the total task bound on wide, deep fixtures, not just the bound at one level.
3. On failure or caller cancellation, cancel pending owned tasks and await their cleanup before propagating the error. Explicit task cancellation plus awaiting results can preserve the existing `ValueError` contract; a `TaskGroup` alternative must account for exception groups. Preserve the originating key path and exception cause, and do not convert cancellation into a summary failure.
4. Audit exported and legacy helpers. Route their actual text/image network calls through the limiter or document and deprecate the bypasses. Do not claim a module-wide request bound while public paths bypass it.

**Acceptance tests**: use events/barriers and timeouts rather than sleeps to prove concurrency and order. Cover text plus image requests sharing the limit, two documents on one loop, successive loops, invalid configuration, and a large wide/deep tree. Assert task allocation stays within the documented budget. Inject one child failure while siblings block; after return, assert cleanup completed, no additional requests or mutations occur, and a later run can use all permits. Repeat for caller cancellation.

### 7.3. Publish successful results consistently — priority 1

**Observed gaps**: `textualize` seeds counters from previous values and writes them back before final document summarization succeeds. Reruns therefore accumulate progress counts even when mocked serialized output matches. Finalization assigns `semantics_json` before writing its file; a write failure leaves memory and disk disagreeing, and direct `write_text` can truncate an existing artifact.

**Implementation**:
1. Define progress counters as per-run completed table/section counts, starting from zero, or introduce separate progress fields if the existing fields must retain document inventory semantics. Commit counters only after successful finalization.
2. Stage the complete result locally. When an output path is configured, write a temporary file in the destination directory and atomically replace the artifact; clean up the temporary file on failure. Assign `semantics_json` and final counters only after publication succeeds. Without a file path, commit memory after successful serialization. Preserve `ast_json` and its artifact on every path.
3. Reject overlapping `textualize` calls on the same DOM instance with a clear error, or serialize them using an explicitly managed run guard. The event-loop request semaphore alone does not protect instance publication.
4. Correct the empty-input error to name `ast_json`. Validate the decoded root and `blocks` list before issuing requests, including action-produced root replacements.

**Acceptance tests**: deterministic reruns preserve source bytes and reset progress counts; describe equal mock output as repeatability, not a guarantee about stochastic providers. Inject child, document-summary, serialization, and artifact-write failures; existing output bytes, memory, and counters must remain unchanged. Verify successful disk bytes equal `semantics_json`, temporary files are cleaned up, empty documents follow the chosen policy, and concurrent calls on one instance follow the selected guard behavior.

### 7.4. Measure remaining redundant summarization — priority 2

Safe short-leaf defaults do not prove a bounded useful-call budget: long values may be summarized during recursion, again by `_coerce_summary_async`, at list aggregation, at the parent, and at document finalization.

1. Instrument mocked request counts by stage on representative paragraphs, long single-item lists, tables, images, and nested sections. Establish a baseline before changing behavior.
2. Reuse child `s` values; avoid resummarizing already-produced scalar summaries and singleton/empty aggregations where no synthesis is needed. Define which semantic levels deserve requests. Treat output changes as intentional, with content/coverage assertions rather than only exact old strings.
3. Record total calls, maximum active calls, elapsed time, and task count. Set fixture-specific acceptance budgets; do not promise a wall-clock improvement from order-preserving gather alone.

### 7.5. Verification and completion criteria

- Run focused offline tests for each change, export the affected notebooks, inspect generated diffs, then run `pytest tests/` and offline notebook tests. Record commands, collected counts, and results. This document review has not rerun those suites and does not certify their current status.
- Inspect notebook cells before running the entire model notebook: it contains top-level awaited live-document tests. Gate live cells behind explicit opt-in so routine notebook verification needs no credentials or provider access.
- Use the existing deterministic mocks for correctness. A separate opt-in live smoke test on `assets/SN024002/SN024002.md` should record provider/model, settings, call counts, duration, and artifact validity. Compare like-for-like inputs and settings; no exact live-output equality requirement.
- A phase is complete only when its acceptance tests pass in the notebooks and generated implementation. Preserve unrelated workspace changes. Legacy API removal and eliminating `jsoncfg` from other modules remain separate work.
