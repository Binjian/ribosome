# Historical implementation record: §6 Phases 5 & 6 — sibling parallelization and jsoncfg removal

## Status and scope

Reviewed 2026-10-08. Phase 5 landed in `10a1844` and Phase 6 in `e66e579`. The follow-up implementation and verification results are recorded below. Historical test counts and performance expectations are not current verification results.

**Implementation update:** the follow-up criteria below are now implemented in the summary notebook and `tests/test_summary_concurrency.py`. The historical implementation analysis is retained below for context; the current behavior is described in the following paragraph. Atomic DOM publication/counter changes, threshold separation, and redundant-summary reduction remain separate §7 work.

The current worker uses `_ordered_summary_map` at all three sibling sites. `RIBOSOME_SUMMARY_TASK_LIMIT` (default `8`) caps concurrently active traversal-owned worker tasks across a whole traversal, excluding its caller and provider-internal tasks. Nested maps process children inline when no slots remain; they never wait for a parent to release slots. Result storage still scales with input size, and recursion depth still uses Python call frames. `RIBOSOME_SUMMARY_CONCURRENCY` (default `8`) independently caps requests per event loop, shared across documents and including legacy text/image calls. Both settings require positive integers and are validated at import and when budgets are created. Failures/cancellation cancel and await all owned pending siblings before returning. `_with_summary_context` binds actual positional/keyword arguments, preserves the originating location and cause, and leaves cancellation unchanged. Unusual dict keys use JSON-quoted bracket paths.

Callbacks now run once per traversed dict/list/scalar, excluding `t`/`s` and the internals of images and ignored nodes. Replacements preserve dict/list/scalar category; generated scalar coercion retains the existing summary contract. Images receive the chosen language. Callback completion order is not guaranteed, and callbacks must not rely on shared order-dependent state.

The authoritative open implementation work is §7 of [dom-textualize-recursion-fix.md](dom-textualize-recursion-fix.md). This document records the landed architecture and the acceptance gaps relevant to these two phases. Do not reintroduce the dual-tree worker or implement the superseded code snippets from the original version of this plan.

This is an nbdev project: edit `nbs/01.core.dom.summary.natural_text.ipynb` and `nbs/01.core.dom.model.ipynb`, then export. The generated `ribosome/core/dom/summary.py` and `model.py` are currently ignored by Git, so a clean Git status alone does not prove generated code matches the notebooks. Locate notebook cells by function/test name and verify their identity before editing; historical cell positions and editor-specific instructions are not reliable implementation targets.

## Phase 5 — Sibling parallelization — complete with limits

Three sites now use ordered bounded mapping: list-valued dict children in `summarize_node_async`, the worker's list branch, and per-item coercion in `_summarize_list_async`. Result lists retain input order. Dict-valued keys and scalar dict fields are still processed sequentially; long scalar fields can issue requests, so their sequential treatment is not justified by a guarantee that they fall below the threshold.

`_SUMMARY_CONCURRENCY` is read from `RIBOSOME_SUMMARY_CONCURRENCY` at import, defaulting to `8`. `_get_summary_semaphore` stores a semaphore for each running event loop in a `WeakKeyDictionary`. This avoids sharing a loop-bound semaphore between successive notebook test loops when acquisition must wait. The bound is per loop, shared by documents and clients on that loop; it is not a process-wide limit across loops or threads.

Request permits surround `get_summary_response_async` in `_summarize_leaf_async` and `get_image_summary_async` in `_summarize_image_node_async`. Parent recursion does not acquire permits while waiting for descendants, avoiding that recursive deadlock pattern. Document finalization and legacy text calls routed through `_summarize_leaf_async` share the budget. Legacy image/text calls and the exported `get_leaf_summary_async` / `get_list_summary_async` pair now also acquire request permits around their network touchpoints.

The semaphore bounds active requests; the independent per-traversal task budget bounds scheduled workers. Invalid settings now fail with a named positive-integer configuration error before work begins, including zero and negative values. Results still require O(n) storage; the change does not bound document memory or Python recursion depth.

`_record_summary_progress` is synchronous, with no suspension between reading and incrementing a counter. Its updates are safe relative to other tasks on the same loop; that does not establish thread safety, per-run counter semantics, or atomic publication of DOM state. Completion log order can vary.

### Failure behavior

Originally, gathered siblings could continue after a child failure was propagated. The bounded-map helper now cancels and awaits owned workers before propagating an error or caller cancellation, including workers in nested maps. This prevents subsequent sibling requests/progress updates after the failed traversal returns; it does not roll back mutations already made in the local AST.

The shared `with_async_context` remains unchanged. This traversal now uses a local signature-bound decorator that adds originating node/path context and an exception cause for unexpected failures, including plain backend `ValueError`. Already located errors pass through. Cancellation propagates as cancellation. Order-preserving results do not guarantee equal live-model output, callback visitation order, or identical behavior for callbacks that depend on shared mutable state.

### Existing regression coverage

- `test_summarize_node_async_gathers_siblings_concurrently` observes overlapping mocked leaf work and checks sibling result order.
- `test_summarize_leaf_async_caps_llm_fanout_with_semaphore` observes the text-request cap and result count.
- `test_textualize_wraps_unexpected_errors_with_context` checks wrapping of an unexpected backend failure; it does not assert that sibling tasks have stopped.

The first two notebook tests now use events and timeouts to force overlap/contention. The request-cap test accepts one permit, while the sibling-overlap test explicitly configures a task budget greater than one.

## Phase 6 — Single-tree traversal — complete

`summary_node_pair_async` was renamed to `summarize_node_async`. The worker takes the in-memory node and returns the summarized node directly, rather than returning a `(config_node, node)` tuple. It carries a diagnostic `path`, defaulting to `$`, with `textualize` starting at `$.blocks`.

`_node_error(message, path, node=None)` derives node type from the supplied node and includes the key path. The summary module no longer imports `jsoncfg`, walks `ConfigNode` objects, or uses `_config_node_line`. `_config_error` and `with_async_context` remain exported from utils without changes. The old sample decorator that treated the second positional argument as `path` was incorrect: that argument is `root_path`. New context builders must derive arguments from the actual call signature, including positional and keyword call forms.

`textualize` reads `self.ast_json` directly, with no requirement for an AST file on disk. It still writes `semantics_json_file` during finalization; removal of the disk-backed input does not make the method free of filesystem operations. The legacy `summary_nodewline_main` also uses the new worker, but still asserts `ast_json_file` and passes it as the logging file path. Removing that legacy requirement is separate work.

`jsoncfg` remains a project dependency: other modules, including DOM utils and tree, still use it. Phase 6 removed the parallel config tree from this traversal, not the dependency from the whole project. The legacy `summary_node_async` also remains separate from the new near-homonymous worker.

### Existing regression coverage

- `test_summarize_node_async_summarizes_dict_list_and_scalar_paths` retains expected output strings for a deterministic fixture.
- `test_summarize_node_async_reports_key_path_in_errors` asserts the failing key path; it does not separately assert every node-type/context field.
- `test_summarize_node_async_threads_action_through_every_depth` covers forwarding on its fixture; the new callback acceptance test additionally checks scalar list items, scalar dict fields, empty lists, replacements, and ignored-node boundaries.
- `tests/test_summary_subnodes.py` verifies recursion through `subnodes` using the single-tree API.
- `test_textualize_rerun_is_idempotent` checks equal mocked serialization and input preservation; it does not guarantee counter reset or live-provider determinism.

The missing-config regression was deleted because there is no second tree to align. Replacement tests should exercise in-memory traversal and path diagnostics, rather than reconstructing an obsolete config tree.

## Implemented follow-up acceptance criteria

The notebook changes and `tests/test_summary_concurrency.py` cover these criteria. The existing sleep-based notebook concurrency tests were also replaced with event-driven checks, including one-permit configuration:

1. Validate concurrency as a positive integer before work begins. Test missing, malformed, zero, negative, one, and greater-than-one values. Isolate configuration tests so import-time settings do not leak into other tests.
2. Prove the combined text/image request budget, two documents sharing one loop, and fresh sequential event loops. Use events/barriers with timeouts to force contention and out-of-order completion while asserting ordered results.
3. Introduce ordered bounded scheduling with a documented task budget. Measure total task allocation on both wide and deep trees; a per-level window alone does not establish a global bound. Keep parent recursion outside request permits.
4. On child failure or caller cancellation, cancel owned pending tasks and await cleanup before returning. Assert no further requests, mutations, or progress updates occur after return and that a subsequent run can use all permits. Verify cleanup at all three former gather sites, including nested failures.
5. Preserve originating node/path and exception causes for failures raised through positional and keyword API calls. Add node-type assertions, image errors, unsupported shapes, and cancellation propagation. Define escaping for unusual dict keys if key paths must uniquely locate arbitrary input.
6. Exercise `textualize` with `ast_json_file=None` and with a deliberately stale AST artifact; neither should affect traversal. Assert the input AST stays unchanged and configured semantic output still writes successfully. Treat failure publication/counter semantics as §7.3 work.
7. Establish callback coverage and permitted replacements before claiming traversal equivalence. Compare coverage and deterministic output, not concurrent callback completion order. Test nested lists, dict children, `subnodes`, and image/ignored-node boundaries.
8. Measure request counts and elapsed time separately. A semaphore plus gather proves neither elimination of redundant summaries nor a guaranteed speedup. Live comparisons require the same document, provider/model, thresholds, and concurrency settings.

## Verification workflow

1. Make notebook source changes and export with `.venv/bin/nbdev-export` before testing imported modules. Inspect the complete diff and preserve unrelated notebook/test changes; export can process other notebooks too.
2. Run focused tests for the change, then `.venv/bin/pytest tests/`. Record the command, collected count, failures, and skips rather than expecting a historical count.
3. Run `.venv/bin/nbdev-test --path nbs/01.core.dom.summary.natural_text.ipynb` and `.venv/bin/nbdev-test --path nbs/01.core.dom.model.ipynb`. Confirm live/example cells retain `# | notest`; `# | hide` does not skip execution. Avoid `--save` so verification does not rewrite outputs.
4. Check module source with `rg -n 'jsoncfg|config_node|_config_node_line|summary_node_pair_async' ribosome/core/dom/summary.py ribosome/core/dom/model.py`. No hits are expected (rg exit status 1 means no matches). Check the two affected notebooks and collected callers separately. Historical docs and self-contained `tests/validation_script*.py` fakes intentionally retain the old name, so a repository-wide zero-match requirement is wrong.
5. Inspect generated helper signatures and compare them with notebook export cells even if Git reports no generated diff. Do not count grep/rg absence alone as proof of correct runtime behavior.
6. Keep any live-provider smoke test opt-in, with a scratch output location. Record provider/model, input/settings, requests, maximum active requests, elapsed time, artifact validity, and diagnostic paths. Do not require exact live-output equality or an unmeasured wall-clock improvement.

Relevant sources are the two notebooks, their generated summary/model modules, `tests/test_summary_subnodes.py`, and textualize tests in `tests/test_dom_behavior.py`. Utils is a reference for exception behavior; edits there require an explicit consumer audit rather than incidental changes to this historical phase record.

The shared utils decorators were not modified. A mocked call-count baseline records four requests for a Section containing one Str at threshold 1; elapsed time is measured separately without claiming a speedup. Single-tree tests cover absent/stale AST artifacts while verifying source preservation and semantic artifact bytes. Live-provider smoke testing remains optional and was not run.

Verification: targeted export used `.venv/bin/nbdev-export --path nbs/01.core.dom.summary.natural_text.ipynb`. The focused pytest selection passed 55 tests (1 unrelated embedding test deselected); `tests/test_summary_concurrency.py` alone passed 47 tests. Both affected notebooks passed `nbdev-test`, and the summary notebook also passed with both limits set to `1`. The full suite ran outside the sandbox to avoid the known subprocess-fallback stall: 128 passed, 10 failed, all failures in the existing embedding tests whose partial notebook changes are ahead of the generated embedding module. Those edits were preserved. `git diff --check` passed. Phase 1–4 acceptance work was committed first as `37dacae`; this follow-up implementation is not yet committed.
