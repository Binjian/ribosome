# KRL chatbot collection

[All utility groups](../README.md)

Collect original answers from [KUKA Center](https://www.kukacenter.com/chat) in
**KRL编程** mode, keeping the complete question and answer as Markdown. Completed
pairs are reused across runs, and each output file holds up to 20 pairs.

## Utilities

| Module | Interface | Purpose |
| --- | --- | --- |
| [collect_krl_qa.py](collect_krl_qa.py) | CLI; `KukaClient` | Authenticate, submit prompts to the KRL model, consume streamed answers, check them, and publish completed pairs. |
| [krl_collection_store.py](krl_collection_store.py) | Python library | Parse numbered prompts, validate existing output, identify pending questions, and append completed answers atomically. |

## Requirements

Run the commands below from the repository root, using the project Python
environment. The collector's live HTTP dependency is `requests`; `--help`,
`--dry-run`, and the storage module use only the standard library.

```sh
uv pip install --python .venv/bin/python requests
.venv/bin/python utils/krl/collect_krl_qa.py --help
```

In an environment with pip installed, `python -m pip install requests` is an
alternative; use that environment's Python for the collector too.
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

The default username is `13918961550`; use `--username` or `KUKA_USERNAME` for
another account. The login flow asks you to accept the site's linked policies,
enter the password at a hidden prompt, and read the four-character CAPTCHA from
the temporary image path printed in the terminal. Keep the command waiting
while you open that image and enter its code. The image is removed after the
login attempt.

`KUKA_PASSWORD` can supply the password from the environment. The script has no
password command-line option and keeps passwords and authentication cookies in
memory. `--accept-login-terms` explicitly accepts the site's policies without
the consent prompt; it does not automate CAPTCHA entry.

Each question gets its own conversation with the KRL model. The collector waits
for the complete UTF-8 stream, applies the site's answer check, and compares the
persisted Markdown answer with the stream before saving it. It preserves code
fences, tables, indentation, and any reasoning/reference sections returned by
the service. If the site's check rejects an answer, it saves the refusal shown
by the frontend.

### 3. Continue the remaining questions

```sh
.venv/bin/python utils/krl/collect_krl_qa.py
```

Rerun the same command after an interruption. Completed local pairs are skipped.
Only a fully checked answer is published; an interrupted remote question can be
asked again because an incomplete request is not marked complete locally.

For a bounded session or a separate collection:

```sh
.venv/bin/python utils/krl/collect_krl_qa.py --end 40
.venv/bin/python utils/krl/collect_krl_qa.py \
  --questions assets/KRL/krl_questions_1000.md \
  --output assets/KRL/krl_qa_trial.md --start 19 --end 20
```

## Output and resume rules

The existing 18 pairs stay in `assets/KRL/krl_qa_collected.md`. Samples 0019 and
0020 fill that file; samples 0021–0040 go into `krl_qa_collected_002.md`, then
0041–0060 into `_003.md`, and so on. A new collection beginning at 0019 instead
holds 0019–0038 in its first file. The final batch can contain fewer than 20
pairs. Existing introductory text, including historical manual counts, is
preserved; the dry-run report reflects the actual saved records.

New records include source and exact-answer hashes, answer length, completion
markers, and collection metadata. Existing prompts must still match the input.
Edited answers in new integrity-marked records, duplicate IDs, malformed batches,
numbering gaps, or changed prompts stop collection rather than silently replacing
records. Legacy manually collected answers have no answer hashes, so their
contents cannot be checked for edits. Continue an existing series with the same
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
| `--username` | `KUKA_USERNAME` or built-in account | Login account. |
| `--start` / `--end` | 19 / last source ID | Inclusive sample range. |
| `--batch-size` | 20 | Maximum pairs per file. |
| `--max-questions` | All pending in the range | Limit newly collected pairs this run. |
| `--delay` | 2 seconds | Pause between questions. |
| `--request-timeout` | 180 seconds | HTTP connection/read handling; connection timeout is 20 seconds. |
| `--answer-timeout` | 900 seconds | Maximum streamed-answer duration. |
| `--accept-login-terms` | Off | Explicitly accept the site's login policies. |
| `--dry-run` | Off | Validate files and report pending work without network access. |

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
| API rejection, quota limit, or expired login | Resolve the service/account issue, then rerun; completed pairs remain saved. |
| Stream and persisted answer differ | Inspect the service response/interface before retrying; the uncertain pair was not published. |
| Another collector owns the output | Wait for or stop that process; an existing lock file alone does not mean it is locked. |
| Saved-record conflict | Compare the source and output files; retain the original series or choose a separate output path. |

Exit status is 0 for success/dry run, 1 for collection or file errors (including
declined consent or a blank/invalid CAPTCHA), 130 for Ctrl+C or end-of-input at
an interactive prompt, and 2 for invalid command-line arguments.
The frontend HTTP interface can change, so live collection still requires an
initial account/CAPTCHA check.

```sh
.venv/bin/python -m pytest --confcutdir=tests -q \
  tests/test_collect_krl_qa.py tests/test_krl_collection_store.py
```

These tests use fake HTTP responses and temporary files; they do not log in or
submit chatbot questions.
