# KUKA chatbot collection (KRL, KQA, and KRLU)

[All utility groups](../README.md)

Collect original answers from [KUKA Center](https://www.kukacenter.com/chat) in
**KRL编程**, **智能问答**, or **KRL程序理解** mode, keeping the complete question and answer as Markdown. Completed
pairs are reused across runs, and each output file holds up to 20 pairs.

## Utilities

| Module | Interface | Purpose |
| --- | --- | --- |
| [collect_krl_qa.py](collect_krl_qa.py) | CLI; `KukaClient` | Authenticate, submit prompts to the KRL model, consume streamed answers, check them, and publish completed pairs. |
| [collect_kqa_qa.py](collect_kqa_qa.py) | CLI | Collect 智能问答 answers using the shared client and storage, with separate KQA defaults. |
| [collect_krlu_qa.py](collect_krlu_qa.py) | CLI | Upload generated KRL samples, wait for training, and collect KRL程序理解 answers with separate KRLU defaults. |
| [generate_krlu_questions.py](generate_krlu_questions.py) | CLI | Reproduce the 1,000-question KRLU bank from program templates and comprehension scenarios. |
| [krl_collection_store.py](krl_collection_store.py) | Python library | Parse numbered prompts, validate existing output, identify pending questions, and append completed answers atomically. |

## KRLU: collect KRL程序理解 pairs

The [KRLU question bank](../../assets/KRLU/krlu_questions_1000.md) contains
1,000 Chinese program comprehension questions. Every question includes an original
generated KRL sample, its assumptions, and targeted questions about its behavior.
The bank covers data, control flow, subprograms, motion, coordinate transforms,
I/O, timers, interrupts, and application sequences. These are source prompts;
chatbot answers are collected separately.

```sh
.venv/bin/python utils/krl/collect_krlu_qa.py --dry-run
.venv/bin/python utils/krl/collect_krlu_qa.py --max-questions 1
.venv/bin/python utils/krl/collect_krlu_qa.py
```

KRLU starts at **0001** and reads `assets/KRLU/krlu_questions_1000.md`.
Answers go to `assets/KRLU/krlu_qa_collected.md`, then
`krlu_qa_collected_002.md`, and so on, with 20 pairs per file by default.
The collector shares the account settings in `utils/krl/.env`, interactive CAPTCHA,
retries, concurrency, integrity checks, atomic publication, and ID-based resume
described below. The module entry point is
`python -m utils.krl.collect_krlu_qa --dry-run`.

KRL程序理解 has a separate workflow in the
[public frontend](https://chat.kukacenter.com/): each new conversation receives
the sample's `.src` and optional `.dat` files through `/api/uploadFiles`, followed
by `/api/krl/train` and bounded `/api/krl/queryStatus` polling. Collection waits
for successful training before submitting the complete prompt to
`/api/krl/stream`. This endpoint does not receive `robotModel` or
`networkSearchFlag`. The answer check and persisted-answer verification use
the same endpoints as the other modes. No files are uploaded during a dry run.

Custom KRLU sources must retain one complete program file per fenced `krl`
block, beginning with a filename comment such as `; FILE: KRLU_0001.src`.
Companion data modules use their matching `.dat` filenames. The filename
marker and the entire code block remain in the saved question. Invalid or
missing source files are rejected before login. Each question has its own
conversation and training context, including when collecting concurrently.
`--answer-timeout` bounds project preparation and then separately bounds answer
generation; training status and file readiness are polled every two seconds.

The source bank can be reproduced or checked offline:

```sh
.venv/bin/python utils/krl/generate_krlu_questions.py --check
.venv/bin/python utils/krl/generate_krlu_questions.py --output /tmp/krlu_questions_1000.md
```

Regenerating the default source after changing templates can change prompt
hashes. Review those changes before resuming an existing collection.

Start with one live question to check the site's current upload/training
behavior before a longer collection. Failed training, interrupted streams,
or a mismatching persisted answer do not publish a completed QA pair;
rerunning requests only missing IDs. Existing KRL and KQA output series are
independent of KRLU.

## KQA: collect 智能问答 pairs

The [KQA question bank](../../assets/KQA/kqa_questions_1000.md) contains 1,000
Chinese questions about KUKA robots: selection, installation, safety, operation,
commissioning, maintenance, troubleshooting, communication, and application
integration. It is a prompt inventory; answers are collected from the chatbot.

```sh
.venv/bin/python utils/krl/collect_kqa_qa.py --dry-run
.venv/bin/python utils/krl/collect_kqa_qa.py --max-questions 1
.venv/bin/python utils/krl/collect_kqa_qa.py
```

KQA starts at **0001** and reads `assets/KQA/kqa_questions_1000.md`. New answers
go into `assets/KQA/kqa_qa_collected.md`, then `kqa_qa_collected_002.md`, and so
on, with 20 pairs per file by default. Rerunning skips completed IDs across all
batches. KRL keeps its existing defaults, including starting at 0019.

All collectors use `utils/krl/.env`, the same account/CAPTCHA login, CLI
options, retry policy, source-order batching, heading repair, and atomic resume
rules documented below. KQA also supports `--concurrency 2 --max-questions 4`
and the module entry point `python -m utils.krl.collect_kqa_qa --dry-run`.
Dry runs report the selected mode/model and do not make network requests or
create output files. Use a separate output series for each mode.

The [public chat frontend](https://chat.kukacenter.com/) identifies 智能问答
as `intelligent_customer_service_robot`; KRL编程 uses `krl_question_robot`.
Both tabs use `/api/efs/stream`, `/api/check`, and
`/api/krl/selectQuestionInfo` (including the `krl` path for 智能问答).
New KQA batches have a KQA title and 智能问答 provenance; record metadata includes
the selected mode and model. All collectors retain the existing storage
format's `krl-qa-record` / `krl-qa-complete` integrity markers so parsing,
heading repair, and recovery use the same implementation.

## Requirements

Run the commands below from the repository root, using the project Python
environment. Live collection uses `requests` for HTTP; reading settings from
`utils/krl/.env` uses `python-dotenv`, including during `--dry-run`.
`--help` and the storage module use only the standard library.

```sh
uv pip install --python .venv/bin/python requests python-dotenv
.venv/bin/python utils/krl/collect_krl_qa.py --help
```

In an environment with pip installed,
`python -m pip install requests python-dotenv` is an alternative; use that
environment's Python for the collector too.
Live collection requires a KUKA Center account, connectivity to its login/chat
services, and a person to enter the site's image CAPTCHA.

## Tutorial: inspect, collect one answer, then resume

### 1. Check the input and existing batches offline

```sh
.venv/bin/python utils/krl/collect_krl_qa.py --dry-run
```

The default input is `assets/KRL/krl_questions_1000.md`. Questions begin with
headings such as `## 0019 真空搬运`; the complete body under each heading is sent
as the prompt. Numbered headings inside fenced code do not split a question.
IDs must be unique, increasing, and have nonempty prompt bodies.

With samples 0001–0018 already saved, the report shows 1,000 source questions,
18 saved pairs, and 982 pending, beginning at **0019 真空搬运**. The dry run makes
no login or chatbot requests and creates no output directories or lock files.

### 2. Collect one missing answer

```sh
.venv/bin/python utils/krl/collect_krl_qa.py --max-questions 1
```

Live runs read credentials from `utils/krl/.env`, beside `collect_krl_qa.py`,
regardless of the current working directory. Use these exact variable names
(`ACCOUT` is the expected spelling):

```dotenv
KUKACENTER_ACCOUT=your-account
KUKACENTER_PASSWORD='your-password'
COLLECT_CONCURRENCY=2
```

Existing process environment variables override `utils/krl/.env`; `--username`
overrides the account from either source. If no account is configured, the existing
`13918961550` default is used. If the password is absent, the collector asks for
it at a hidden prompt. Credential values are read literally without `${...}`
interpolation, and unrelated `.env` values are not exported into the process.

The login flow also asks you to accept the site's linked policies and read the
four-character CAPTCHA from the temporary image path printed in the terminal.
Keep the command waiting while you open that image and enter its code. The
image is removed after the login attempt.

The script has no password command-line option and keeps loaded passwords and
authentication cookies in memory. `--accept-login-terms` explicitly accepts the
site's policies without the consent prompt; it does not automate CAPTCHA entry.

Each question gets its own conversation with the KRL model. The collector waits
for the complete UTF-8 stream, applies the site's answer check, and compares the
persisted Markdown answer with the stream before saving it. It preserves code
fences, tables, indentation, and any reasoning/reference sections returned by
the service. Comparison tolerates CRLF/LF line-ending differences and outer
newlines, while preserving spaces and indentation; the original persisted
Markdown is saved verbatim. If the site's check rejects an answer, it saves the
refusal shown by the frontend.

KQA streams can begin with a `##FAQ##` marker that KUKA omits from the saved
answer. The collector sends the full stream to the site's answer check and
ignores the leading marker when comparing it with the saved answer. A verified
notice that KUKA cannot answer yet is saved verbatim and collection continues.

If creating a conversation returns `code=1`, all three collectors check whether
the newest conversation is empty, using the same history checks as the web
client. KRLU also checks for existing project files and training state before
uploading a sample. The collector can use that conversation once, and workers
share the same record of which conversations have already been assigned during
the run.

### 3. Continue the remaining questions

```sh
.venv/bin/python utils/krl/collect_krl_qa.py
```

Rerun the same command after an interruption. Completed local pairs are skipped.
Only a fully checked answer is published; an interrupted remote question can be
asked again because an incomplete request is not marked complete locally.

During collection, transient connection errors, timeouts, interrupted streams,
and HTTP 408/429/500/502/503/504 responses get up to three additional attempts.
Retries wait 5, 10, then 20 seconds by default; the configurable delay doubles
up to 60 seconds. A generation retry discards the partial stream and starts a
fresh conversation and question UUID. The previous remote request may still
finish. Once a stream is complete, failures in the answer check or saved-answer
fetch retry only that step, retaining the original stream and identifiers.
Missing, empty, or mismatching persisted answers also retry the fetch, allowing
a delayed server save to catch up. HTTP failures and verification mismatches
share the same fetch retry budget; neither generation nor the answer check is
repeated during those fetch retries. If verification exhausts that budget with
a mismatch, both sequential and concurrent CLI runs retry the unverified
question once in a fresh conversation. Concurrent runs first pause new questions
and drain active calls, saving their verified answers, then continue with one
worker for the rest of the run. Sequential runs retry before moving to the next
question. This gives an unusable generation a chance to recover without
overlapping new requests.
The replacement answer must still pass the full check and exact stream/saved
comparison. A second exhausted mismatch for the same question stops collection;
other terminal errors retain their existing stop-and-drain behavior.
`--retries 0` also disables this recovery.

A persistent mismatch always prevents publication of that answer. Its log includes
the raw stream/saved character counts and the first differing offset in the
comparison (starting at zero), without printing answer text.

On the first mismatch in each generation, the CLI also saves a private diagnostic
JSON file beside the output in `.<output-stem>.diagnostics/`. For KQA, the default
directory is `assets/KQA/.kqa_qa_collected.diagnostics/`. Each file contains the
raw `streamed_answer` and `saved_answer`, and is written with permissions `0600`.
The writer does not add credentials, cookies, request headers, or session request
payloads. These files remain separate from collected answers and never mark a
question complete. Use `--diagnostics-dir PATH` to choose another directory, or
`--no-diagnostics` to disable response capture.

For repeated short responses, compare the two diagnostic fields to see whether
the answer text differs or the stream carries an extra marker.

Use `--retries 0` to disable retries, or adjust `--retries` and `--retry-delay`.
Authentication/API rejections and malformed response types stop immediately.
Exhausting retries or cancelling preserves completed local pairs for the next run.

### 4. Collect concurrently

Start with two concurrent questions and a small run:

```sh
.venv/bin/python utils/krl/collect_krl_qa.py --concurrency 2 --max-questions 4
```

Concurrency defaults to `COLLECT_CONCURRENCY` from the process environment, then
`utils/krl/.env`, then 1 when unset. For example, `COLLECT_CONCURRENCY=2` in that
file enables two workers without a command-line option. `--concurrency` overrides
both sources, including invalid defaults; `--concurrency 1` selects sequential
collection. Configured values must be positive integers.

With concurrency greater than 1, `--delay` is the minimum global spacing between
dispatches of new questions, allowing their streamed answers to overlap. Stage retries keep their
existing backoff and are not subject to this spacing.

Answers are stored in question-list order, regardless of which request finishes
first. Each validated answer is inserted into its source-order position and
the Markdown batches are adjusted as needed. Completed answers remain saved
even if an earlier question fails; filling that gap on resume restores its
correct position without requesting the completed answers again.

Login and CAPTCHA happen once. Workers have independent HTTP sessions with
copied authentication cookies, but still use the same account. KUKA Center's
account concurrency limits are unverified; measure a small run before increasing
concurrency. More workers do not guarantee faster service-side generation.

After a terminal question failure or the first Ctrl+C, the collector stops
starting questions, finishes active questions with their configured retries,
saves validated answers, and exits with status 1 or 130 respectively. Graceful
shutdown can wait through HTTP timeouts and retry backoff. Keep the process
running until it exits to retain these answers. An output-write failure stops
all further publication while active HTTP calls drain.

An exhausted stream/saved-answer mismatch has one bounded recovery before it
becomes terminal: the coordinator drains active calls, retries the unverified
question in a new conversation, and lowers concurrency to 1 for the remainder
of that run. Several mismatches in the drained calls are retried sequentially.
This does not assume a particular account limit or accept mismatching text.

For a bounded session or a separate collection:

```sh
.venv/bin/python utils/krl/collect_krl_qa.py --end 40
.venv/bin/python utils/krl/collect_krl_qa.py \
  --questions assets/KRL/krl_questions_1000.md \
  --output assets/KRL/krl_qa_trial.md --start 19 --end 20
```

## Output and resume rules

For KRL, with either sequential or concurrent collection, the existing 18 pairs stay in
`assets/KRL/krl_qa_collected.md`. Samples 0019 and
0020 fill that file; samples 0021–0040 go into `krl_qa_collected_002.md`, then
0041–0060 into `_003.md`, and so on. A new collection beginning at 0019 instead
holds 0019–0038 in its first file. The final batch can contain fewer than 20
pairs. Existing introductory text, including historical manual counts, is
preserved; the dry-run report reflects the actual saved records.

All modes store completed QAs in the order of the source question list. Each
batch holds at most `--batch-size` pairs; gaps can remain when a question fails
or a selected range skips questions. Resume checks completed IDs across all
batches and requests only missing questions. Inserting an earlier answer can
move existing records between batches, preserving their prompts, exact answer
text, and metadata. Existing introductory text stays with its original file.

Live runs also repair batches produced by the older completion-order behavior,
and nest saved answer headings beneath their `### 小库原始回答` label, before
login or collection. Dry runs inspect existing files without rewriting
them. A multi-file rewrite uses a rollback journal: failure or cancellation
restores the previous batch contents, and a later live run recovers an update
interrupted by a process crash under the output lock.

The heading hierarchy is H1 collection → H2 sample → H3 question/answer labels
→ H4 answer sections → H5 subsections. Each answer's shallowest ATX heading
becomes H4; relative levels are retained up to Markdown's H6 limit. Fenced code,
indented code, and non-heading text are preserved. Original heading levels are
stored in record metadata so the exact original response, answer hash, and
length remain available to the storage library and resume checks.

Repair an existing collection without logging in or requesting answers:

```sh
python utils/krl/collect_krl_qa.py --repair-headings
```

This command uses only the standard library, holds the output lock, preserves
existing collection metadata, and exits after repairing saved records. Repeating
it makes no further changes. It cannot be combined with `--dry-run`.

New records include source and exact-answer hashes, answer length, completion
markers, and collection metadata. Existing prompts must still match the input.
Edited answers in new integrity-marked records, duplicate IDs, malformed batches,
numbering gaps, or changed prompts stop collection rather than silently replacing
records. Legacy manually collected answers have no answer hashes, so their
contents cannot be checked for edits until a heading repair adds integrity
markers for their currently saved text. Continue an existing series with the same
batch size; choose a new output path for a different batching scheme.

Each completed pair is written through a temporary file and atomic replacement.
The CLI holds an operating-system lock for the output series. Its hidden
`.<output_stem>.lock` file remains on disk, but the lock itself is released when
the process exits or crashes.

## Options

| Option | Default | Use |
| --- | --- | --- |
| `--questions` | `assets/KRL/krl_questions_1000.md` | Source question file. |
| `--output` | `assets/KRL/krl_qa_collected.md` | First file in the output series. |
| `--username` | `KUKACENTER_ACCOUT` from the environment / `utils/krl/.env`, or built-in account | Override the login account. |
| `--start` / `--end` | 19 / last source ID | Inclusive sample range. |
| `--batch-size` | 20 | Maximum pairs per file. |
| `--max-questions` | All pending in the range | Limit newly collected pairs this run. |
| `--concurrency` | `COLLECT_CONCURRENCY` from the environment / `utils/krl/.env`, or 1 | Maximum simultaneous questions; overrides configured defaults. |
| `--delay` | 2 seconds | Sequential pause; with concurrency > 1, global spacing between new-question dispatches. |
| `--request-timeout` | 180 seconds | Non-streaming HTTP read idle timeout; connection timeout is 20 seconds. |
| `--answer-timeout` | 900 seconds | Streaming read idle timeout and answer-duration limit checked as chunks arrive. |
| `--retries` | 3 | Additional attempts per collection step after transient HTTP failures or missing/mismatching persisted answers; 0 disables retries. |
| `--retry-delay` | 5 seconds | Initial retry delay, doubling up to 60 seconds. |
| `--diagnostics-dir` | `.<output-stem>.diagnostics/` beside the output file | Directory for private JSON files containing unverified stream/saved responses. |
| `--no-diagnostics` | Off | Disable unverified-response diagnostic files. |
| `--accept-login-terms` | Off | Explicitly accept the site's login policies. |
| `--dry-run` | Off | Validate files and report pending work without network access. |
| `--repair-headings` | Off | Repair saved answer headings offline and exit without login or new questions. |

Default source/output paths resolve from the script's repository location;
custom relative paths resolve from your current working directory. The module
entry point is also supported from the repository root:

```sh
.venv/bin/python -m utils.krl.collect_krl_qa --dry-run
```

## Tutorial: inspect collection state from Python

This read-only example can run from the repository root:

```python
from pathlib import Path
from utils.krl.krl_collection_store import CollectionStore, parse_questions

questions = parse_questions(Path("assets/KRL/krl_questions_1000.md"))
store = CollectionStore(Path("assets/KRL/krl_qa_collected.md"), questions, batch_size=20)
pending = store.pending(start=19)
print(f"Saved: {len(store.completed_ids)}; pending: {len(pending)}")
if pending:
    print(pending[0].id, pending[0].title)
```

`Question` exposes `id`, `title`, `prompt`, and `sha256`. A custom collector can
call `store.save(question, answer, metadata=None)` after confirming that its
answer is complete; the return value is the destination `Path`. The storage
library does not hold a long-lived process lock, so use the CLI's `output_lock`
when coordinating custom writers. Saving the same exact answer twice is
idempotent; a different answer for an existing sample is rejected.

## Troubleshooting and validation

| Symptom | Next step |
| --- | --- |
| Login fails | Check the account and CAPTCHA, then rerun for a fresh challenge. |
| HTTP 463 at `/api-b/program_assistant/api/sign` | Update the collector and rerun. Main-site service requests need the frontend's `/api` proxy prefix: `/api/api-b/program_assistant/api/sign`. Completed pairs are reused. |
| `ConnectionError`, read timeout, or interrupted stream | Collection retries transient failures automatically. If retries are exhausted, check connectivity and rerun; completed pairs remain saved. Increase `--answer-timeout` if generation needs longer silent intervals. |
| API rejection, quota limit, or expired login | Resolve the service/account issue, then rerun; completed pairs remain saved. |
| `Question ... failed: /api/session/saveOrUpdate: API rejected request (code=...)` | Conversation creation was rejected before generation. For `code=1`, the collector can continue if the newest conversation is confirmed empty and available. KRLU also requires an empty project context. If collection stops, the numeric code alone does not identify the service-side cause. |
| Stream and persisted answer differ | The collector first refetches with the same stream/conversation up to `--retries` times. Both sequential and concurrent CLI runs then retry the unverified question once in a fresh conversation; concurrent runs first drain active calls and continue with one worker. A repeated failure stops without publishing the uncertain answer. `--retries 0` disables recovery. Inspect `streamed_answer` and `saved_answer` in the private diagnostic JSON for the actual responses; response lengths alone do not establish the server-side cause. |
| Another collector owns the output | Wait for or stop that process; an existing lock file alone does not mean it is locked. |
| Saved-record conflict | Compare the source and output files; retain the original series or choose a separate output path. |

Exit status is 0 for success/dry run, 1 for collection or file errors (including
declined consent or a blank/invalid CAPTCHA), 130 for Ctrl+C or end-of-input at
an interactive prompt, and 2 for invalid command-line arguments.
The frontend HTTP interface can change, so live collection still requires an
initial account/CAPTCHA check.

```sh
.venv/bin/python -m pytest --confcutdir=tests -q \
  tests/test_collect_krl_qa.py tests/test_collect_kqa_qa.py tests/test_collect_krlu_qa.py \
  tests/test_krl_collection_store.py
```

These tests use fake HTTP responses and temporary files; they do not log in or
submit chatbot questions.

## Concurrent collection implementation plan

The implemented design follows these decisions:

1. Add a positive `--concurrency` option, defaulting to `COLLECT_CONCURRENCY` from
   the environment / `utils/krl/.env`, then 1, and show it in dry-run output.
   Keep the synchronous path for sequential runs and add no dependencies.
2. Authenticate once, then allocate at most `min(concurrency, pending_count)`
   independent worker sessions with copied headers and complete cookie jars.
   Run whole `KukaClient.ask()` calls with `asyncio.to_thread()`, retaining the
   existing generation/check/fetch retry boundaries and settings.
3. Filter completed questions and apply `--max-questions` before scheduling.
   Bound active calls and globally space new-question dispatches with `--delay`.
   Process available failures before starting replacement work.
4. Keep one coordinator as the only writer under the existing process lock.
   Insert validated answers in source-question order, including across batch
   boundaries and resumed gaps. Keep per-file atomic publication and use a
   rollback journal for multi-file reordering, preserving existing answers,
   metadata, integrity checks, and ID-based resume.
5. On terminal failure or Ctrl+C, stop dispatching and drain active calls before
   closing sessions or releasing the output lock. Save validated successes and
   exit 1 or 130. On a write failure, stop further publishing while draining.
6. Verify offline: CLI validation, unchanged sequential behavior, one login and
   isolated sessions, bounded overlap, global spacing, single-writer saves,
   source-order batching/resume, rollback/recovery, retry preservation, and
   graceful failure/Ctrl+C.
   Live service concurrency and performance require a separate small trial.
