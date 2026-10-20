# Object-summary checkpoint reuse

For `nbs/05.application.summary.object.ipynb`, keep `RESUME=True` and reuse the
existing corpus checkpoints when adding summary features, including deeper table
row groups or list items. Do not reset checkpoints or invalidate completed
documents solely because summarizer code or the object schema version changed.
Check completed outputs for missing descendants or summaries and incrementally
fill those gaps. Preserve existing object, prose, section, and document summaries;
request only missing summaries and publish upgrades atomically. On failure or
cancellation, preserve the previous output and completed checkpoint for retry.
Rebuild completed documents only when the user explicitly requests it.
Preserve the existing source/output signature and model/settings compatibility
checks, which detect changed inputs or processing configuration.
