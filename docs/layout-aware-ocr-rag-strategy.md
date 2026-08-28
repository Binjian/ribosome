# Layout-Aware OCR RAG Strategy

## Status

- Scope: OCR-processed PDF bundles containing marker-delimited Markdown, schema-v2 `*.layout.json`, region assets, and—when available—the source PDF, plus an optional derived `*.conditioned.md` semantic view.
- Reference document: `SX322023《新松机器人控制器软件指令集》(A-2)`.
- Decision: use hierarchical, multi-view retrieval. Do not concatenate the Markdown and layout JSON into one embedding.
- Current reference state: `processed`, with zero findings from the offline OCR audit on 2026-08-28.

## Executive summary

Treat each OCR output as an evidence bundle, and separate canonical retrieval evidence from the lossy Markdown view used by the DOM semantics pipeline:

```text
PDF + canonical Markdown + layout JSON + region crops
                       |
             offline audit and repair
        native text / adaptive split / resume
                       |
              re-audited evidence bundle
                 /                 \
                v                   v
     canonical LayoutDocument    derived .conditioned.md
     retrieval and citations     DOM semantics analysis
                |
       section parents + typed children
             text / table / figure
                |
       dense + lexical + visual indexes
                |
        fusion + reranking + expansion
                |
         answer with page/bbox citations
```

The layout JSON is the authority for geometry, recorded order, status, OCR provenance, and crop paths. The canonical marker-delimited Markdown is the formatted region representation. The source PDF provides aligned native text and page rendering when available. Conditioning may correct clearly disjoint ordering inversions, but the canonical Markdown, layout JSON, source PDF, crops, and repair history remain the citation authority; conditioned Markdown does not.

## Reference-bundle audit

Source files:

- [Canonical marker-delimited Markdown](<../assets/PDF-20260721/.md_unlimited/04指令手册/SX322023《新松机器人控制器软件指令集》(A-2).md>)
- [Layout sidecar](<../assets/PDF-20260721/.md_unlimited/04指令手册/SX322023《新松机器人控制器软件指令集》(A-2).layout.json>)
- [Source PDF](<../assets/PDF-20260721/04指令手册/SX322023《新松机器人控制器软件指令集》(A-2).pdf>)
- [Derived conditioned Markdown](<../assets/unlimited_ocr_mit_conditioning/.md_unlimited/04指令手册/SX322023《新松机器人控制器软件指令集》(A-2).conditioned.md>)

These corpus assets are local and gitignored; the links require the populated workspace and will not resolve in a fresh source-only clone.

Observed characteristics:

| Property | Value |
|---|---:|
| Pages | 72 |
| Render size | 2481 x 3508 pixels at 300 DPI |
| Layout regions | 731 |
| Completed regions | 719 |
| Preserved figure regions | 11 |
| Intentionally recovered decorative regions | 1 |
| Failed regions | 0 |
| Table regions | 152 |
| Region assets | 167 |
| Bundle status | `processed` |
| Offline audit findings | 0 |

The 731 Markdown `layout-region` markers match the JSON records on page, index, label, bbox, and status.

### Completed reference repairs

The earlier `10 -> 10` / `no_progress` run was a useful stop-condition diagnosis, not the final state. It exposed a stale-checkpoint overwrite, a missing recovery-verification handler, incorrect routing of a terminal preserved region, and ineffective identical reprocessing of contaminated output. The current sidecar preserves the superseded responses in each region's `repair_history` and records the accepted repair strategy separately.

| Region(s) | Outcome |
| --- | --- |
| Pages 5, 7, 8, and 70 | Existing recovered OCR was compared with spatially aligned native PDF text and accepted at agreement `1.0`. |
| Page 9, region 5 | The empty page-number crop was intentionally discarded as decorative and recorded as `recovered`; it is not semantic content. |
| Page 53, region 4 | The token-limited table was replaced with aligned native PDF text. The proposal still records `split_and_stitch`; the native-first handler resolved it before the tile fallback ran. |
| Page 61, region 7 | Repetitive output containing `<\|det\|>` was replaced with aligned native text. |
| Page 66, region 2 | OCR-evaluator commentary was replaced with aligned native text. |

The source PDF is tagged and has a usable native text layer. Native extraction recovered usable aligned text for the reference failures, but its replacement of table OCR can lose row/column markup. The table crop therefore remains first-class evidence.

### Residual reference risks

- Audit-clean means that the implemented structural and OCR-output checks pass; it does not prove lexical correctness. Confusions such as `IO`/`I0`, `STRREVERSE`/`STREVERSE`, and `OUT_T`/`OUT T` still require aliases or source verification.
- The unconditioned canonical Markdown repeats the document title, footer code, and company name, and flattens most detected headings to Markdown level two. The conditioning pass removes the running matter and rebuilds a numbered hierarchy in a separate derived file.
- `embed_page_image` was disabled. Full-page visual retrieval must render the source PDF rather than expect page images in the asset bundle.

### Corpus repair snapshot (2026-08-28)

The audit notebook's 2026-08-24 narrative records the pre-repair state: seven partial documents, nine blocking regions among 18,986 regions, eight output-token-limit failures, and one repetition failure. Under the notebook's current root, `assets/PDF-20260721/.md_unlimited`, 163 of 163 final sidecars are now marked `processed` and no partial checkpoints remain. A full audit still finds 805 unresolved region findings across 114 processed files: 536 `recovered`, 260 `incomplete`, and 9 `suspicious_completed`. Therefore `processed` must never be treated as synonymous with audit-clean. With `partial_documents_only=True`, `stop_reason="clean"` means only that no partial documents remain in the filtered queue; always run an unfiltered `audit_layout_ocr_results()` or `collect_ocr_repair_queue()` afterward.

`assets/unlimited_ocr_mit_original` is a distinct historical repaired snapshot and is not the source for these counts or the conditioned reference view. In particular, its page-53 reference table records a two-tile split, whereas the current `.md_unlimited` bundle records the native-PDF replacement described above.

## Design principles

1. Preserve exact evidence. Summaries may improve recall but must never replace instruction syntax, parameter values, table cells, or source text.
2. Keep semantic content and provenance separate. Do not embed raw JSON, bbox numbers, timestamps, paths, or diagnostic messages.
3. Retrieve small child chunks for precision, then expand to their instruction or section parent for context.
4. Keep exact lexical retrieval alongside dense retrieval because robot mnemonics and parameter syntax are not safely represented by semantic similarity alone.
5. Treat tables and figures as first-class evidence types.
6. Make every answer traceable to a document hash, page, region, bbox, and source asset.
7. Quarantine suspicious OCR even when its status is `completed`.
8. Version every derived record by source content and ingestion-pipeline version.
9. Keep conditioned Markdown as a replaceable semantic projection; never use it as the sole source for exact retrieval or citations.

## Canonical bundle ingestion

### Pair the artifacts

Pair the following by relative stem:

```text
<stem>.pdf
<stem>.md
<stem>.layout.json
<stem>.assets/
```

Validate:

- `schema_version` is supported;
- page and region identifiers are unique and ordered;
- every JSON asset exists;
- every Markdown marker resolves to exactly one JSON region;
- page dimensions and bboxes are valid;
- bundle and region statuses are known;
- the source PDF matches the expected size when available.

Use SHA-256 of the PDF bytes as the primary `document_id`. If the PDF is unavailable, hash the canonicalized layout JSON plus Markdown. Record separate hashes for every source artifact.

Stable region identifiers should be independent of paths:

```text
{document_id}:p{page_number:04d}:r{region_index:04d}
```

### Join Markdown and layout records

Join on:

```text
(page_number, region.index)
```

For every region, retain:

- cleaned JSON `content`;
- JSON `raw_content`;
- marker-delimited Markdown payload;
- page dimensions and pixel bbox;
- normalized bbox coordinates;
- label, task type, detector score, and OCR status;
- OCR provider/model and layout model;
- optional crop asset;
- previous/next region links;
- active section path.

Normalize bbox coordinates as:

```text
[x1 / page_width, y1 / page_height, x2 / page_width, y2 / page_height]
```

When mapping to PDF coordinates, account for page rotation and scale normalized coordinates to the PDF page rectangle.

## Text reconciliation and quality control

### Implemented offline audit

Run `audit_layout_ocr_file()` or `audit_layout_ocr_results()` before strict conditioning or indexing. The audit is deterministic and makes no OCR or network calls. When scanning a tree, it prefers a published final sidecar over its sibling `.partial.layout.json`; an orphan partial checkpoint remains auditable.

Recorded status is not trusted on its own. The implemented audit checks:

- document/page status, recorded errors, empty non-figure content, and abnormal `finish_reason` values;
- missing or duplicate region indexes, invalid/out-of-page bboxes, and missing crop assets;
- unbalanced HTML tables and known leaked model control tokens;
- evaluator leakage when at least two known commentary markers occur;
- placeholder floods of at least 20 image/page tokens;
- general repetition at 12 Markdown fences, 8 identical non-empty lines, or 8 occurrences of a sliding 32-character block; and
- table repetition at 25 identical non-empty lines.

Intentionally preserved figures do not enter the repair queue when their geometry and asset are valid and no error is recorded. Empty recovered decorative headers, footers, or page numbers are likewise skipped only when their discard metadata and asset state are valid. `collect_ocr_repair_queue()` filters reports with findings, while `propose_ocr_repairs()` deterministically maps every file and region finding to an action. No finding is silently dropped.

### Source preference

For each text or table region:

1. Extract native PDF words intersecting the region bbox.
2. Compare native text with cleaned OCR text.
3. Prefer native text when it is spatially aligned and passes quality checks.
4. Use OCR content when the PDF has no usable text layer or when OCR preserves structure better.
5. For failed or suspicious regions, rerun OCR on the region crop, split the crop into logical tiles, or use a vision-language model.
6. Preserve superseded responses in `repair_history` and record which source produced the canonical retrieval text.

For tables, native PDF text may recover words but lose row/column structure. Combine it with the crop and OCR HTML rather than blindly replacing the table.

### Bounded repair execution

The executable repair handler uses this order for each sidecar:

1. For `verify_recovery`, `discard_and_reprocess`, and split actions, try spatially aligned native PDF text first.
2. Re-audit the sidecar.
3. Adaptively split and stitch remaining split proposals, including token-limited or malformed-table output.
4. Re-audit again, then stage a checkpoint resume for remaining supported retry, crop, resume, or decorative-review actions.
5. Publish the sidecar and reconstructed Markdown as a staged pair, re-audit, and rebuild the remaining proposal queue.

Native verification uses a default normalized-text agreement threshold of `0.65`. Only `verify_recovery` retains existing OCR when it meets that threshold; other selected actions replace it with valid aligned native text. The handler records the strategy, agreement, source, and page in `native_text_repair`.

Adaptive splitting defaults to a 1,400-pixel tile height, 96-pixel overlap, a 128-pixel low-ink boundary search, a 320-pixel minimum tile height, and a maximum recursion depth of 4. A tile that is truncated or fails the same quality checks is recursively divided. Every fragment and the stitched result must validate before publication. Table stitching removes overlapping HTML rows and records tile bounds, split depth, finish reason, response ID, usage, and elapsed time in `split_repair`.

If `.layout-work/checkpoint.json` exists, it is the authoritative repair state. Staging verifies a relocated source against its recorded size and nanosecond mtime, clones the assets when it creates a workspace, and retains `repair_checkpoint` while the document is partial. Pair publication uses staged renames plus best-effort rollback rather than a cross-file transaction; after promotion to `processed`, it removes the workspace and partial artifacts. Source identity for ingestion still requires a cryptographic hash because checkpoint relocation checks are not content hashes.

Native replacement, split repair, and selective reopening of terminal regions append the previous response to `repair_history`. Generic failed-region retry or document resume does not yet guarantee the same archival step, so production hardening must make repair-history preservation uniform across every mutating handler.

Handlers are grouped per file so aliases do not cause duplicate OCR calls. Dry runs preview dispatch without invoking handlers. After each round, execution stops with one of `clean`, `dry_run`, `no_handlers`, `execution_failed`, `no_progress`, or `max_rounds`. Unsupported structural changes and non-decorative human-review findings remain queued for a purpose-built handler.

The native-first order above is the opt-in `run_checkpoint_repairs()` composition in the notebook, not one exported orchestration function. The module exports the audit, planning, native/split/staging primitives and the handler-driven bounded executor so applications can register an equivalent or different policy.

### Additional production gates

The following checks are not implemented by the offline audit itself:

- unexpected language changes;
- OCR/native disagreement outside explicit repair actions;
- implausible content density for the bbox;
- extremely low detector scores; and
- likely identifier confusions.

The canonical layout-bundle ingestion gate already emits warning flags for detector scores below `0.3` and known identifier aliases. The other checks remain production extensions.

Two release modes are intentional. Strict release blocks on any offline-audit finding, as `condition_markdown_file()` and the example indexing gate do. Controlled canonical ingestion may retain warning-severity regions with their flags while quarantining unresolved error-severity regions. A derived conditioned view may be used only after that canonical severity-aware gate; it cannot reconstruct region quarantine from its own text.

Production requirement: never discard prior evidence. A repair may update the active `content` and `raw_content`, but it must first append the previous status, raw/content text, error, recovery, finish reason, and proposal reasons to `repair_history`. Store lexical corrections and aliases separately, for example:

```json
{
  "raw_term": "SIRLEN",
  "canonical_term": "STRLEN",
  "reason": "native PDF and document instruction index agree"
}
```

## Hierarchy construction

Build a document graph:

```text
Document
└── numbered chapter
    └── numbered subsection
        └── instruction/semantic parent
            ├── prose child
            ├── syntax/parameter child
            ├── table child
            └── figure child
```

Derive hierarchy from normalized numeric prefixes such as `4.4.1`, including variants with inserted spaces such as `4. 4. 4`. Maintain the active section across page boundaries so a table at the top of a new page remains attached to the preceding instruction.

Use layout labels, geometry, and nearby titles as fallbacks. Treat recurring document titles as page headers rather than semantic headings.

## Semantics-ready Markdown conditioning

Conditioning is an optional, deterministic local branch for `DOMClass` and `analyze_one_document_async`. It is not a prerequisite for `LayoutDocument` ingestion and does not replace canonical region evidence. It treats the canonical marker-delimited Markdown and schema-v2 layout sidecar as read-only inputs and normally writes a sibling `<stem>.conditioned.md` so retained relative image links continue to resolve.

### Deterministic transformation

The implemented pass:

1. Joins Markdown markers to layout records and, in strict mode, requires exact page/index, label, status, bbox, and key-set agreement.
2. Removes explicit `header`, `footer`, and `number` regions; top/bottom running matter repeated on at least `max(3, ceil(20% of pages))` pages; recurring document titles; and boundary boilerplate derived from those labels and the layout `source` document code/revision.
3. Drops non-table regions geometrically nested inside table regions and reorders only vertically disjoint inversions, preserving index order for overlapping regions.
4. Reconstructs numbered headings from the TOC, title similarity, and active accepted parents. Heading level comes from numbering depth. Visible numbers are stripped by default but retained in `section-number`, source-page, and source-region comments; title-like regions that cannot be trusted as headings are demoted to bold or plain text.
5. Reflows safe plain-text lines and joins prose across a page boundary only when geometry, content type, punctuation, list, and block guards agree.
6. Reconstructs tables from sidecar content, removes duplicate crop/nested-region renderings, joins across pages only when the previous page's last semantic region and the next page's first semantic region are tables at the configured margins, deduplicates repeated headers, absorbs leading continuation rows, and emits pipe Markdown chunks with repeated headers and logical-table/page/fragment/asset provenance comments.
7. Retains surviving non-nested figure payloads with page, region, and asset comments.

The defaults are a 20% repeated-page threshold with a three-page minimum, 12% page margins, 78%/22% bottom/top thresholds for cross-page table continuation, `0.55` TOC/title similarity, table-splitting targets of 12 rows or approximately 2,600 body characters, stripped visible section numbers, bold unnumbered titles, provenance comments enabled, and strict validation.

This view is intentionally lossy: original page/region markers and raw HTML tables are removed, ordinary prose does not carry complete bbox provenance, and table crop links are represented in comments rather than as duplicate visible images. The canonical layout records must still retain original HTML, crops, raw region payloads, and stable IDs. Conditioning table chunks are for semantic/DOM structure; they are not the retrieval children defined below.

### Validation and batch contract

`condition_markdown_file()` first runs `LayoutBundleValidator` and the offline OCR audit. Strict mode refuses unresolved audit findings or structural validation failures. It then validates the result through Pandoc: no raw HTML tables, Pandoc-parsed heading counts matching the generated report, no level jumps, a level-one first heading when headings exist, numberless headings when configured, and resolvable recognized local image links. It refuses to overwrite the source and, unless `overwrite=True`, an existing output.

`condition_layout_markdown()` is the in-memory transformation only; callers that use it directly do not receive the file-level audit gate. `condition_markdown_folder()` recursively discovers sorted source Markdown, ignores generated `.conditioned.md` files, requires a sibling sidecar, skips existing outputs by default, and continues after per-file failures by default. Its item statuses are `conditioned`, `conditioned_with_warnings`, `missing_layout`, `skipped_existing`, and `failed`. Progress is shown by default, and caught errors are printed immediately through the progress display.

The example corpus runner deliberately sets non-strict mode. When a non-strict item has audit findings, they are copied into the report as `OCR audit: ...` warnings and its status is `conditioned_with_warnings`; an audit-clean, eligible item can still be `conditioned`. A warned output is not production-approved merely because it was written, and `conditioned_with_warnings` can also reflect failed eligibility. Consumers must inspect `eligible`, `validation_errors`, and `warnings`. Currently, non-strict `LayoutBundleValidator` issues are not merged into those warnings, so callers must validate the canonical bundle separately; surfacing those tolerated structural issues in the report remains hardening work. Existing-output skipping has no hash-based staleness check, so production orchestration must version or regenerate conditioned views when the source or configuration changes.

The current reference conditioned file exactly matches a fresh in-memory pass and is Pandoc-eligible:

| Conditioning property | Value |
| --- | ---: |
| Input regions | 731 |
| Removed explicit-label regions | 210 |
| Removed recurring running titles | 71 |
| Safe geometry reorderings | 2 |
| Cross-page prose joins | 1 |
| Input table fragments | 152 |
| Cross-page table links | 42 |
| Multi-fragment logical table sequences | 37 |
| Output Markdown table chunks | 153 |
| Reconstructed headings | 95 (`4 / 14 / 77` at levels 1 / 2 / 3) |
| Retained images | 4 |
| Eligibility errors or warnings | 0 |

The canonical retrieval flow resumes below; its chunk sizes and evidence contract are independent of the derived conditioning chunks.

## Chunking rules

### Parents

- Prefer one complete instruction or numbered section as a parent.
- Permit parents to span pages.
- Keep function, format, parameter definitions, constraints, examples, and related notes under the same parent.
- If a parent becomes too large, split around 1,200–2,000 model tokens at semantic boundaries and retain a shared section parent.

### Prose children

- Start with approximately 350–700 embedding-model tokens.
- Merge adjacent prose regions only when they share the same section and content type.
- Never cross a numbered instruction boundary.
- Prefer sentence/paragraph boundaries over fixed character windows.
- Use limited neighbor expansion at retrieval time instead of large arbitrary overlaps.

### Table children

- Preserve original HTML and crop path.
- Expand `rowspan` and `colspan` into an explicit logical grid.
- Generate a canonical text view containing caption, headers, row labels, and values.
- Keep small tables atomic.
- Split large tables by semantic row groups, repeating headers in every child.
- Keep groups such as `功能`, `格式`, `参数`, `说明`, and `举例` together where possible.
- Protect literal tokens such as `<参数 1>` from generic HTML stripping.

Example retrieval representation:

```text
指令: MOVJ
功能: 两点之间以关节插补方式进行运动
格式: MOVJ P<参数1> V=<参数2> ACC=<参数3> CNT=<参数4>
参数2: 关节运动速度，范围 1–100%
```

### Figures

- Associate each figure with its nearest caption and active section.
- Store its crop, caption, nearby explanatory text, and optional visual description.
- Do not index decorative images independently.
- Detect nested figures geometrically. The page-17 subfigures should remain children of their surrounding table, not independent search results.

### Boilerplate and TOC

- Exclude labels `header`, `footer`, and `number` from semantic embeddings by default.
- Exclude highly repeated page-title text from semantic embeddings.
- Retain all excluded regions as provenance metadata.
- Tag table-of-contents chunks as `content_type=toc` and down-rank or filter them for ordinary answer retrieval.

These are canonical retrieval rules. The conditioner uses TOC text to infer headings but does not itself add retrieval-specific TOC tags or filters.

## Retrieval record schema

Each searchable child should resemble:

```json
{
  "chunk_id": "sha256:...:p0042:r0008-0012",
  "parent_id": "sha256:...:section:4.4.1",
  "document_id": "sha256:...",
  "document_title": "新松机器人控制器软件指令集",
  "document_code": "SX322023",
  "revision": "A/2",
  "section_path": "4 基础指令 > 4.4 运动指令 > 4.4.1 MOVJ",
  "instruction_code": "MOVJ",
  "content_type": "table_rows",
  "retrieval_text": "...",
  "raw_markdown": "...",
  "page_start": 42,
  "page_end": 43,
  "region_ids": ["p0042:r0008", "p0043:r0004"],
  "bboxes_normalized": [[0.18, 0.31, 0.85, 0.62]],
  "region_labels": ["paragraph_title", "table"],
  "ocr_status": "completed",
  "quality_flags": [],
  "text_source": "native_pdf+ocr_html",
  "asset_paths": ["...png"],
  "source_pdf_uri": "...pdf",
  "content_sha256": "...",
  "embedding_model": "...",
  "pipeline_version": "..."
}
```

If the vector store accepts only scalar metadata, serialize lists as compact JSON or keep them in a companion relational/document store keyed by `chunk_id`.

`raw_markdown` must refer to the canonical marker-delimited payload, not the conditioned projection. If a conditioned view contributes parent context or a summary, store its content hash, configuration/pipeline version, and report separately so the projection can be invalidated without changing evidence IDs.

## Embedding strategy

Do not embed the entire bundle. Index several representations:

1. Exact child content with the full section breadcrumb.
2. Parent/instruction summary for broad semantic recall.
3. Exact identifiers and canonical aliases in a lexical/sparse index.
4. Table schema/row representations.
5. Optional page/crop visual multi-vectors.

Recommended dense input:

```text
文档: 新松机器人控制器软件指令集 A/2
章节: 4 基础指令 > 4.4 运动指令 > 4.4.1 MOVJ
类型: 指令格式与参数
关键词: MOVJ, 关节运动
正文: ...
```

Do not include bboxes, hashes, absolute paths, timestamps, detector diagnostics, or raw JSON syntax in this text.

Conditioned Markdown may supply cleaner document-level context for parent summaries, but exact child text must still be recoverable from canonical regions. A warned projection must never bypass canonical region-level quarantine or serve as exact child evidence; use it only as derived parent context after the canonical bundle is gated.

`BGE-M3` is appropriate for mixed Chinese/English technical content and supports dense, sparse, and multi-vector retrieval. The current Ollama embedding interface consumes only dense vectors; use a separate BM25 index for the first implementation or call FlagEmbedding directly when sparse BGE-M3 output is required.

Late chunking is optional when the chosen embedding runtime exposes long-context token embeddings. It can improve child embeddings by retaining parent context, but it must not replace structural section, instruction, or table boundaries.

## Storage architecture

### Initial implementation

- Canonical records and graph: SQLite or PostgreSQL.
- Dense child and parent vectors: a dedicated Chroma collection.
- Lexical retrieval: SQLite FTS5 or another BM25-capable index.
- Original Markdown, JSON, PDF, crops, repair history, conditioned views, and conditioning reports: filesystem/object storage.
- Rank fusion and reranking: application layer.

Use `upsert`, versioned collections, and delete-by-document/version. Reingestion must be idempotent.

### Scaled implementation

For larger corpora or visual multi-vector retrieval, use a store that supports hybrid sparse/dense and multi-vector indexing, while retaining the canonical document graph outside the vector index.

## Query and retrieval flow

Recommended starting configuration:

1. Normalize the query while preserving exact identifiers.
2. Run dense child retrieval, initially top 30.
3. Run BM25/sparse retrieval, initially top 30.
4. Run visual retrieval, initially top 10, for table/figure/layout-sensitive queries.
5. Fuse rankings with reciprocal-rank fusion.
6. Rerank the top 20–30 candidates with a multilingual cross-encoder or visual reranker.
7. Select approximately 5–8 child hits with parent/page diversity.
8. Expand each hit to its instruction parent and bounded neighboring regions.
9. Supply exact evidence—not summaries alone—to the generation model.
10. Produce claim-level citations with PDF page, region IDs, bboxes, and crop links.

Use lexical weighting or query routing for mnemonics and syntax such as:

```text
STRFINDEND
OUT_T
PR[].x
MOVJ P[1] V=10 ACC=100 CNT=100
```

For conceptual questions, increase dense weight. For visually grounded questions, include page/crop visual candidates and pass the selected image evidence to a vision-language model.

## Citation contract

Every retrieved evidence object must support:

- immutable document ID and source hash;
- document code and revision;
- page number;
- one or more region IDs;
- raw and normalized bboxes;
- source text span;
- original PDF/Markdown URI;
- optional crop URI;
- OCR status and quality flags.

Example answer citation:

```text
[SX322023 A/2, p.43, regions 4–11]
```

The UI should deep-link to the page and highlight the cited bboxes. A citation must point to evidence that directly supports the associated claim; attaching a document-level source is insufficient. A conditioned heading, table comment, or prose span may help organize evidence, but it cannot replace the raw region IDs and bboxes in the citation.

## Repository integration

Before the adapter implemented below, the existing DOM pipeline provided useful Markdown parsing and section reorganization, but its embedding path was not sufficient for layout-aware OCR RAG:

- [`DOMClass`](../ribosome/core/dom/model.py) parses Markdown through Pandoc and builds summarized semantic trees.
- [`embed()`](../ribosome/core/dom/embedding.py) embeds document and node summaries.
- Current Chroma metadata contains only `embed_model`.
- The persistent collection is a generic `mitochondria` collection.
- That generic path does not itself implement exact retrieval, hybrid indexing, reranking, citation assembly, or OCR-quality filtering.
- The same `ollama_model` setting is used for tasks that should have separate generation, vision, and embedding models.

Use the dedicated audit/repair and layout-bundle path before retrieval indexing. The optional conditioner makes OCR Markdown suitable for the existing DOM semantics path, but exact region evidence and sidecar provenance stay in canonical retrieval records.

Suggested components:

```text
LayoutBundleLoader
LayoutBundleValidator
OCRAudit
OCRRepairPlanner
OCRRepairExecutor
PDFTextReconciler
OCRQualityGate
MarkdownConditioner
LayoutHierarchyBuilder
LayoutChunker
RetrievalRecordStore
HybridIndexer
HybridRetriever
EvidenceExpander
CitationAssembler
```

## Implemented repository API

The preprocessing and retrieval path is implemented in four notebook-first modules:

- [`03.preprocessing.ocr.audit_repair.ipynb`](../nbs/03.preprocessing.ocr.audit_repair.ipynb) exports `ribosome.preprocessing.ocr.audit_repair`. It provides deterministic tree/file audits, complete repair proposals, native-PDF verification and replacement, adaptive split-and-stitch, durable checkpoint staging, bounded repair rounds, dry runs, live progress reports, and mandatory re-auditing.
- [`02.preprocess.conditioning.markdown.ipynb`](../nbs/02.preprocess.conditioning.markdown.ipynb) exports `ribosome.preprocessing.conditioning.markdown`. It provides the optional derived semantic Markdown view, Pandoc eligibility checks, strict audit-gated single-file conditioning, and recursive batch conditioning, including an explicitly non-strict corpus runner with warnings and per-file outcomes.
- [`03.preprocessing.ocr.layout_bundle.ipynb`](../nbs/03.preprocessing.ocr.layout_bundle.ipynb) exports `ribosome.preprocessing.ocr.layout_bundle`. It provides strict bundle validation, stable hashing and IDs, Markdown/JSON joining, OCR audit gates, native-PDF reconciliation, numbered hierarchy construction, table normalization, typed chunks, page rendering, and crop discovery.
- [`04.retrieval.layout_rag.ipynb`](../nbs/04.retrieval.layout_rag.ipynb) exports `ribosome.retrieval.layout_rag`. It provides the canonical SQLite graph, FTS5 and exact alias indexes, separate optional Chroma child/parent collections, reciprocal-rank fusion, injectable visual retrieval and reranking, parent/neighbor expansion, citations, and retrieval metrics.

Current implementation boundary:

| Phase | Status | Included now |
| --- | --- | --- |
| 1. Audit, repair, conditioning, and canonical ingestion | Implemented | Offline audit/proposals, exported native/split/staging primitives, a bounded handler executor, an opt-in native-first notebook composition, checkpoint-aware staged publication, strict pairing/validation, stable IDs and hashes, marker joins, optional semantics conditioning, native-PDF reconciliation, quarantine, and reference-document repairs. |
| 2. Hierarchical hybrid retrieval | Implemented | Hierarchy and typed chunks, SQLite FTS/exact retrieval, optional explicit-vector Chroma indexes, rank fusion, parent expansion, versioned upserts, and citations. |
| 3. Tables and visual retrieval | Adapter implemented | Table row groups, crop discovery, PDF page rendering, and visual-candidate fusion are included; a particular ColPali/vision model and its index are intentionally deployment choices. |
| 4. Evaluation and tuning | Foundation implemented | Recall, MRR, and abstention metrics are included; the representative labelled dataset, ablation runs, answer grading, and UI remain application work. |

Audit and lexical indexing need no embedding service. Gate the raw evidence bundle before ingestion. The example uses top-level `await` as supported by Jupyter; a synchronous script can wrap the indexing call with `asyncio.run(...)`:

```python
from ribosome.preprocessing.ocr.audit_repair import audit_layout_ocr_file
from ribosome.preprocessing.ocr.layout_bundle import ingest_layout_bundle
from ribosome.retrieval.layout_rag import (
    HybridIndexer,
    HybridRetriever,
    SQLiteRetrievalRecordStore,
)

audit = audit_layout_ocr_file("document.layout.json")
if audit.needs_repair:
    raise ValueError(
        f"OCR audit has {len(audit.file_reasons)} file findings and "
        f"{len(audit.region_issues)} region findings"
    )

result = ingest_layout_bundle(
    "document.layout.json",
    markdown_path="document.md",
    pdf_path="document.pdf",  # explicit path overrides obsolete sidecar paths
)

store = SQLiteRetrievalRecordStore("layout-rag.sqlite3")
await HybridIndexer(store).index(result)
evidence = HybridRetriever(store).retrieve_evidence(
    "MOVJ P[1] V=10 ACC=100 CNT=100"
)
```

Condition only when the DOM semantics path needs a document-shaped Markdown view:

```python
from ribosome.preprocessing.conditioning.markdown import condition_markdown_file

conditioned = condition_markdown_file(
    "document.md",
    layout_path="document.layout.json",
)
# Pass conditioned.output_path to DOMClass/analyze_one_document_async.
# Continue to use the raw bundle for retrieval records and citations.
```

Dense indexing remains explicit and cannot silently download Chroma's default embedding model:

```python
import chromadb
from ollama import AsyncClient
from ribosome.retrieval.layout_rag import ChromaDenseIndex, OllamaEmbeddingProvider

dense = ChromaDenseIndex(
    chromadb.PersistentClient(path="chroma/layout-rag"),
    embedding_model="bge-m3",
)
embedder = OllamaEmbeddingProvider(AsyncClient(), model="bge-m3")
await HybridIndexer(store, dense).index(result, embedder=embedder)

query_vector = (await embedder(["MOVJ 的参数范围是什么？"]))[0]
evidence = HybridRetriever(store, dense_index=dense).retrieve_evidence(
    "MOVJ 的参数范围是什么？",
    query_embedding=query_vector,
)
```

`embedding_model` is required because it identifies the vector space. Chroma collection names include a digest of the raw pipeline version and embedding-model identity, and every query also filters those metadata fields. Per-document dense replacement snapshots and restores the prior Chroma records if an upsert/delete fails; the enclosing SQLite transaction is rolled back at the same time. A rollback failure is surfaced as an error instead of being silently accepted.

For visual retrieval, `PDFPageRenderer` materializes missing page images and `collect_region_visual_assets()` returns table/figure crops. Supply a visual candidate function to `HybridRetriever`; the repository intentionally does not impose a particular ColPali or vision model.

Production indexing excludes quarantined, boilerplate, nested-figure, and TOC children by default. Native-PDF repairs can make a suspicious region indexable again, but superseded OCR must remain in the sidecar's `repair_history` and canonical provenance. A `conditioned_with_warnings` file must not bypass this production gate.

## Implementation phases

### Phase 1: audit, repair, conditioning, and canonical ingestion

Completed capabilities:

- pair and validate bundles, hash their artifacts, and assign stable IDs;
- parse marker-delimited Markdown and join it to layout records;
- audit recorded OCR independently and retain a deterministic repair queue;
- provide aligned-native, adaptive-split, checkpoint-staging, and re-audit primitives, with an opt-in notebook composition for the current provider;
- reconstruct an optional, validated semantic Markdown view without changing canonical evidence; and
- quarantine unresolved findings and preserve repair provenance for native, split, and selective terminal-region repairs.

Remaining operational work:

- review or repair unresolved findings in already processed corpus documents;
- add purpose-built handlers for structural manifest/layout changes and non-decorative manual review;
- make `repair_history` archival uniform for generic failed-region retry and document-resume paths;
- merge non-strict bundle-validation issues into conditioning reports; and
- add cryptographic source verification to checkpoint relocation, which currently checks size and nanosecond mtime.

Acceptance criteria:

- all 731 reference regions join deterministically and every referenced asset resolves;
- the repaired reference sidecar has zero offline-audit findings;
- a fresh reference conditioning pass exactly matches the stored output and is Pandoc-eligible;
- rerunning ingestion produces identical IDs; and
- known failed, suspicious, recovered, or warned regions cannot enter the production index unnoticed.

### Phase 2: hierarchical hybrid retrieval

- Implemented: numbered section/instruction parents, typed child chunks, exact child and parent-summary separation, FTS5/exact retrieval, optional explicit-vector Chroma retrieval, reciprocal-rank fusion, and parent/neighbor expansion.
- Deployment choice: provide and tune the dense model and optional reranker rather than silently selecting or downloading one.

Acceptance criteria:

- exact instruction queries retrieve the correct instruction parent;
- parameter/syntax answers preserve exact source tokens;
- citations include correct page and region provenance;
- reingestion uses upsert and leaves no stale records.

### Phase 3: tables and visual retrieval

- Implemented adapter: normalized HTML tables, logical row-group children, table/figure crop discovery, PDF page rendering, and injectable visual-candidate fusion.
- Deployment choice: add and evaluate the visual encoder/index and visual reranker or ColPali-style page retriever.

Acceptance criteria:

- table questions retrieve the correct row group and crop;
- figure questions return the relevant page/figure evidence;
- nested images do not create duplicate or context-free results.

### Phase 4: evaluation and tuning

Build a representative evaluation set containing:

- exact mnemonic lookups;
- instruction format and parameter questions;
- conceptual Chinese queries;
- cross-page instruction questions;
- table cell/row questions;
- figure/layout questions;
- revision/version questions;
- questions whose answer lies in a repaired region;
- unanswerable questions.

Measure:

- child-region recall@k;
- parent/page recall@k;
- MRR or nDCG;
- table and identifier exact match;
- answer correctness and faithfulness;
- citation correctness, completeness, and bbox accuracy;
- latency and storage cost;
- abstention accuracy for missing/low-quality evidence.

Ablate:

1. dense only;
2. sparse only;
3. dense + sparse;
4. hybrid + reranker;
5. hybrid + parent expansion;
6. OCR only versus OCR/native reconciliation;
7. text only versus text + visual retrieval;
8. conventional versus late chunk embeddings where supported.

The final configuration should be chosen from evaluation results rather than assumed to be universally optimal.

## External references

- [Chunk and vectorize by document layout — Microsoft](https://learn.microsoft.com/en-us/azure/search/search-how-to-semantic-chunking)
- [Retrieval-Augmented Generation with Document Intelligence — Microsoft](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/retrieval-augmented-generation?view=doc-intel-4.0.0)
- [BGE-M3](https://arxiv.org/abs/2402.03216)
- [Late Chunking](https://arxiv.org/abs/2409.04701)
- [ColPali](https://arxiv.org/abs/2407.01449)
- [HYRR: Hybrid Infused Reranking](https://arxiv.org/abs/2212.10528)
- [ALCE: Benchmarking Citation Quality](https://arxiv.org/abs/2305.14627)
