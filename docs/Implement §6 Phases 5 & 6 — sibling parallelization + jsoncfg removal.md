# Implement §6 Phases 5 & 6 — sibling parallelization + jsoncfg removal

## Context

`docs/dom-textualize-recursion-fix.md` §6 lists six phases. Phases 1–4 are already implemented and green
(81 pytest tests, both notebooks pass `nbdev-test`). Two residual issues from §5 remain:

- **§5.7 Sequential LLM awaits** — `summary_node_pair_async` awaits each child one at a time, so a document's
  independent subtree summaries run serially. Now that §2.5's redundant-call explosion is fixed, wall-clock
  time is dominated by serialized network round-trips.
- **§5.3 jsoncfg retained solely for log line numbers** — the recursion walks *two* trees: the in-memory dict
  AST and a disk-backed `jsoncfg` `ConfigNode` tree loaded from `ast_json_file`. The config tree is used for
  nothing but `jsoncfg.node_location(...).line` in log messages, yet it is the source of the whole
  memory-vs-disk drift fragility class (§5.4, fixed defensively in Phase 2) and forces `textualize` to require
  a flushed AST file on disk.

Outcome: sibling summaries run concurrently under a bounded fan-out, and the recursion becomes a single-tree
walk that logs `node type + key path` instead of a source line number. Per the tradeoff recorded in §6, we
lose line numbers in logs and gain elimination of the dual-tree failure mode by construction.

**User decisions already taken:** implement in documented order (Phase 5, then Phase 6); rename
`summary_node_pair_async` → `summarize_node_async`; bound the fan-out with a semaphore; keep `_config_error`
and `with_async_context` exported from `core.dom.utils` untouched.

**Workflow note (nbdev):** notebooks are the source of truth. `ribosome/core/dom/*.py` are generated *and
git-ignored*. All edits go into `nbs/01.core.dom.summary.ipynb` and `nbs/01.core.dom.model.ipynb`, then
`.venv/bin/nbdev-export`, then tests. `NotebookEdit` requires a fresh `Read` of the notebook immediately
before every edit.

---

## Phase 5 — Sibling parallelization (dual-tree walk still in place)

File: `nbs/01.core.dom.summary.ipynb`

### 5.1 Exported imports + bounded concurrency (cell `3cc79b0f`)

`asyncio` is currently imported only in the `# | hide` cell `80b336b7`, so it is *not* in the generated
`summary.py`. Add `import asyncio` and `from weakref import WeakKeyDictionary` to the exported import cell.

Add a loop-keyed semaphore. Python is `>=3.12`, where `asyncio.Semaphore` binds to the running loop on first
await and raises if reused from another loop — and the notebook test helper `run_async` (cell `5d7e0e77`)
spawns a *fresh* loop in a thread per call. A plain module-level semaphore would therefore break the second
test that uses it:

```python
_SUMMARY_CONCURRENCY = int(os.getenv("RIBOSOME_SUMMARY_CONCURRENCY", "8"))
_summary_semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()

def _get_summary_semaphore() -> asyncio.Semaphore:
    """Per-event-loop semaphore bounding concurrent LLM calls."""
    loop = asyncio.get_running_loop()
    sem = _summary_semaphores.get(loop)
    if sem is None:
        sem = _summary_semaphores[loop] = asyncio.Semaphore(_SUMMARY_CONCURRENCY)
    return sem
```

### 5.2 Guard the two network touchpoints (not the recursion)

Acquire the semaphore only around actual LLM calls, never around a nested summarizer — that keeps the
semaphore non-reentrant use impossible (no deadlock):

- cell `8f89b3fc` `_summarize_leaf_async`: wrap just the `await get_summary_response_async(...)` call in
  `async with _get_summary_semaphore():`. The short-text early return stays outside the guard.
- cell `f7bcc663` `_summarize_image_node_async`: wrap the `await get_image_summary_async(...)` call.

This also bounds `summary_node_async` (legacy) and `_finalize_summary_ast_async` (model) for free, since both
route through `_summarize_leaf_async`.

### 5.3 Gather the sibling recursion (cell `6f666125`)

Three sites, all order-preserving (`asyncio.gather` returns results in argument order):

1. **dict branch, list-valued key** — replace the `for citem, item in zip(citems, value)` loop:
   ```python
   results = await asyncio.gather(*(
       summary_node_pair_async(
           citem, item,
           root_path=root_path, client=client, model=model, file_path=file_path,
           table_count=table_count, section_count=section_count,
           action=action, leaf_min_len=leaf_min_len, min_len=min_len, lang=lang,
       )
       for citem, item in zip(citems, value)
   ))
   children = [child for _, child in results]
   node[key] = children
   ```
2. **list branch** — hoist the per-item dispatch into a local coroutine and gather it:
   ```python
   async def _child(citem, item):
       if isinstance(item, (dict, list)):
           _, child = await summary_node_pair_async(citem, item, ...)
           return child
       if isinstance(item, (str, int, float, bool)):
           return str(item).strip()
       return item

   node = list(await asyncio.gather(*(_child(c, i) for c, i in zip(citems, node))))
   ```
3. cell `92951cb5` `_summarize_list_async` — gather the per-item coercion:
   ```python
   parts = list(await asyncio.gather(*(
       _coerce_summary_async(item, leaf_min_len=leaf_min_len, min_len=min_len,
                             client=client, model=model, lang=lang)
       for item in root
   )))
   ```

**Left sequential on purpose:** dict-valued keys (single child, nothing to parallelize) and scalar dict values
(Header level, attrs — below `leaf_min_len`, so no LLM call and no benefit).

**Shared-state safety:** `table_count` / `section_count` are mutated only inside the synchronous
`_record_summary_progress` (cell `78f4c0ad`), which contains no `await` between read and write — atomic with
respect to the event loop. Log *ordering* becomes nondeterministic; accepted per §5.7.

**Error semantics:** default `gather` (no `return_exceptions`) propagates the first exception, which the
`@with_async_context` wrapper on `summary_node_pair_async` re-wraps into the
`"Error summarizing node ... backend exploded"` `ValueError` that
`tests/test_dom_behavior.py::test_textualize_wraps_unexpected_errors_with_context` asserts. Loser siblings are
not cancelled and may log "Task exception was never retrieved"; that is cosmetic and expected.

### 5.4 New tests (append to hide cell `d01018ee`, or a new `# | hide` cell after it)

- `test_summarize_siblings_run_concurrently`: fake `_summarize_leaf_async` that increments an in-flight
  counter, `await asyncio.sleep(0)`, then decrements, tracking the max. Payload with ≥3 sibling subtrees;
  assert `max_inflight > 1` and that the returned tree matches the same expected strings the existing
  sequential test asserts (order preservation).
- `test_summary_semaphore_caps_llm_fanout`: patch `get_summary_response_async` with a concurrency-tracking
  fake, `asyncio.gather` 20 `_summarize_leaf_async` calls over threshold, assert `max_inflight <=
  _SUMMARY_CONCURRENCY`.

### Phase 5 verification
1. `.venv/bin/nbdev-export`; confirm `ribosome/core/dom/summary.py` now contains `import asyncio`,
   `_get_summary_semaphore`, and three `asyncio.gather` sites.
2. `.venv/bin/pytest tests/` — expect 81 passing, no regressions.
3. `.venv/bin/nbdev-test --path nbs --file_glob "01.core.dom.summary.ipynb"` → Success.
4. `.venv/bin/nbdev-test --path nbs --file_glob "01.core.dom.model.ipynb"` → Success (`textualize` path).

---

## Phase 6 — jsoncfg removal, single-tree walk, rename

Files: `nbs/01.core.dom.summary.ipynb`, `nbs/01.core.dom.model.ipynb`, `tests/test_summary_subnodes.py`,
`docs/dom-textualize-recursion-fix.md`

### 6.1 New error helper replacing `_config_error` in summary (new exported cell near `57194bc8`)

Substring-compatible with every existing `contains=` assertion:

```python
def _node_error(message: str, node_type: str, path: str, node=None) -> ValueError:
    details = [message, f"Node type: {node_type}", f"At path: {path}"]
    if node is not None:
        details.insert(1, f"Node: {node}")
    return ValueError("\n".join(details))
```

Preserved messages: `Node does not have a 't' key`, `Invalid image caption format`,
`Invalid image node structure`, `Invalid image link: ...`, `Unsupported node type: ...`,
`Error summarizing node`, `Error summarizing image node`.

### 6.2 Delete `_config_node_line` (cell `57194bc8`) and its test (cell `ba9bf318`)

This is the one deviation from "keep it": `_config_node_line` lives in the **summary** notebook, not utils,
and has zero consumers outside the two log lines being rewritten (verified by grep across `nbs/`, `tests/`,
`docs/`). Keeping it would keep the `jsoncfg` import in `summary.py`, defeating the phase's stated goal.
`_config_error` and `with_async_context` in `core.dom.utils` stay exactly as they are.

### 6.3 Imports (cell `3cc79b0f`)

Drop `import jsoncfg`, the `ConfigJSONArray / ConfigJSONObject / ConfigJSONScalar / ConfigNode` imports, and
`_config_error` from the `core.dom.utils` import; keep `with_async_context`. In hide cell `5d7e0e77`, delete
the now-unused `make_config` helper.

### 6.4 `_resolve_image_link` (cell `117061a4`) and `_summarize_image_node_async` (cell `f7bcc663`)

- `_resolve_image_link(node, root_path, node_type, path)` — raise `_node_error(...)` instead of `_config_error`.
- `_summarize_image_node_async(node, root_path, client, model, node_type, path)`; decorator becomes
  `@with_async_context(lambda node, *a, **k: str(_node_error("Error summarizing image node", node.get("t", "?"), path_arg, node)))`
  — concretely: `@with_async_context(lambda node, root_path, client, model, node_type, path: str(_node_error("Error summarizing image node", node_type, path, node)))`.
- Log line: `print(f"Summarize image: {image_link} at {path}")`.
- Update test cell `9e3fab94` (drops the leading `None` config arg, adds `node_type`/`path`).

### 6.5 `_record_summary_progress` (cell `78f4c0ad`) + test (cell `38ed13a0`)

`config_node: ConfigNode` → `path: str`; prints `... Summarize table: N at {path}` /
`... Summarize section: N at {path}`. Test builds a path string instead of `make_config(...)`.

### 6.6 The recursion: `summary_node_pair_async` → `summarize_node_async` (cell `6f666125`)

New signature (returns the node directly, not a `(config_node, node)` tuple):

```python
@with_async_context(lambda node, path, *a, **k: str(_node_error("Error summarizing node", type(node).__name__, path, node)))
async def summarize_node_async(
    node: dict | list | str | int | float | bool | None,
    root_path: Path,
    client: AsyncClient | AsyncOpenAI,
    model: str,
    file_path: Path,
    table_count: dict,
    section_count: dict,
    path: str = "$",
    action: Optional[Callable] = None,
    leaf_min_len: int = 100,
    min_len: Optional[int] = None,
    lang: str = "zh",
) -> dict | list | str | int | float | bool | None:
```

Body changes:
- Delete every `isinstance(config_node, ConfigJSON*)` guard and the `cvalue` / `citems` zip machinery.
- Child paths: dict-valued key → `f"{path}.{key}"`; list items → `f"{path}.{key}[{i}]"` (and `f"{path}[{i}]"`
  in the list branch). Pass `path=child_path` into each recursive `summarize_node_async` call inside the
  Phase-5 gathers.
- Unsupported-shape raises become `_node_error(f"Unsupported node type: {type(node)}", type(node).__name__, path, node)`.
- `return node`.

**Caution:** the legacy `summary_node_async` (cell `acbcc743`) stays and is now a near-homonym of
`summarize_node_async`. Leave it untouched (it backs `summary_node_main`); flag removal of the legacy pair as
a separate follow-up rather than folding it into this change.

### 6.7 Recursion tests (cell `d01018ee`)

- `test_summary_node_pair_async_summarizes_dict_list_and_scalar_paths` → rename to
  `test_summarize_node_async_summarizes_dict_list_and_scalar_paths`, drop the `config` argument, expect a bare
  node (not a tuple), and keep the same expected strings — this is the regression net proving the single-tree
  walk is output-identical. `fake_progress` signature gains `path`.
- **Delete** `test_summary_node_pair_async_tolerates_dict_child_missing_from_config` — the Phase-2 failure mode
  (dict key present in memory, absent from `ast_json_file`) cannot exist once there is only one tree.
- `test_summary_node_pair_async_default_leaf_min_len_avoids_llm_calls` → keep as
  `test_summarize_node_async_default_leaf_min_len_avoids_llm_calls`, dropping the `make_config(...)['v']`
  first argument.
- Phase-5 concurrency tests: update to the new name/signature.

### 6.8 Model notebook (`nbs/01.core.dom.model.ipynb`)

Duplicate cell ids exist in this notebook (`8ba2abd1` at indices 32 *and* 33; `97bcb2cb` at 43 and 45), so
address cells by index (`cell-33`) and re-`Read` after every edit.

- cell 8 (`2b3fc27e`, exported imports): `summary_node_pair_async` → `summarize_node_async`; drop
  `import jsoncfg` and `from jsoncfg.config_classes import ConfigJSONObject`.
- cell 9 (`337d028a`, hide): drop the `jsoncfg = __import__("jsoncfg")` / `ConfigJSONObject = ...` lines.
- cell 33 (`8ba2abd1`, `textualize`): delete `config_ast = jsoncfg.load_config(str(self.ast_json_file))`, the
  `isinstance(config_ast, ConfigJSONObject)` check, and `config_blocks`; call
  `blocks = await summarize_node_async(node=blocks, root_path=..., client=..., model=..., file_path=..., table_count=table_counter, section_count=section_counter, path="$.blocks", action=action, leaf_min_len=..., min_len=..., lang=...)`.
  `textualize` no longer touches the filesystem — the `ast_json_file` precondition disappears.
- cell 34 (`82250ea8`): fake becomes `async def fake_summarize(node, root_path, client, model, file_path, table_count, section_count, path="$", action=None, leaf_min_len=100, min_len=None, lang="zh")` returning the
  bare list; keep the `dict(table_count)` snapshot assertions and the `dom.ast_json` unchanged assertion.
- cell 37 (`6fe67ad6`, `summary_nodewline_main`): same jsoncfg removal + rename; drop the
  `assert self.ast_json_file` line.
- cell 38 (`bebc4e28`): fake signature update, bare return.

### 6.9 `tests/test_summary_subnodes.py`

Drop `import jsoncfg` and the `config = jsoncfg.loads_config(...)` construction; call
`summarize_node_async(payload, Path('.'), object(), "unused", Path('doc_ast.json'), {"count": 0}, {"count": 0}, leaf_min_len=9999, min_len=9999)`
and use the returned node directly (no `_,` unpack). Assertions about `subnodes` recursion are unchanged.

`tests/test_dom_behavior.py` needs no edits — it patches `get_summary_response_async` /
`get_image_summary_async`, which keep their signatures. `tests/validation_script*.py` are not collected by
pytest (filename filter) and only import `with_async_context`, which `summary.py` still re-exports.

`nbs/01.core.dom.tree.ipynb` keeps its own jsoncfg usage and is imported by nothing — out of scope, so
`jsoncfg` stays in `pyproject.toml` / `requirements.txt`.

### 6.10 Doc update

In `docs/dom-textualize-recursion-fix.md`: mark §5.3 and §5.7 resolved, note the rename
(`summary_node_pair_async` → `summarize_node_async`), the `RIBOSOME_SUMMARY_CONCURRENCY` bound, the
line-number → key-path logging change, and refresh §4's test matrix to point at the renamed/new tests.

### Phase 6 verification
1. `.venv/bin/nbdev-export`, then:
   `grep -n "jsoncfg\|config_node\|_config_node_line" ribosome/core/dom/summary.py ribosome/core/dom/model.py` → no hits.
   `grep -rn "summary_node_pair_async" nbs tests` → no hits.
2. `.venv/bin/pytest tests/` — green (one Phase-2 test intentionally removed, two Phase-5 tests added).
3. `.venv/bin/nbdev-test --path nbs --file_glob "01.core.dom.summary.ipynb"` and `"01.core.dom.model.ipynb"` → Success.
4. Output equivalence: the renamed `test_summarize_node_async_summarizes_dict_list_and_scalar_paths` asserts
   the identical expected strings as before the rewrite, and
   `tests/test_dom_behavior.py::test_textualize_summarizes_text_and_images` +
   `::test_textualize_rerun_is_idempotent` still pass unchanged.
5. Optional manual smoke (needs a live Ollama/DashScope): run `textualize()` on `assets/SN024002/SN024002.md`
   and confirm the log now shows `... Summarize section: N at $.blocks[i].c[j]` and wall-clock time drops.

---

## Critical files

| File | Role |
| --- | --- |
| `nbs/01.core.dom.summary.ipynb` | Phases 5 & 6 source of truth: cells `3cc79b0f`, `8f89b3fc`, `92951cb5`, `117061a4`, `57194bc8`/`ba9bf318`, `f7bcc663`/`9e3fab94`, `78f4c0ad`/`38ed13a0`, `6f666125`/`d01018ee`, `5d7e0e77` |
| `nbs/01.core.dom.model.ipynb` | Phase 6: cells 8, 9, 33, 34, 37, 38 (index-addressed; duplicate ids present) |
| `tests/test_summary_subnodes.py` | Phase 6 caller update |
| `ribosome/core/dom/utils.py` | Read-only reference: `with_async_context` (110–156) passes the wrapped fn's args to the context builder — unchanged |
| `docs/dom-textualize-recursion-fix.md` | §5.3/§5.7 status + §4 matrix update |

Reused rather than rewritten: `with_async_context`, `_summarize_leaf_async`, `_summarize_list_async`,
`_coerce_summary_async`, `_record_summary_progress`, `_finalize_summary_ast_async`.