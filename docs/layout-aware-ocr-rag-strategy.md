# Layout-Aware OCR RAG Strategy

## Status

- Scope: OCR-processed PDF bundles containing Markdown, schema-v2 `*.layout.json`, region assets, and—when available—the source PDF.
- Reference document: `SX322023《新松机器人控制器软件指令集》(A-2)`.
- Decision: use hierarchical, multi-view retrieval. Do not concatenate the Markdown and layout JSON into one embedding.

## Executive summary

Treat each OCR output as an evidence bundle:

```text
PDF + Markdown + layout JSON + region crops
                    |
                    v
          canonical LayoutDocument
                    |
          +---------+----------+
          |                    |
   section parents      typed child chunks
                        text / table / figure
          |                    |
          +---------+----------+
                    |
        dense + lexical + visual indexes
                    |
             rank fusion + reranking
                    |
        parent/neighbor evidence expansion
                    |
          answer with page/bbox citations
```

The layout JSON is the authority for geometry, reading order, status, OCR provenance, and crop paths. The Markdown is the formatted region representation. The source PDF provides native text and page rendering when available.

## Reference-bundle audit

Source files:

- [Markdown](<../assets/unlimited_ocr_mit_coding/04指令手册/SX322023《新松机器人控制器软件指令集》(A-2).md>)
- [Layout sidecar](<../assets/unlimited_ocr_mit_coding/04指令手册/SX322023《新松机器人控制器软件指令集》(A-2).layout.json>)
- [Source PDF](<../assets/PDF-20260721/04指令手册/SX322023《新松机器人控制器软件指令集》(A-2).pdf>)

Observed characteristics:

| Property | Value |
|---|---:|
| Pages | 72 |
| Render size | 2481 x 3508 pixels at 300 DPI |
| Layout regions | 731 |
| Completed regions | 715 |
| Preserved figure regions | 11 |
| Failed regions | 5 |
| Table regions | 152 |
| Region assets | 167 |
| Bundle status | `partial` |

The 731 Markdown `layout-region` markers match the JSON records on page, index, label, bbox, and status.

### Known quality risks

- Page 53's main `IF OP AND/OR OP` table failed after reaching the OCR output-token limit.
- Page 61 contains a long, repetitive `WAIT` hallucination despite being marked `completed`.
- Page 66 contains English evaluator leakage before valid instruction text.
- Page 70 contains a failed footnote region.
- Failed page-number regions on pages 5, 7, and 9 are harmless because page identity already exists in metadata.
- OCR confusions affect exact identifiers, including `IO`, `STRLEN`, `STRREVERSE`, `SETP`, and `OUT_T`.
- The document title, footer code, and company name repeat on nearly every page.
- Most detected headings are flattened to Markdown level two. Section hierarchy must be inferred from numbering rather than Markdown heading depth alone.
- The layout sidecar still records an obsolete `/res/` source path. Stable identity must not depend on absolute paths.
- `embed_page_image` was disabled. Full-page visual retrieval must render the available source PDF rather than expecting page images in the asset bundle.

The source PDF is tagged and has a usable native text layer. Native extraction correctly recovers content on pages where OCR failed or hallucinated, so source reconciliation should precede embedding.

## Design principles

1. Preserve exact evidence. Summaries may improve recall but must never replace instruction syntax, parameter values, table cells, or source text.
2. Keep semantic content and provenance separate. Do not embed raw JSON, bbox numbers, timestamps, paths, or diagnostic messages.
3. Retrieve small child chunks for precision, then expand to their instruction or section parent for context.
4. Keep exact lexical retrieval alongside dense retrieval because robot mnemonics and parameter syntax are not safely represented by semantic similarity alone.
5. Treat tables and figures as first-class evidence types.
6. Make every answer traceable to a document hash, page, region, bbox, and source asset.
7. Quarantine suspicious OCR even when its status is `completed`.
8. Version every derived record by source content and ingestion-pipeline version.

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

### Source preference

For each text or table region:

1. Extract native PDF words intersecting the region bbox.
2. Compare native text with cleaned OCR text.
3. Prefer native text when it is spatially aligned and passes quality checks.
4. Use OCR content when the PDF has no usable text layer or when OCR preserves structure better.
5. For failed or suspicious regions, rerun OCR on the region crop, split the crop into logical tiles, or use a vision-language model.
6. Preserve all raw alternatives and record which source produced the canonical retrieval text.

For tables, native PDF text may recover words but lose row/column structure. Combine it with the crop and OCR HTML rather than blindly replacing the table.

### Quality gates

Flag or quarantine regions with:

- failed/preserved status when textual content is required;
- output-token truncation;
- excessive repeated lines, n-grams, or image tokens;
- unexpected evaluator/system-prompt language;
- unexpected language changes;
- malformed or unclosed HTML tables;
- implausible content length for the bbox;
- empty content;
- OCR/native disagreement above a configured threshold;
- extremely low detector score;
- common identifier confusions.

Keep raw text immutable. Store corrections and aliases separately, for example:

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

`BGE-M3` is appropriate for mixed Chinese/English technical content and supports dense, sparse, and multi-vector retrieval. The current Ollama embedding interface consumes only dense vectors; use a separate BM25 index for the first implementation or call FlagEmbedding directly when sparse BGE-M3 output is required.

Late chunking is optional when the chosen embedding runtime exposes long-context token embeddings. It can improve child embeddings by retaining parent context, but it must not replace structural section, instruction, or table boundaries.

## Storage architecture

### Initial implementation

- Canonical records and graph: SQLite or PostgreSQL.
- Dense child and parent vectors: a dedicated Chroma collection.
- Lexical retrieval: SQLite FTS5 or another BM25-capable index.
- Original Markdown, JSON, PDF, and crops: filesystem/object storage.
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

The UI should deep-link to the page and highlight the cited bboxes. A citation must point to evidence that directly supports the associated claim; attaching a document-level source is insufficient.

## Repository integration

The existing pipeline provides useful Markdown parsing and section reorganization, but its embedding path is not sufficient for layout-aware OCR RAG:

- [`DOMClass`](../ribosome/core/dom/model.py) parses Markdown through Pandoc and builds summarized semantic trees.
- [`embed()`](../ribosome/core/dom/embedding.py) embeds document and node summaries.
- Current Chroma metadata contains only `embed_model`.
- The persistent collection is a generic `mitochondria` collection.
- Exact retrieval, hybrid indexing, reranking, citation assembly, and OCR-quality filtering are not implemented.
- The same `ollama_model` setting is used for tasks that should have separate generation, vision, and embedding models.

Add a dedicated layout-bundle adapter before the current DOM/embedding layer. Reuse section reorganization and optional parent summarization, but keep exact region evidence and sidecar provenance in the new retrieval records.

Suggested components:

```text
LayoutBundleLoader
LayoutBundleValidator
PDFTextReconciler
OCRQualityGate
LayoutHierarchyBuilder
LayoutChunker
RetrievalRecordStore
HybridIndexer
HybridRetriever
EvidenceExpander
CitationAssembler
```

## Implementation phases

### Phase 1: canonical ingestion and repair

- Implement bundle pairing, validation, hashing, and stable IDs.
- Parse marker-delimited Markdown and join it to layout records.
- Add native PDF extraction aligned by bbox.
- Add quality flags and quarantine logic.
- Repair or exclude pages 53, 61, 66, and 70 in the reference document.

Acceptance criteria:

- all 731 regions join deterministically;
- every referenced asset resolves;
- rerunning ingestion produces identical IDs;
- known failed/hallucinated regions cannot enter the production index unnoticed.

### Phase 2: hierarchical hybrid retrieval

- Build numbered section and instruction parents.
- Generate typed child chunks.
- Index exact child content and parent summaries separately.
- Add dense Chroma retrieval and FTS5/BM25 retrieval.
- Add rank fusion, reranking, and parent expansion.

Acceptance criteria:

- exact instruction queries retrieve the correct instruction parent;
- parameter/syntax answers preserve exact source tokens;
- citations include correct page and region provenance;
- reingestion uses upsert and leaves no stale records.

### Phase 3: tables and visual retrieval

- Normalize HTML tables and create logical row-group children.
- Add table and figure crop evidence.
- Render PDF pages for a visual index.
- Add visual reranking or a ColPali-style page retriever.

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
