# Fix `embed()` / `embed_walk_node()` correctness issues in the DOM embedding pipeline

## Context

Review of `embed()` (ribosome/core/dom/embedding/natural_text.py:120-189, generated from `nbs/01.core.dom.embedding.natural_text.ipynb` cell 13) and its recursive helpers `embed_node()` / `embed_walk_node()` found real correctness bugs in how semantics-JSON nodes are walked, hashed, embedded, and stored into the Chroma collection. The walk does cover every dict/list value of the pandoc-style AST, but:

> **Implementation status (2026-10-09):** implemented in the notebook source and exported. The partial draft has been completed with framed structural IDs, a pre-embedding action pass, response-model metadata, stale-row cleanup, and propagated failures. See the verification record below. The bug descriptions describe the original implementation.

**Verified bugs (all confirmed against the code):**

1. **Double-embedding of dict-valued child nodes** (embedding.py:260-263). When a child is a *dict value* (not inside a list) with `t ∈ embed_types`, `embed_node(value)` runs first, then `embed_walk_node(value)` recurses and its tail (line 265-267) calls `embed_node` **again**. The second call hashes the node *after* `'i'`/`'m'` were injected by the first → different sha256 → **two Chroma rows for the same text** (first id orphaned) + a wasted embedding API call. The first call also runs *before* `action` is applied, unlike every other embed point. No existing test covers the dict-child branch (all fixtures use list-valued children).
2. **`cur_object_path` is not an ancestor path** (line 268). Node ids are appended *after* children are processed and never popped, so the "path" is `[root_id] + completion-ordered ids of all previously embedded nodes`. Node ids therefore depend on global traversal order, the list grows unboundedly (O(n²) hashing of `str(cur_object_path)`), and the intended path semantics (cf. commented-out `'obj_path'` metadata, line 217) are lost.
3. **Root double-embed risk (latent)**: `embed()` embeds the root summary itself (id = sha256(file_path + canonical_json), lines 143-173), then `embed_walk_node(ast)` would embed the root again if `ast['t'] ∈ embed_types` — overwriting `ast['i']` with a different id and appending it to the path twice. Production roots have no `'t'`, but direct `embed_walk_node` callers (notebook cell 18) do pass embeddable roots.
4. **Fragile model metadata** (line 215): `node_response.model` raises `AttributeError` if the response was a dict — which `get_embeddings_with_retry` explicitly supports (lines 80-89). Also inconsistent: root metadata uses the requested model, node metadata the response model.
5. **No provenance metadata**: Chroma rows carry only `{"embed_model": ...}` — impossible to filter/delete per document. The repo's own convention (`ribosome/retrieval/layout_rag.py:946-966`) stores `document_id`, `content_type`, scalar `section_path`, etc., and uses `upsert`.
6. **`collection.add` instead of `upsert`** (lines 168, 234): re-running `embed_doc` on an unchanged document hits duplicate-id errors that are silently swallowed by `try/except print`.
7. **Empty root summary is still embedded** (lines 149-155) while `embed_node` skips empty summaries — inconsistent, wastes an API call, stores an empty document row.
8. **Identical siblings need distinct identities.** An ancestor-ID-only scheme gives two identical nodes under the same parent the same hash. Switching from `add` to `upsert` would then hide the collision by replacing the first row.
9. **Upsert does not remove stale rows.** It makes an unchanged rerun safe, but when a node is removed or its ID changes, the old row remains queryable. Provenance metadata makes cleanup possible; the embedding transaction must actually perform that cleanup.
10. **Storage failures are reported as success.** Both root and node writes catch broad exceptions, print, and continue producing `_embed.json` with `i`/`m` fields. The artifact can therefore claim rows exist when Chroma rejected them.

Non-issues / keep as-is: preserve `separators=(",", ";")` in the root canonicalization so ordinary semantics inputs retain their current root ID; unused `db_client` remains for caller compatibility (`model.py:500`). Node IDs intentionally change under the scheme below. Generated `i`/`m` fields must be excluded from canonical semantic content so an exported embed artifact can be embedded again without recursively changing its identity.

**Scope decision (user-approved):** correctness fixes only. Batch embedding (two-phase walk-then-batch via Ollama list input) is deferred to a separate change — it would alter per-node call order that current tests/fakes assert.

**ID contract:** the document ID remains `sha256(str(file_path) + canonical_semantic_json)` for metadata-free input. Each node ID hashes a canonically serialized tuple/object containing `doc_id`, `structural_path`, and `canonical_semantic_node`; do not concatenate variable-length fields without framing. `structural_path` is an unambiguous JSON-pointer-like path containing dict keys and list indexes. This makes identical siblings distinct and removes completion-order dependence. Reordering list elements is allowed to change their IDs; promising stability across such a reorder conflicts with positional uniqueness unless the AST supplies a durable semantic key. Existing node IDs will change once. The implementation removes obsolete rows for the same source `str(file_path)` after all new rows have been written successfully. Callers that need cross-invocation identity must pass the path consistently; changing path normalization is outside this compatibility-focused change.

## Changes

**Source of truth is the notebook** `nbs/01.core.dom.embedding.natural_text.ipynb`; `ribosome/core/dom/embedding/natural_text.py` is autogenerated (never hand-edit). Keep the `embed()` signature unchanged so `DOMClass.embed_doc` needs no production-code change. Preserve exported helper call forms through wrappers while private helpers carry the new explicit context.

### 1. Shared canonicalization and identity helpers

Add small private helpers in the notebook source:

- `_semantic_payload(value)` recursively copies content while omitting pipeline-owned `i`/`m` fields on recognized document/AST nodes. Define that recognition narrowly (for example, a 64-hex ID plus the embedding metadata schema) so an arbitrary nested business dict with semantic keys named `i` or `m` is not silently erased. Do not mutate the caller's input while hashing.
- `_canonical_json(value)` owns the existing JSON settings, including `sort_keys=True`, `ensure_ascii=False`, and `separators=(',', ';')`.
- `_child_path(parent, key_or_index)` emits an unambiguous escaped path; do not join arbitrary keys with `/` without escaping.
- `_response_model(response, requested_model)` handles object and dict responses. Use it for root and node rows so `embed_model` consistently means the model reported by the provider, falling back to the requested model. If both are useful, add a separate `requested_embed_model` field rather than changing the meaning by row type.

Validate decoded input before API calls: the root must be a dict, `action` must preserve each visited value's container/scalar category, and `embed_types` must contain strings. Use exact `str(file_path)` consistently for the compatibility-sensitive root ID, metadata, and cleanup, and test relative and absolute inputs as distinct source identities.

### 2. Cell 17 — `embed_walk_node` (fixes 1, 2, 3, 8)

Give the internal walker explicit immutable context: `doc_id`, `structural_path`, `file_path`, and `skip_self_embed`. A separate mutable stack is no longer needed for identity.

- Run a dedicated recursive transform pass first, applying `action` exactly once to every value covered by the documented callback contract. Validate every replacement. The identity/embedding walk then receives the fully transformed tree and never calls `action`; consequently the document ID, node IDs, stored text, and returned AST all describe the same content. With the default identity action, the root ID remains byte-compatible.
- For every dict/list child, recurse once and assign the returned value. Remove the dict child's pre-emptive `embed_node` call. Scalar values remain unchanged unless the documented action contract explicitly includes them.
- Build each child's structural path from its dict key or list index. Use that path in the node-ID hash and metadata. This distinguishes identical siblings. Do not claim that IDs survive list reordering.
- Keep the existing child-before-parent node insertion order if compatibility requires it, but do not use insertion/completion order as hash input.
- Embed the current node at one tail call unless `skip_self_embed=True`. No push/pop cleanup is required when path values are immutable.
- `embed_node` and `embed_walk_node` are exported today. Keep their existing positional call forms as thin compatibility wrappers (with documented default root/path context) around new private context-rich helpers, or explicitly deprecate them before a later breaking release. Add tests for both the public wrapper and the internal path-aware route; do not silently reinterpret `cur_object_path`.

### 3. Cell 15 — `embed_node` (fixes 4, 5, 6, 8, 10)

- Accept the precomputed `node_id`, `doc_id`, and structural path explicitly. A direct caller must supply enough context to compute the same formula; do not trust a stale input `i` merely because it is present.
- Fix 4 — dict-safe model extraction, no rebinding of the `model` param:
  ```python
  response_model = getattr(node_response, "model", None)
  if response_model is None and isinstance(node_response, dict):
      response_model = node_response.get("model")
  embed_model = response_model if isinstance(response_model, str) and response_model else model
  ```
  Put this logic in the shared helper and use it for the root as well.
- Fix 5 — enriched scalar metadata (Chroma-safe), mirroring layout_rag.py conventions:
  ```python
  node_metadata = {
      'embed_model': embed_model,
      'node_type': node.get('t', ''),
      'document_id': doc_id,
      'node_path': structural_path,
      'file_path': file_path,
  }
  ```
- Use `document_id`, matching `layout_rag.py`, rather than introducing the competing metadata name `doc_id`. Retain `embed_model` for compatibility with the existing DOM artifact schema; a future cross-pipeline schema migration can standardize it with `embedding_model` separately.
- Pipeline calls require non-empty provenance. A compatibility wrapper invoked without `file_path` should omit unavailable optional fields rather than persist ambiguous empty-string provenance; test the behavior against the supported Chroma version.
- Use `collection.upsert(...)`. Let invalid embeddings and persistence errors propagate with document/node/path context; do not emit `i`/`m` as though persistence succeeded. Empty summaries are an intentional skip and receive no row metadata.

### 4. Cell 13 — `embed` (fixes 3, 5, 6, 7, 9, 10)

- Complete the recursive action pass, validate the transformed root, strip generated metadata for canonicalization, then compute the document ID. For ordinary metadata-free input under the identity action this preserves `sha256(str(file_path) + canonical_json)`.
- Only request a root embedding when its stripped summary is non-empty. Use the response-model helper and the same scalar metadata schema as node rows. Give the root structural path `$` rather than overloading `node_path` with its ID.
- Walk with `skip_self_embed=True`; children are still visited and no action is run during this pass.
- Snapshot IDs currently associated with exact `file_path`, then track every successfully upserted ID for this run. Only after all embeddings and upserts succeed, delete `old_ids - new_ids`. This ordering preserves the previous complete rows if generation fails, although a mid-upsert failure can leave additional new rows; surface that partial state and make a retry converge. If Chroma cannot provide the required filtered read/delete semantics, state that limitation and add the smallest compatible manifest strategy rather than deleting first. Rows created by old versions without provenance cannot be discovered automatically and require a one-time collection rebuild or separate migration.
- Build the returned JSON only after persistence and stale-row cleanup succeed. Set `i`/`m` for rows that actually exist. Propagate failures so `DOMClass.embed_doc` does not publish a misleading `_embed.json`.
- Add a one-line comment noting `db_client` is accepted for caller compatibility but unused.

### 5. Test fakes + updated assertions

- `FakeCollection` in all three locations must model Chroma semantics: keep records keyed by ID, replace on `upsert`, support the filtered lookup/delete used for stale cleanup, and retain a separate operation log when insertion order matters. Implementing `upsert` as `add` cannot test rerun idempotency.
- Notebook cell 14 (`test_embed_adds_ast_and_nested_nodes...`): update root metadata to the consistent response-model rule and structural path `$`; extend assertions for `file_path` and `document_id`. When an action changes the tree, recompute the expected root ID from the fully transformed semantic payload.
- Notebook cell 16 (`embed_node` tests): update metadata equality to the new key set; **add fix-4 regression** — client returning raw dict `{"embeddings": [[...]], "model": "dict-model"}` must not crash and yields `embed_model == "dict-model"`; dict without `"model"` falls back to requested model.
- Notebook cell 18 (`embed_walk_node` test): update it for immutable structural paths and assert child-before-parent operation order independently from stored-record order.
- **New hidden notebook cell after 18** with regressions:
  - *Fix 1*: `{"t": "Section", "s": "root", "meta_node": {"t": "Table", "s": "table"}}`, `embed_types={"Section","Table"}` → exactly 2 stored rows and 2 client calls (today: 3/3); child-first operation order; unique ids; action applied before hashing and embedding.
  - *Fix 2/8*: nested dict/list fixtures assert exact structural paths; two identical sibling nodes get distinct IDs and rows. Repeating identical input yields identical IDs. Reordering list elements may change positional IDs and must not be tested as invariant.
  - *Fix 3*: `embed()` with root `{"t": "Section", ...}` and `embed_types={"Section"}` → root embedded exactly once; `result["i"]` equals the recomputed sha256(file_path + canonical_json).
  - *Fix 7*: root `"summary": ""` → zero root embed calls, no root row.
  - *Fix 9*: rerun unchanged input against the same fake collection keeps the same row count; then remove/change a node and assert obsolete IDs are deleted only after the replacement run succeeds.
  - *Fix 10*: inject embedding, upsert, stale-query, and delete failures. Assert the call raises, no embed artifact is presented as successful, and cleanup is never performed before all new rows exist. Where Chroma cannot make the whole replacement atomic, document the precise recoverable partial state.
- Add canonicalization regressions for an exported artifact containing pipeline `i`/`m`, an unrelated nested business object whose semantic `i`/`m` must survive, keys containing `/` or `~`, non-ASCII text, duplicate siblings, and root/node dict responses with and without a model.
- Add action regressions proving root and descendants are transformed exactly once before any hash or embedding request, and that an invalid replacement fails before persistence starts.
- tests/test_embedding_fastcore.py: mirror the new regressions as pytest functions; update metadata assertions at lines 122, 126, 227 for the new key set.
- tests/test_dom_behavior.py: update the integration assertions for structural `node_path`, `document_id`, consistent model metadata, and `file_path == str(self.md_file)`; keep record-count, document-order, and unique-ID checks where the fixture still makes those expectations exact.

## Critical files

- `nbs/01.core.dom.embedding.natural_text.ipynb` — cells 8, 13, 14, 15, 16, 17, 18 + one new regression cell (**only place to edit pipeline code**)
- `ribosome/core/dom/embedding/natural_text.py` — autogenerated target (verify after export)
- `tests/test_embedding_fastcore.py`, `tests/test_dom_behavior.py` — fakes + assertions
- Reference for metadata conventions: `ribosome/retrieval/layout_rag.py:807, 906-966`
- `ribosome/core/dom/model.py` should require no code change, but its `embed_doc` artifact-write behavior is part of the failure-path integration test.

## Verification

1. Export: `cd /d/devel/rag/ribosome && nbdev_export` (or run notebook cell 19). Confirm `ribosome/core/dom/embedding/natural_text.py` reflects cells 13/15/17 and still has the AUTOGENERATED header.
2. Notebook tests: run the repository-supported offline notebook command after inspecting the notebook for live cells; do not use a live provider as a correctness prerequisite. Record the exact command and collected result.
3. Pytest: `python -m pytest tests/test_embedding_fastcore.py tests/test_dom_behavior.py -v`, then full `python -m pytest`. Record collected counts and results.
4. Chroma integration test: use an ephemeral/scratch collection, not only the fake. Embed unchanged input twice, then changed input once. Verify exact row IDs/counts, duplicate siblings, stale-row deletion, scalar metadata, and queryability. The expected count is the number of non-empty embeddable nodes plus one only when the root summary is non-empty.
5. Failure integration: force an embedding/upsert failure through `DOMClass.embed_doc` and verify no new `_embed.json` is published. Retry successfully and confirm the collection converges without stale rows.
6. Optional live sanity: run a small asset against the configured provider and a scratch database. Record provider/model, source path, row counts, duration, and artifact validity; do not use exact vector values as assertions.


## Implementation and verification record (2026-10-09)

- The notebook owns `_semantic_payload`, canonicalization, path escaping, framed
  node IDs, action validation, persistence, and the immutable-context walk.
  Root IDs retain the original canonicalization for ordinary metadata-free input.
  Recognized old `doc_id` artifacts and new `document_id` artifacts strip their
  pipeline annotations; unrelated business `i`/`m` fields remain semantic content.
- Actions visit every dict/list container once, including list-valued properties,
  before any embedding request or hash. Scalar fields are unchanged. Invalid
  container replacements fail before collection access or persistence.
- Public helper signatures remain compatible. `cur_object_path` remains an
  ancestor-ID seed: its first item supplies document identity, additional items
  are framed into a compatibility namespace, and the sequence is never mutated.
  Standalone helpers default to structural path `$`; pipeline helpers carry the
  actual dict-key/list-index path. Missing optional provenance is omitted.
- Rows use provider-reported models with a requested-model fallback, scalar
  metadata, `document_id`, escaped structural paths, and exact source strings.
  Non-empty root summaries are embedded once. Empty summaries have no row
  annotations. Invalid/non-finite vectors and provider/storage errors propagate
  with source, document, node, and path context.
- Source-filtered old IDs are read before writes; stale deletion occurs only after
  all new upserts succeed. Chroma supports the required lookup/delete operations.
  Replacement is not an atomic transaction: failed generation/upserts can leave
  additional new rows, and a failed delete can leave incomplete cleanup. No
  successful artifact is returned in either case; retrying completes replacement.
  Existing rows lacking source provenance still require migration or a rebuild.
- All three collection fakes now replace records by ID and keep an independent
  operation log, with filtered lookup and delete. Pytest and hidden notebook
  regressions cover duplicate siblings, escaping/non-ASCII content, actions,
  exported-artifact reruns, business metadata, exact relative/absolute source
  identity, raw-dict responses, and query/provider/upsert/delete failures.
- Real ephemeral Chroma tests verify unchanged and changed reruns, exact IDs/counts,
  stale deletion, scalar metadata, optional provenance, and vector queryability.
  `DOMClass.embed_doc` failure tests verify absent artifacts remain absent and
  previous artifact bytes remain untouched; successful retries remove partial rows.
  No production model change was needed.
- Reusable development scripts are saved under `utils/`, with usage and revision
  constraints documented in `utils/README.md`.

Commands and results:

- `.venv/bin/nbdev-export --path nbs/01.core.dom.embedding.natural_text.ipynb`: successful;
  generated module retains its `AUTOGENERATED` header.
- `.venv/bin/nbdev-test --path nbs/01.core.dom.embedding.natural_text.ipynb`: successful, offline.
- `.venv/bin/nbdev-test --path nbs/01.core.dom.model.ipynb`: successful, offline.
- Focused embedding/DOM tests: 46 passed (including 28 new correctness cases).
- Full suite: 166 passed. The sandbox run stalled at the existing subprocess tests;
  the completed run used `timeout 60 .venv/bin/pytest tests/ -q --tb=short`
  outside the sandbox, as in prior full-suite verification.
- `git diff --check`: successful. Optional live-provider smoke was not run.
