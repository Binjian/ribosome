# Processing and Recursion Refactoring in `DOMClass.textualize`

## 1. Executive Summary

This document reviews the AST summarization pipeline triggered by `DOMClass.textualize` in `ribosome.core.dom.model` (line 532) and its underlying recursive worker `summary_node_pair_async` in `ribosome.core.dom.summary` (line 270).

The `textualize` method is designed to traverse the hierarchical AST produced by `reorg`, generate LLM summaries for nodes (paragraphs, images, tables, sections), populate each node's `"s"` field, synthesize a whole-document summary, and transition the document into the `semantics_json` state required for downstream vector embedding via `embed_doc`.

A rigorous code audit uncovered critical flaws in **state management**, **dual-tree parallel traversal**, **recursive parameter dropping**, **progress counting**, and **uncontrolled LLM call explosion on leaf tokens**. This document outlines the problems and the architectural refactoring to resolve them.

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

### 3.1. Complete State Finalization in `_finalize_summary_ast_async`
Update `_finalize_summary_ast_async` to set:
```python
self.semantics_json = serialized
self.ast_json = serialized  # Preserve for backward compatibility with existing tests
if self.semantics_json_file:
    self.semantics_json_file.write_text(serialized, encoding="utf-8")
```
This ensures `embed_doc()` can seamlessly consume `self.semantics_json`.

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

### 3.4. Cumulative Progress Tracking
Support a mutable counter mapping `counters = {"table": table_count, "section": section_count}` passed through recursion so that progress logging accurately tracks cumulative table and section counts across the AST.

---

## 4. Verification and Test Matrix

1. **Lifecycle Consistency**:
   - Verify `dom.textualize()` populates `dom.semantics_json` and creates `dom.semantics_json_file`.
   - Verify `dom.embed_doc()` succeeds directly after `dom.textualize()`.
2. **Action Propagation**:
   - Verify a custom `action` function touches leaf and container nodes throughout the AST during `textualize`.
3. **Regression Safety**:
   - All 80 existing tests in `tests/` must pass without regressions.

