# Implement §6 Phases 1–4 of docs/dom-textualize-recursion-fix.md

## Context

Commit `372e1c2` landed the textualize/recursion refactor; a follow-up audit (doc §5) found residual issues. Per user decision, we implement **Phases 1–4 only** (Phases 5–6 — asyncio.gather and jsoncfg removal — stay deferred).

This is an **nbdev project**: source of truth is `nbs/01.core.dom.summary.ipynb` and `nbs/01.core.dom.model.ipynb`; `ribosome/core/dom/*.py` are generated. All code edits go in notebooks, then `.venv/bin/nbdev-export`, then tests. No conftest.py exists; tests live both inline in notebooks (`# | hide` cells, run via `.venv/bin/nbdev-test`) and in `tests/` (run via `.venv/bin/pytest`).

## Phase 1 — Mechanical cleanups (§5.1, §5.6)

**nbs/01.core.dom.summary.ipynb**
- Cell 25 (`_record_summary_progress`, id `78f4c0ad`): signature → `table_count: dict, section_count: dict`; delete `isinstance(..., dict)` branches — always increment `counter["count"]` and print it.
- Cell 27 (`summary_node_pair_async`, id `6f666125`): signature → `table_count: dict, section_count: dict`; delete the `{"count": ...}` re-wrapping lines at the top; use the params directly (rename internal `table_counter`/`section_counter` usages to the params).
- Cell 26 (id `38ed13a0`, test of `_record_summary_progress`): update to pass dicts (`{"count": 2}` etc.) and assert on `["count"]` values.
- Cell 28 (id `d01018ee`, test of `summary_node_pair_async`): update both calls from int counters (`1, 2` / positional `0, 0`) to `{"count": 0}` dicts; adapt the fake `_record_summary_progress` and expected `progress` tuples (`('Header', 1, 2)` → dict-based expectations).

**nbs/01.core.dom.model.ipynb**
- Cell 17 (`_finalize_summary_ast_async`, id `702d4458`): replace condition `if not ast_dict["blocks"] or dict_summary == []` with `if not ast_dict["blocks"]`.
- Cell 34 (id `82250ea8`, textualize test): update `fake_summary_pair` signature to accept the dict counters plus `action`/`leaf_min_len`/`min_len`/`lang` kwargs (currently missing — signature drift masked by mock_patch).

**tests/test_summary_subnodes.py** (line ~28): change positional int counters `0, 0` → `{"count": 0}, {"count": 0}`.

## Phase 2 — Consistent missing-config tolerance (§5.4)

**nbs/01.core.dom.summary.ipynb**, cell 27:
- Dict branch guard: change `if not isinstance(config_node, ConfigJSONObject): raise _config_error(...)` to `if config_node is not None and not isinstance(config_node, ConfigJSONObject): raise ...` (mirrors the list branch at the `elif isinstance(node, list)` level).
- Inside the dict branch, the key-aware `cvalue` lookup and `_record_summary_progress(t, config_node, ...)` already tolerate None (`_config_node_line` catches exceptions → "unknown"; `_config_error` in `ribosome/core/dom/utils.py:110-115` is None-safe). Verify `config_node[key]` access is skipped when `config_node is None` (add `config_node is not None and` to the `cvalue` condition).
- New regression test (add to cell 28 or tests/test_summary_subnodes.py): in-memory dict node has a dict-valued key absent from the jsoncfg config tree → recursion completes without raising, `"s"` populated.

## Phase 3 — Safe default for leaf_min_len (§5.5)

**nbs/01.core.dom.summary.ipynb**, cell 27: `leaf_min_len: int = 0` → `leaf_min_len: int = 100` (matches `DOMClass.leaf_min_len`, model.py:188).
- Audit done: only callers are `textualize` (passes explicit value), tests/test_summary_subnodes.py (passes 9999), notebook cell 28 (passes 0 explicitly) — all unaffected.
- New test (cell 28): call `summary_node_pair_async` **without** `leaf_min_len` on a short leaf string with a mocked `get_summary_response_async`; assert zero LLM calls (short text below the 100-char threshold is returned as-is).

## Phase 4 — Single-write finalization (§5.2)

**nbs/01.core.dom.model.ipynb**, cell 17: delete the line `self.ast_json = serialized` from `_finalize_summary_ast_async`. Update docstring if it mentions ast_json.

Update assertions that read `ast_json` after textualize/finalize:
- Cell 18 (id `d0e5f8f1`): `test_eq(dom.ast_json, serialized)` → assert `dom.semantics_json == serialized` and `dom.ast_json` unchanged (still pre-summary content).
- Cell 34 (id `82250ea8`): `result = json.loads(dom.ast_json)` → `json.loads(dom.semantics_json)`.
- **tests/test_dom_behavior.py:112**: `result = json.loads(dom.ast_json)` → `json.loads(dom.semantics_json)`.

New rerun-idempotency test in tests/test_dom_behavior.py (reuse existing mock pattern: `FakeOllamaClient` at line 25, `object.__setattr__` injection at line 63, `unittest.mock.patch("ribosome.core.dom.summary.get_summary_response_async", ...)` at lines 107–109): call `await dom.textualize()` twice; assert both runs succeed and produce identical `semantics_json` (deterministic fake summaries).

## Execution order & verification

Implement Phases 1–4 in order in the notebooks + tests files, then:
1. `.venv/bin/nbdev-export` — confirm `ribosome/core/dom/summary.py` and `model.py` diffs match notebook edits.
2. `.venv/bin/pytest` (testpaths=tests per pyproject.toml) — full suite green.
3. `.venv/bin/nbdev-test` — inline notebook tests green.
4. Spot-check doc §4 items: semantics_json populated + file written; embed_doc succeeds after textualize (covered by tests/test_dom_behavior.py:175 flow).

## Files touched

- nbs/01.core.dom.summary.ipynb (cells 25, 26, 27, 28)
- nbs/01.core.dom.model.ipynb (cells 17, 18, 34)
- tests/test_summary_subnodes.py
- tests/test_dom_behavior.py
- ribosome/core/dom/summary.py, ribosome/core/dom/model.py (regenerated only, via nbdev-export)