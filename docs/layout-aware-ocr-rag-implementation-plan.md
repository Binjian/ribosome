# Export and Implement OCR Strategy Hardening

## Plan Artifact

- Create `docs/layout-aware-ocr-rag-implementation-plan.md` containing this implementation plan.
- Keep the plan synchronized if implementation details change during verification.

## Implementation

- Add shared SHA-256 source signatures across OCR utilities, Baidu Unlimited-OCR, GLM OCR, and repair staging.
- Migrate hashless checkpoints only at their recorded path; require size, mtime, and SHA-256 for relocation.
- Archive each affected region exactly once in `repair_history` before generic retry, document resume, selective reopening, regeneration, or discard.
- Make notebook repair execution opt-in, use an explicit repair target, and report an unfiltered post-run audit.
- Merge tolerated bundle-validation issues into conditioning warnings and reject duplicate layout/Markdown keys in strict conditioning.
- Refresh current-corpus reference tests and update the strategy’s completed/remaining status wording.
- Export generated Python modules from the canonical nbdev notebooks while preserving unrelated notebook metadata changes.

## Compatibility

- Newly written `source_signature` objects gain `sha256`; schema versions and public Python signatures remain unchanged.
- Legacy same-path checkpoints are upgraded automatically; hashless relocated checkpoints are rejected.
- Indexing retains its existing caller-enforced audit gate.
- Corpus files and unresolved structural/manual-review findings remain untouched.

## Verification

- Test new, migrated, relocated, altered, and rejected source-signature cases for both OCR providers.
- Test exact-once history preservation through staging and successful provider replacement.
- Test non-strict conditioning warnings, strict no-output failures, duplicate-key rejection, and clean folder status.
- Run the updated reference acceptance and retrieval tests without skips.
- Run targeted notebook/pytest checks, `nbdev-export`, the full test suite, and a final generated-code/diff consistency review.

## Verification Results (2026-08-28)

- Both provider source-signature policies and successful replacement history preservation pass focused regression coverage.
- Staging exact-once history, relocation, hashless-migration, and altered-source rejection checks pass.
- Conditioning warning order, strict no-output failures, duplicate-key rejection, and clean-folder status checks pass.
- The current-corpus acceptance and exact-retrieval reference tests pass without skips (`2 passed`).
- The five changed canonical notebooks pass `nbdev-test`; `nbdev-export` completes successfully.
- The full Python suite passes (`77 passed`). It was run outside the managed filesystem sandbox because that runner cannot wake an asyncio event loop from `asyncio.to_thread`; the same suite’s ordinary execution is clean outside that boundary.
- Ruff, notebook JSON validation, `git diff --check`, and generated-code consistency checks pass.
