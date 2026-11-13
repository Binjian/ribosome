# Upgrade the Baidu OCR notebook to v2

## Summary

Adopt native multi-page parsing as the default, retain PP-DocLayout as an option,
and write new results to `.md_unlimited_v2`. Preserve the existing corpus and
summary checkpoints.

Follow Baidu's [v2 serving recipe](https://github.com/baidu/Unlimited-OCR/blob/main/README.md)
and [block-format conversion rules](https://github.com/baidu/Unlimited-OCR/blob/main/utils/README.md).

## Notebook and API changes

- Update the canonical notebook, preserving existing edits, cell IDs, and metadata;
  regenerate its ignored Python export through nbdev.
- Add `layout_mode="native"` and make it the default for `ocr_pdf`, `ocr_image`, and
  `ocr_folder`. Retain `plain` and `pp-doclayout`, updating their individual requests
  to the v2 protocol.
- Default to the documented served alias `Unlimited-OCR-2`, retain endpoint/model
  overrides, and change the output-directory default to `.md_unlimited_v2`.
- Send PDF pages in one request, with ordered images followed by automatically
  constructed `<PAGE><image>` markers and task text.
- Replace `vllm_xargs` with SGLang's image configuration, custom processor, and
  decoding parameters. Reference the server's processor using its compatible
  serialized class reference, without installing SGLang on the client.
- Add `crop_mode=True` and `max_page_tokens=20000`. Keep `max_tokens=8192` as the
  per-page budget; multiply it by page count for native PDF requests. Make an
  omitted n-gram window resolve to 128 for one image and 1024 for multiple pages.
- Stream native responses, preserve usage and finish information, and raise the
  default request timeout to 6000 seconds. Keep request concurrency limits.
- Update configuration instructions to address the existing `.env` override
  selecting v1 and document the required remote SGLang deployment.

## Native output and downstream compatibility

- Parse page/block markers, heading levels, normalized coordinates, and merge
  indicators. Preserve blank pages and reject page-count mismatches or truncated
  responses.
- Produce the existing schema-v2 `.layout.json`, marker-delimited Markdown, and
  `.assets/` bundle. Keep physical regions page-local, preserve model reading order,
  convert coordinates to rendered-page pixels, and generate real crop assets.
- Retain original block fragments and raw model output. Add native provenance
  containing labels, normalized boxes, heading levels, and ordered merge-group
  member references.
- Make layout ingestion and Markdown conditioning consume explicit native merge
  groups once. Preserve all member citations/assets, concatenate text continuations,
  and deduplicate repeated table headers. Derive merged content from current
  fragments so later repairs remain effective.
- Honor native reading order and heading levels before existing geometric
  heuristics. Keep legacy bundle behavior unchanged.

## Checkpoints and publication

- Keep `OVERWRITE=False` and partial-resume support enabled.
- Cache each complete native response atomically before local conversion. Resume
  parsing, cropping, or publication from that cache without another OCR request.
- Preserve source-signature and model/settings compatibility checks. Parser changes
  alone must not invalidate cached provider responses.
- Preserve incomplete response diagnostics, but retry interrupted inference as a
  whole-document request; do not combine independent page retries into a native
  document.
- Reuse rollback-safe bundle publication. Failures and cancellation must preserve
  previous published outputs and usable checkpoints.
- Leave existing OCR outputs and object-summary checkpoints untouched.

## Validation and assumptions

- Test exact payloads, lightweight processor serialization, streaming chunks, token
  budgets, errors, and truncation.
- Cover blank pages, heading levels, coordinate conversion, crops, text/table
  continuations, and duplicate-free conditioning and retrieval.
- Verify strict bundle validation, checkpoint reuse, incompatible settings,
  cancellation, and publication rollback.
- Run the notebook's offline tests and focused OCR, layout, conditioning, and
  retrieval regressions.
- Remote deployment is outside this notebook update. The configured endpoint
  refused connections during planning; perform a small opt-in smoke test when a v2
  service is available, without launching corpus processing.
