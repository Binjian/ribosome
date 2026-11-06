# Notebook architecture implementation plan

## Summary and deliverables

Implement all four delivery phases from [the architecture review](notebook-architecture-review.md), preserving existing summaries, checkpoints, and index generations. This file records the approved plan, implementation status, verification, and deviations.

Implementation includes offline tests, migration tooling, and evaluation support. Live corpus processing and index activation remain opt-in. The planning baseline compiled all 604 code cells across 36 notebooks; runtime tests require dependency setup and nbdev exports.

## Architecture and compatibility decisions

- Keep authoritative nbdev notebooks, SQLite metadata, immutable local artifacts, and Chroma vectors. Export changed notebooks and synchronize tracked generated modules.
- Introduce validated `Document`, `SourceRevision`, `Node`, `SourceSpan`, `Relation`, `SummaryArtifact`, `EmbeddingArtifact`, `Publication`, and `EvidenceItem` records. Existing JSON formats remain supported through adapters.
- Persist logical document IDs independently of paths; use immutable source revisions and revision-bound occurrence IDs. Preserve legacy aliases and distinct occurrences with identical content. Moves retain identity through explicit relocation mappings.
- Keep `RESUME=True`; audit missing capabilities and summaries without invalidating documents for code/schema changes. Preserve existing object, prose, section, and document summaries and source/output/model/settings compatibility checks.
- Legacy coverage and historical image identity remain explicitly unknown. Establish current asset baselines without rebuilding or claiming historical identity; subsequent asset changes count as changed inputs.
- Readers resolve committed generations through the artifact repository. Legacy files and checkpoint formats remain recoverable compatibility projections.
- Preserve model selection and the existing code-answer mode. New retrieval representations and answer modes are explicit options until evaluated.

## Ordered implementation

### Preparation

1. **Save the plan, establish the baseline, and build the integration fixture.**
   - Set up the project environment from dependency manifests and export notebooks before runtime tests.
   - Record fresh baseline results separately from historical review results.
   - Create a synthetic manual with a merged-cell task table, exact identifiers, prose, nested list/figure, code/formula content, and source spans.
   - Include legacy completed output with missing descendants and summaries; use the fixture throughout implementation.

### Phase 1 — Correctness at existing boundaries

2. **Repair legacy orchestration and conditioning — F01, F02, F08.**
   - Remove legacy raw-AST semantic overwrites; use supported publication and `embed_doc()`.
   - Require source/configuration compatibility and artifact completeness before DOM cache reuse; verify index contents before skipping embedding.
   - Preserve recognized table-header provenance, remove each header once, and retain headerless first rows as data.
   - Correct `reorg(action=...)` forwarding and the duplicate notebook cell ID.

3. **Implement whole-document, missing-only summary work — F04, F05.**
   - Add pure `audit_completeness(document, required_capabilities)` returning present, missing, intentionally ignored, or intentionally skipped for each summary.
   - Audit objects, prose, sections, and document summaries before accepting completed checkpoints.
   - Plan dependency-ordered missing work; preserve all existing summaries exactly.
   - Share a recursive ownership visitor for blockquotes, paragraphs, lists/items, table cells, figures, code, and formulas; dispatch nested figures visually.
   - Reconcile additions using source occurrences and legacy aliases rather than extraction ordinals; preserve outputs and report ambiguous reconciliation.
   - Record preserved ancestor coverage as historical or unknown when descendants are added.

4. **Repair task retrieval and evidence validation — F03, F06, F07, F12.**
   - Normalize `_tasks.json`; adapt legacy task type to structural table plus task role.
   - Share an index manifest declaring artifact/source identity, kinds/views, encoder, and active generation. Producer/consumer defaults resolve the same selected manifest; overrides undergo preflight.
   - Build EvidenceRegistry records before context; only issued nonempty entries authorize citations.
   - Deduplicate overlapping evidence identities while retaining attributions; identical text cannot create empty evidence.
   - Derive manual/revision from cited metadata; reject invented labels and incompatible revisions.
   - Retrieve category partitions independently before applying the category bonus.

### Phase 2 — Canonical adapters and recovery

5. **Introduce canonical graph and provenance adapters — F09, F16.**
   - Adapt semantic/task JSON, Pandoc wrappers, and layout bundles to ordered ownership plus typed relations.
   - Preserve table coordinates, merged-cell origins, headers, hierarchy, exact text, and many-to-many spans.
   - Validate occurrences, ownership/order, links, spans, coordinates, revisions, and representation hashes before publication.
   - Fingerprint assets with missing/unavailable/remote-unverified states and compatible legacy adoption.
   - Record transformations and original spans; retain ambiguous continuations and margin/body matches.
   - Issue PDF coordinates only from supported original region mappings.

6. **Make publication recoverable across stores — F15.**
   - Extend immutable generations to summaries and layout indexing.
   - Stage outputs, vectors, metadata, and completeness reports; persist recovery data before switching visibility.
   - Preserve prior publication/checkpoint on preparation failure or cancellation.
   - Reconcile committed outputs and intended checkpoints before compatibility routing on restart; never repeat committed model work.
   - Treat post-commit projection synchronization as recoverable bookkeeping.
   - Pin one generation snapshot for retrieval and evidence expansion.

7. **Consolidate summary runtime and input contracts — F10 and related debt.**
   - Replace scoped function patches/global guard with explicit provider/limits/progress/persistence runtime.
   - Route natural-text entrypoints through shared traversal/repository policies, preserving call signatures.
   - Budget leaf/reduction inputs, split at semantic boundaries, and avoid duplicated full-parent input.
   - Preserve exact source fields and record coverage/input/model/prompt provenance.
   - Apply new rules only to missing work; debounce progress snapshots with final flush and bounded, awaited cancellation.
   - Measure snapshot and resume costs.

### Phase 3 — Retrieval quality

8. **Add encoder and representation contracts — F11, F14.**
   - Specify resolved model, dimension, normalization, enforced options, formatting, overflow, and input hashes.
   - Preflight pooled model content; keep routing separate from semantic identity.
   - Split supported oversized inputs or reject explicitly; prevent silent truncation.
   - Add selected contextual source vectors alongside summaries, preserving all catalog nodes without embedding every tiny cell.
   - Keep legacy contracts and publish incompatible views/encoders separately.

9. **Share retrieval, expansion, and answers — F11.**
   - Reuse exact/lexical/fusion/expansion policies from layout retrieval.
   - Apply hard corpus/revision filters before independently budgeted channels; fuse, rerank, diversify, and expand by type.
   - Pack evidence including prompt/output allowance; record retrieved/selected/expanded/omitted/cited sets.
   - Add explicit procedure, troubleshooting, comparison, and overview modes, separating revision comparisons.
   - Return supported partial answers or missing-evidence explanations; separate deterministic validation from semantic assessment.

### Phase 4 — Serving and maintenance

10. **Bound storage work and implement lifecycle management — F13.**
    - Index filter metadata and separate large blobs; preselect generations and fetch selected payloads only.
    - Batch writes and bound blocking storage workers with proper connection ownership.
    - Add authoritative corpus manifests, relocation aliases, and tombstones; failed/partial discovery never deletes.
    - Add dry-run cleanup, grace period, and active/prepared/reader/rollback protections; no automatic deletion.

11. **Complete evaluation, reporting, and operating documentation.**
    - Bind replay to generations, retrieval settings, registry schema, and encoder/view contracts.
    - Preserve historical results without citation-regex fallback.
    - Report coverage, latency, tokens, cache reuse, size, recovery, and snapshot costs.
    - Document migration, rollback, reconciliation, cleanup, and optional live evaluation.
    - Record completed steps and actual verification evidence here.

## Validation and acceptance

- Missing summary kinds are filled once; old summaries remain identical; repeated runs issue zero requests; code/schema changes never rebuild completed documents.
- Test nested objects, headerless/continued/merged tables, duplicate occurrences, aliases, and span round trips.
- Reject forged citations/revisions, empty evidence, and incompatible revision mixtures.
- Test encoder digests/options/formatting, overflow, invalid vectors, reuse, and failure.
- Inject failures/cancellation at journal/output/vector/catalog/pointer/checkpoint boundaries and test process restart without redundant requests.
- Verify concurrent snapshot consistency, bounded payload reads, and cleanup protection.
- Exercise the synthetic manual from missing-only resume through both embedding views to identifier/paraphrase retrieval and revision-consistent citations.
- Run focused suites per phase, then the full offline suite, notebook validation/compilation, export consistency, and final diff checks. Report unavailable corpus checks separately.

Future activation compares baseline/candidate on the same frozen held-out cases, separated from tuning by manual/version. Require improved evidence Recall@8 and claim-support precision, no citation/revision regressions, and p95 latency plus mean generation input/output tokens each at most **125% of baseline**. Report per-type/version results and index size.

This implementation tests the gate offline; it does not claim fresh corpus gains or activate a replacement index. Visual embeddings, cross-document clustering, remote databases, and fuzzy identity matching across arbitrary rewrites remain optional experiments.

## Execution record

| Step | Status | Verification / notes |
| --- | --- | --- |
| 1 | Complete | Plan saved; isolated test environment installed; synthetic manual/diagram and incomplete legacy-output integration fixture added. Fresh HEAD baseline: 624 passed, 6 skipped, 11 failed, 8 errors; failures/errors were stale notebook-cell selectors. |
| 2 | Complete | Legacy output publication, DOM compatibility/completion checks, action forwarding, cell IDs, and header/headerless conditioning repaired. |
| 3 | Complete | Whole-document audit, recursive semantic ownership, nested visual/code/formula coverage, alias reconciliation, and missing-only resume verified. |
| 4 | Complete | Task adapters/default manifests, category partitions, nonempty evidence registry, revision validation, and strict replay implemented. |
| 5 | Complete | Canonical document graph and semantic/layout adapters validate ownership, spans, table metadata, exact inputs, and historical provenance. Shared asset fingerprints preserve existing compatibility checks. |
| 6 | Complete | Summary journals, staged layout generations, generic repository publication, rollback, and pinned index readers verified with failures and process restart. |
| 7 | Complete | Explicit context-local runtime, shared semantic visitor, bounded input/reduction requests, provenance, debounced progress, and overlapping-writer rejection implemented. |
| 8 | Complete | Verified encoder/input contracts, source and summary views, explicit overflow handling, vector reuse, and missing-vector repair implemented. |
| 9 | Complete | Generation/view/kind filter pushdown, independently budgeted retrieval channels, parent navigation, source expansion, token packing, and explicit answer modes implemented. Live quality comparisons remain gated. |
| 10 | Complete | Indexed hot metadata, lazy payload lookup, batched vectors, storage-query workers, immutable reader pins, explicit tombstones, and protected dry-run cleanup implemented. |
| 11 | Complete | Frozen evaluation identities, offline activation gate, maintenance CLI, operating guide, and final verification recorded. |

### Final verification

- Full offline regression suite: **713 passed, 10 skipped**, in 18.61 seconds. All skips concern unavailable ignored layout bundles, source manuals/manifests, example indexes, or local manual image assets. Two dependency deprecation warnings remain (Chroma asyncio introspection and the PyMuPDF `fitz` alias).
- `nbdev-test` across the 12 changed/new core notebooks: **passed**. Application/corpus workflows were excluded. Embedded summary tests were updated for semantic ownership and the bounded provider interface.
- Compiled **610 code cells across 39 notebooks** after IPython transformation: no errors and no duplicate cell IDs.
- `nbdev-export`: passed. AST comparison verified exported function/class definitions for **13 changed/new exporting notebooks**, including the legacy runner, against generated modules.
- Synthetic vertical slice: missing nested figure repaired without repeating ancestor summaries; source/task JSON and checkpoints unchanged by indexing/retrieval; source and summary views retrieve exact identifiers and paraphrases with citations bound to the same source revision.
- Failure/concurrency checks include real subprocess death after publication, checkpoint projection retry without model calls, source/asset mismatch rejection, shared-checkpoint writer exclusion, immutable revision validation, and reader-pin protection during cleanup.
- `git diff --check`: passed. Temporary offline `.env` configuration was removed; installed dependencies remain in the ignored local `.venv`.

Tests requiring local asynchronous storage workers were executed outside the restricted runner because its thread-wakeup restrictions stall them. Model providers were fakes and test stores were temporary.

### Compatibility boundaries and follow-up operations

- Existing domain catalogs remain the authoritative publication pointers for their compatibility APIs; the shared canonical repository supplies the validated graph/artifact protocol and explicit adoption API. Migration is additive, without a bulk rewrite of corpus artifacts.
- Legacy semantic outputs without verified source provenance remain readable as historical projections. They are never attributed to current Markdown merely because that file exists. Verified stale source/asset bindings fail indexing preflight.
- Independent summary batches may run concurrently; overlapping source/checkpoint writers in the same process are rejected before provider calls. Operate one summary writer process per source/checkpoint set.
- Reader pins survive crashes conservatively. Investigate reader ownership before manually reconciling a stale pin; cleanup never deletes through an unresolved pin.
- Low-level caller-owned natural-text collectors retain their own storage guarantees. Production DOM embedding uses the new generation-aware adapter and a dedicated model-bound collection.
- Live held-out quality, corpus memory/latency measurements, and index activation were not run. The 125% latency/token gate is implemented and tested with frozen offline reports; it must pass measured evaluation before promoting a replacement index.

See [notebook-architecture-operations.md](notebook-architecture-operations.md) for migration, inspection, rollback, tombstones, cleanup, and evaluation commands. No live corpus/model workflows or index activation were performed.
