# Document architecture operations

The [implementation plan](notebook-architecture-implementation-plan.md) tracks delivery and verification. Notebooks remain the source of production code; run `nbdev-export` after editing them. The new canonical document and artifact contracts are exported as `ribosome.core.document` and `ribosome.core.artifacts`; retrieval contracts are in `ribosome.retrieval.contracts`.

## Existing documents and checkpoints

Keep `RESUME=True` in the object-summary application. A compatible completed document is audited for missing object, prose, section, and document summaries. Only missing work is requested. Saved ancestor summaries remain unchanged even when new descendants are discovered; historical coverage is recorded rather than inferred.

Source/output/model/settings compatibility continues to matter. Code and schema changes alone are capability upgrades. Older image fingerprints and summary prompt coverage are unknown. First adoption records the currently observed assets without claiming that they produced the historical summaries. Later asset changes are changed inputs.

Summary publication writes recovery information before replacing outputs. Recovery runs before checkpoint compatibility routing. Committed work is reconciled instead of requested again. Failed preparation retains the old completed output/checkpoint. Prepared partial work is distinct from committed work.

## Canonical migration and inspection

Run these commands from the repository root. Preview adoption validates a semantic artifact without writing a repository or touching the source:

```sh
.venv/bin/python -m utils.architecture_maintenance --repository /tmp/artifact-pilot adopt /path/manual_semantics.json --source /path/manual.md --corpus pilot
```

Add `--apply` only when adopting the reviewed input. Adoption preserves source JSON and all saved summary text; it does not call a model. The repository assigns a persistent logical document ID. Reuse it with `--document-id` or `relocate_document()` when explicitly relocating an input. Identical content in different files is not evidence that they are the same logical document.

```sh
.venv/bin/python -m utils.architecture_maintenance --repository /tmp/artifact-pilot status
.venv/bin/python -m utils.architecture_maintenance --repository /tmp/artifact-pilot recover
.venv/bin/python -m utils.architecture_maintenance --repository /tmp/artifact-pilot rollback DOCUMENT_ID GENERATION
```

Rollback validates the target and previews by default; add `--apply` to switch the pointer. Recovery verifies blob hashes and identifies prepared generations; it never automatically promotes incomplete preparations. Layout and object indexes retain their own generation catalogs behind the same staged-publication contract. A canonical graph adoption does not activate a vector index.

## Index and retrieval contracts

Embedding and RAG applications resolve the shared task/object index default. An explicit `OBJECT_RAG_INDEX` remains supported and must match the catalog, collection, and encoder manifest. Legacy task JSON is read as a table with a task role; completed summaries do not need rewriting.

Existing summary-only indexes retain their original contract. New source views require an explicit `EncoderSpec` and a separate compatible index configuration. The specification records model digest, dimension, actual provider options, query/document templates, normalization, and input limits. The default token estimator uses a conservative UTF-8 byte bound; no silent provider truncation is allowed. Endpoint routing is operational metadata rather than model identity.

Use `representations=('summary', 'source')` on `ObjectEmbeddingIndex` with that encoder specification, and `retrieval_mode='hybrid'` on `ObjectRAG` to opt into multiple representations and exact/lexical fusion. Keep the current index available until the evaluation gate passes. Code answers remain the default; procedure, troubleshooting, comparison, and overview answers are explicit modes.

Every answer citation is resolved against its issued evidence registry. Literal `[E999]` in a source does not issue a citation. Context-only historical replay is inspectable but cannot authorize generated citations. Displayed revision metadata comes from cited sources. Deterministic response validation does not certify claim support or executable code correctness.

Production natural-text embedding uses a generation-aware adapter and a dedicated model-bound cosine collection. Low-level helpers still accept caller-owned legacy collectors for compatibility; arbitrary external collectors do not acquire transactional publication guarantees merely by using the helper.

## Tombstones, retention, and readers

Corpus synchronization requires a complete authoritative manifest with explicit tombstones:

```json
{
  "corpus_id": "pilot",
  "document_ids": ["KEEP_DOCUMENT_ID"],
  "tombstones": ["REMOVE_DOCUMENT_ID"],
  "authoritative": true,
  "complete": true
}
```

Unlisted inputs remain active. A failed or partial discovery run cannot delete indexed documents. Preview synchronization and cleanup before applying:

```sh
.venv/bin/python -m utils.architecture_maintenance --repository /tmp/artifact-pilot sync manifest.json
.venv/bin/python -m utils.architecture_maintenance --repository /tmp/artifact-pilot cleanup --grace-seconds 604800 --keep-previous 1
```

Both commands require `--apply` for changes. Cleanup protects active, prepared, reader-pinned, and rollback generations. The canonical repository retains at least one rollback generation. Interrupted readers may leave conservative pins; investigate reader ownership before manually removing a stale pin. Object-index lifecycle operations are also exposed by `synchronize_manifest()` and `cleanup_generations()`; they are not triggered by `index_paths()` discovering fewer files.

## Offline evaluation and activation gate

Run fake-provider tests against temporary directories. No production model service or corpus is needed. Existing provider SDK imports may require `DASHSCOPE_API_KEY=offline-test` and `OPENAI_API_KEY=offline-test`; these placeholders are only for import-time construction, not network calls.

The quality gate consumes two frozen, independently adjudicated reports:

```sh
.venv/bin/python -m utils.architecture_evaluation --baseline baseline.json --candidate candidate.json --output gate.json
```

Each report has an `identity` with `case_set`, `source_revisions`, `answer_model`, `output_token_limit`, `generation_set`, `retrieval_config`, `evidence_schema`, and `encoder_contract`. Each case includes `case_id`, `split="held_out"`, `manual_id`, `revision`, `object_type`, `gold_evidence_ids`, `adjudication_provenance`, ranked `retrieved_evidence_ids`, `packed_evidence_ids`, supported/total claim counts, valid/total citation counts, `revision_errors`, `latency_ms`, `input_tokens`, and `output_tokens`.

Baseline and candidate must use the same frozen cases, reference evidence, source revisions, answer model, and output allowance. Tuning cases are rejected. Success requires improved Recall@8 and claim-support precision, no citation/revision regressions, and p95 latency plus mean total generation tokens at most 125% of baseline. Per-type/version metrics and optional index sizes are reported. Exit status 2 means the gate did not pass. The command never activates an index, even on success.

Human calibration, live held-out evaluation, and production activation remain separate operational steps. This offline implementation does not claim measured corpus quality gains.
