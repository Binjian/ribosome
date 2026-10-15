# Historical implementation record: §6 Phases 1–4 of `dom-textualize-recursion-fix.md`

## Status

Phases 1–4 are complete. The main implementation landed in `a132eb3` (`feat: implement Phases 1–4...`), with notebook/test synchronization and the rerun regression in `d003552`. Later work also landed Phase 5 concurrency (`10a1844`) and Phase 6 single-tree traversal (`e66e579`).

Review date: 2026-10-08. Completion here means the original changes are present in notebook source and generated modules; it does not certify a fresh passing test run or completion of the later §7 work.

This file records what Phases 1–4 changed; it is not an implementation queue. In particular, do not restore `summary_node_pair_async`, `ConfigNode`, or missing-config handling from the historical Phase 2 design. The current worker is `summarize_node_async`, which traverses one in-memory tree and reports key paths. The authoritative open work is §7 of [`docs/dom-textualize-recursion-fix.md`](dom-textualize-recursion-fix.md).

This is an nbdev project. Notebook files remain the source of truth:

- `nbs/01.core.dom.summary.natural_text.ipynb`
- `nbs/01.core.dom.model.ipynb`

The corresponding files under `ribosome/core/dom/` are generated with `.venv/bin/nbdev-export` and must not be edited directly.

## Phase 1 — Mechanical cleanup (§5.1 and §5.6) — complete

The summary traversal and `_record_summary_progress` now accept mutable counter dictionaries directly. The dead `int | dict` compatibility branches and internal re-wrapping were removed. `_finalize_summary_ast_async` now tests only whether `blocks` is empty; it no longer checks the unreachable `dict_summary == []` condition after prepending the title.

Current regression coverage:

- `test_record_summary_progress_prints_expected_labels` verifies counter mutation and labels.
- `test_summarize_node_async_summarizes_dict_list_and_scalar_paths` exercises the current worker with dictionary counters.
- `test_textualize_updates_blocks_and_document_summary` verifies that `textualize` writes the completed counts back to the DOM instance.
- `tests/test_summary_subnodes.py` exercises recursive `subnodes` handling with dictionary counters.

Current limitation: `textualize` seeds counters from the instance and commits them before final document summarization completes. Reruns can accumulate counts, and failure can publish partial counter state. This belongs to §7.3 of the authoritative plan rather than this completed phase.

## Phase 2 — Missing-config tolerance (§5.4) — superseded

The original implementation relaxed the dual-tree `jsoncfg` walk so an in-memory dict child missing from the config tree would not fail. That fix was valid for the intermediate architecture, but Phase 6 deleted the config tree entirely. There is now no missing-config case to implement or test.

The current structural replacement is `test_summarize_node_async_reports_key_path_in_errors`, which verifies node type and an in-memory path such as `$.blocks[0].c[1]`. Any new traversal test should target `summarize_node_async`; tests for `summary_node_pair_async`, `ConfigJSONObject`, or `_config_node_line` are obsolete.

## Phase 3 — Safe `leaf_min_len` API default (§5.5) — complete

`summarize_node_async` defaults `leaf_min_len` to `100`, matching `DOMClass.leaf_min_len`. `test_summarize_node_async_default_leaf_min_len_avoids_llm_calls` verifies that a direct call does not send a short leaf to the model when the argument is omitted.

This phase established only the safe default. It did not settle the threshold contract: `_summarize_leaf_async` currently uses `min_len` whenever it is non-`None`, so the DOM defaults make `300`, not `100`, the effective threshold throughout the normal `textualize` path. Separating leaf and aggregate eligibility, validating thresholds, and testing their boundaries belongs to §7.1 of the authoritative plan.

The legacy exported `summary_node_async` still has `leaf_min_len=0`. It is a separate near-homonymous path and should not be silently treated as evidence that Phase 3 regressed; audit, deprecate, or remove that legacy API explicitly.

## Phase 4 — Single-output finalization (§5.2) — complete

`_finalize_summary_ast_async` serializes the summarized tree into `semantics_json` and, when configured, writes `semantics_json_file`. It no longer overwrites `ast_json`, which remains the pre-summary source tree consumed by future runs.

Current regression coverage:

- `test_finalize_summary_ast_async_updates_summary_and_cache` now seeds a populated input AST and artifact, verifies both remain unchanged, and reads back UTF-8 semantics bytes alongside the returned summary.
- `test_textualize_updates_blocks_and_document_summary` reads the result from `semantics_json`.
- `test_textualize_rerun_is_idempotent` verifies equal serialized output across two runs with a deterministic mock while preserving `ast_json`.
- `test_embed_doc_embeds_semantics_and_writes_artifact` verifies forwarding of a manually seeded `semantics_json` to a mocked embedding function and persistence of its returned artifact. It does not execute `textualize` followed by real embedding traversal in one test.

The rerun test proves repeatability for its deterministic fake, not output equality from a stochastic provider. Finalization is also not transactional yet: memory is assigned before the artifact write, counters are committed before finalization, and direct `write_text` can leave memory and disk inconsistent on failure. Atomic publication and same-instance overlap handling belong to §7.3.

## Historical scope and later architectural changes

The original scope deliberately deferred sibling concurrency and removal of `jsoncfg`. Both subsequently landed:

- Phase 5 gathers sibling work and bounds active text/image requests with a per-event-loop semaphore.
- Phase 6 renamed `summary_node_pair_async` to `summarize_node_async`, removed `jsoncfg` from the summary/model traversal, replaced line-number context with key paths, and deleted the Phase 2 regression that described a no-longer-reachable failure.

Those changes invalidate the old cell-by-cell instructions and several old assertions about sequential visitation. `asyncio.gather` preserves result order but does not promise callback or log completion order. Current tests should assert output order and coverage without requiring a global action completion order.

## Verification for the current repository

For a documentation audit, do not claim the suites pass unless they were run in that audit. For a code change based on the current authoritative plan:

1. After notebook edits, export with `.venv/bin/nbdev-export` before running pytest against imported modules. Preserve unrelated notebook changes: the exporter can process more than the two intended sources, so inspect the complete resulting diff.
2. Run focused offline tests for the behavior being changed, for example `.venv/bin/pytest tests/test_summary_subnodes.py tests/test_dom_behavior.py -k 'summarize or textualize'`.
3. Inspect generated diffs in `ribosome/core/dom/summary/natural_text.py` and `ribosome/core/dom/model.py`; the generated code must match the notebook source and retain its autogenerated header.
4. Run `.venv/bin/pytest tests/` and record the collected count and result.
5. Run `.venv/bin/nbdev-test --path nbs/01.core.dom.summary.natural_text.ipynb`, then `.venv/bin/nbdev-test --path nbs/01.core.dom.model.ipynb`. The installed CLI accepts `--path`. The current model notebook marks live/example cells with `# | notest`; confirm those markers remain on all provider-backed cells before execution. `# | hide` only controls presentation and is not a test skip. Avoid `--save` during verification so checks do not rewrite notebook outputs.
6. Use deterministic mocks for correctness. Keep any real-provider smoke test separate and record provider/model, thresholds, call count, duration, and artifact validity.

The completed Phase 1–4 changes touched the two source notebooks, `tests/test_summary_subnodes.py`, `tests/test_dom_behavior.py`, and their generated summary/model modules. New work should follow the file list and acceptance criteria of §7 in the authoritative plan rather than the historical cell numbers that were previously recorded here.

## Acceptance coverage implemented in this update

The historical phases remain complete. Their additional acceptance checks now live in the model notebook and `tests/test_textualize_lifecycle.py`:

- The notebook finalization test and `test_textualize_rerun_preserves_artifacts_and_current_counters` verify semantic UTF-8 bytes and unchanged populated AST memory/artifact. `test_textualize_empty_blocks_writes_empty_summary_without_request` covers an empty document and asserts zero provider requests.
- `test_textualize_then_embed_doc_uses_new_semantics` exercises both lifecycle stages with deterministic provider fakes and a real ephemeral Chroma collection. It asserts the request text comes from the newly produced semantics, stored row count/content, and embedding artifact bytes, starting with an empty embedding cache.
- Root-worker arguments are asserted in the notebook and `test_textualize_forwards_settings_and_shared_counters`, including nondefault thresholds/language, action identity, path, and mutation of the passed counters.
- Rerun coverage includes input artifact preservation, semantic readback, and the existing cumulative counter semantics. Final-summary/file-write failure publication remains part of §7.3; these repeatability checks do not establish transactional publication.

Keep these acceptance criteria synchronized with §7 of the authoritative document when implementing them. Report skipped tests and environmental failures separately from passing checks.

The rerun regression explicitly records the existing cumulative counter behavior (one completed Section per run increments the instance count again). Changing to per-run counters or introducing atomic failure publication remains §7.3 work and was not folded into these verification changes. The lifecycle integration uses real embedding traversal with fake provider responses and an ephemeral Chroma collection; no provider credentials or requests are required.

Verification in this update: the focused pytest selection passed (8 tests, 1 deselected), and both summary/model notebooks passed `nbdev-test`. The model notebook was exported by filename, preserving the unrelated embedding notebook edits. Sandboxed full-suite runs stalled in the unrelated subprocess-fallback test (the bounded retry exited with timeout status 124). Rerunning `timeout 60 .venv/bin/pytest tests/ -q --tb=short` outside the sandbox completed: 81 passed, 10 failed, all failures in existing embedding tests against the old generated embedding implementation. Those workspace changes are outside this Phase 1–4 update.
