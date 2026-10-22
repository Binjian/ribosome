# Recursive object embedding and retrieval notebook

## Summary

Create `nbs/01.core.dom.embedding.object.ipynb` to index every nonempty object
`summary`, recursively, plus the document summary from `*_semantics.json` files.
Use Chroma for vector search and SQLite for complete object records and
relationships. Include configurable corpus indexing and retrieval with raw
evidence and surrounding context. Answer generation remains future work.

## Package and public API

- Convert `ribosome.core.dom.embedding` into a package with `natural_text` and
  `object` modules. Import the existing interfaces directly from `natural_text`;
  the package initializer does not re-export them. Update callers, tests, links,
  sidebar and module index.
- Preserve the pending natural-text notebook rename and remove the obsolete
  generated `embedding.py` after export.
- Export `ObjectEmbeddingIndex` with asynchronous `index_file`, `index_paths`,
  `search`, and synchronous `get_object`. Accept an injected Ollama client, model,
  Chroma collection, and SQLite catalog path. Return indexing counts and search
  hits containing distance, summary, record, ancestors, and immediate children.

## Canonical traversal and metadata

- Discover records throughout the DOM and recursively through `children` and
  `cells`, including tables, row groups, rows, cells, lists, items, and figures.
- Deduplicate mirrored records by source ID within a document. Prefer owning
  `children`, then `cells`, then wrapper/header mirrors. Reject conflicting type
  or raw content. Use the preferred summary and retain alternate occurrence paths.
- Use escaped JSON-pointer-style paths. Derive stable logical IDs from normalized
  semantics-file path and source ID; retain source IDs separately.
- Derive parents and ordered child IDs from containment. Attach top-level objects
  to the document; preserve enclosing section headings as context.
- Catalog type, IDs, paths, source locations, exact raw content, summary, parent,
  children, captions, line ranges, image links, and table/list attributes.
- Keep empty/ignored-summary objects in the catalog but embed only nonempty
  strings. Reject malformed non-string summaries with file/path diagnostics.
- Embed the document summary once; retain original semantic JSON as its content
  with a JSON format marker. Exclude prose and section `s` fields from embedding.

## Persistence and repeated runs

- Chroma documents contain summary text and scalar filter metadata. SQLite stores
  complete records and relationships.
- Write immutable per-document generations and atomically activate each completed
  generation in SQLite. Search only active generations. Failure/cancellation must
  preserve the previous searchable generation.
- Reuse vectors when summary and embedding configuration match; metadata-only
  edits must not incur embedding calls. Retry incomplete work safely.
- Validate finite nonempty vectors, consistent dimensions, and collection/model
  compatibility. Retain inactive generations for later cleanup; absent input
  files do not implicitly delete indexed documents. Support one indexing writer.
- Never modify source JSON, existing summaries, or summarization checkpoints.

## Notebook workflow and retrieval

- Follow existing nbdev documentation/export/offline-test conventions. Keep live
  examples `notest`, with no provider calls or corpus processing on import.
- Default to Ollama `qwen3-embedding:8b` on local and remote async services,
  a dedicated `object_summaries` collection, and persistent files under
  `db/object_embeddings/qwen3-embedding-8b/`.
- Configure roots matching the object-summary corpus, discover recursively in
  deterministic order, and return per-file success/failure reports.
- Search using cosine distance, top-k, optional document/type filters, and the
  same embedding configuration. Return raw evidence, source location, ancestors,
  and immediate children through SQLite lookups.

## Validation and boundaries

- Test deep nesting, cells outside children, mirrors, duplicate summaries,
  repeated IDs across files, escaped paths, raw content, relationships, section
  context, and catalog-only objects.
- Test unchanged runs, summary/metadata edits, removed objects, invalid vectors,
  model mismatches, provider/storage failure, cancellation, and retry.
- Use fake providers with temporary SQLite and real ephemeral Chroma. Verify
  active-generation filters and context lookup.
- Run nbdev export/notebook tests and embedding/DOM integration tests; verify
  inputs remain byte-for-byte unchanged. Do not run live corpus embedding.
- IDs are stable for the same file location and source ID; moving a file creates
  a new document identity. Preserve existing natural-text interface signatures under `embedding.natural_text`.
