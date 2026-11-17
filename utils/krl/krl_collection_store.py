"""Parse KRL prompts and save completed answers in source-ordered Markdown batches.

Records carry integrity markers so interrupted or edited answers cannot be
silently treated as completed on a subsequent run. Displayed headings nest
under each answer label; the original response remains exactly reconstructable.
Legacy layouts can be upgraded without new requests.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_SOURCE_HEADING = re.compile(r"^## +(\d{4}) +(.+?)\s*$")
_SAVED_HEADING = re.compile(r"^## +样本(\d{4})[：:]\s*(.+?)\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ANSWER_HEADING = re.compile(r"^ {0,3}(#{1,6})(?=[ \t]|$)")
_META_PREFIX = "<!-- krl-qa-record "
_END_PREFIX = "<!-- krl-qa-complete "


@dataclass(frozen=True)
class Question:
    id: int
    title: str
    prompt: str

    @property
    def sha256(self) -> str:
        return _digest(self.prompt)


@dataclass(frozen=True)
class _Saved:
    question: Question
    answer: str
    path: Path
    raw: bytes
    metadata: dict | None = None


def _canonical(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _digest(text: str) -> str:
    return hashlib.sha256(_canonical(text).encode("utf-8")).hexdigest()


def _answer_digest(text: str) -> str:
    """Hash the exact Markdown, including significant indentation and spaces."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _outside_lines(text: str):
    """Yield (offset, line) outside fenced code, retaining offsets into text."""
    active = None
    offset = 0
    for line in text.splitlines(keepends=True):
        fence = _FENCE.match(line)
        if active is not None:
            if fence and fence.group(1)[0] == active[0] and len(fence.group(1)) >= len(active):
                if not line[fence.end():].strip():
                    active = None
        elif fence:
            active = fence.group(1)
        else:
            yield offset, line.rstrip("\r\n")
        offset += len(line)


def _answer_headings(answer: str) -> list[tuple[int, int]]:
    """Locate ATX markers without changing fenced or indented code."""
    return [(offset + match.start(1), len(match.group(1)))
            for offset, line in _outside_lines(answer)
            if (match := _ANSWER_HEADING.match(line))]


def _nested_levels(levels: list[int]) -> list[int]:
    # H1 is the collection, H2 a sample, and H3 the question/answer labels.
    # Markdown supports only six levels; retain deeper source levels in metadata.
    base = min(levels) if levels else 4
    return [min(6, level - base + 4) for level in levels]


def _replace_heading_levels(answer: str, levels: list[int]) -> str:
    headings = _answer_headings(answer)
    if len(headings) != len(levels):
        raise ValueError("Saved answer heading integrity check failed")
    for (offset, length), level in reversed(list(zip(headings, levels))):
        answer = answer[:offset] + "#" * level + answer[offset + length:]
    return answer


def _render_record(question: Question, answer: str, metadata: dict | None = None,
                   *, preserve_legacy_hash: bool = False) -> bytes:
    """Nest displayed headings while hashing and retaining the exact response."""
    levels = [level for _, level in _answer_headings(answer)]
    rendered = _replace_heading_levels(answer, _nested_levels(levels))
    record_metadata = dict(metadata or {})
    record_metadata.pop("answer_heading_levels", None)
    record_metadata.update(id=question.id, question_sha256=question.sha256,
                           answer_sha256=_answer_digest(answer), answer_length=len(answer))
    if preserve_legacy_hash:
        # Older marked records hash canonical whitespace. Keep that provenance
        # and format when nesting their headings rather than rehashing them.
        record_metadata.pop("answer_length")
        record_metadata["answer_sha256"] = metadata["answer_sha256"]
    if levels:
        record_metadata["answer_heading_levels"] = levels
    marker = json.dumps(record_metadata, ensure_ascii=False, sort_keys=True)
    return (f"## 样本{question.id:04d}：{question.title}\n"
            f"{_META_PREFIX}{marker} -->\n\n"
            f"### 问题\n\n{question.prompt}\n\n"
            f"### 小库原始回答\n\n{rendered}\n\n"
            f"{_END_PREFIX}{question.id:04d} -->\n").encode("utf-8")


def parse_questions(path: str | Path) -> list[Question]:
    """Read complete prompt bodies, ignoring numbered headings in code blocks."""
    text = Path(path).read_text(encoding="utf-8-sig")
    headings = [(offset, line, match) for offset, line in _outside_lines(text)
                if (match := _SOURCE_HEADING.fullmatch(line))]
    if not headings:
        raise ValueError(f"No numbered questions found in {path}")
    questions = []
    previous = 0
    for index, (offset, line, match) in enumerate(headings):
        question_id = int(match.group(1))
        if question_id <= previous:
            raise ValueError(f"Question IDs must be unique and increasing: {question_id:04d}")
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        prompt = text[offset + len(line):end].strip()
        if not prompt:
            raise ValueError(f"Empty question: {question_id:04d}")
        questions.append(Question(question_id, match.group(2), prompt))
        previous = question_id
    return questions


def _read_batch(path: Path) -> tuple[bytes, list[_Saved]]:
    original = path.read_bytes()
    text = original.decode("utf-8-sig")
    headings = []
    active_record = None
    active_fence = None
    offset = 0
    for original_line in text.splitlines(keepends=True):
        line = original_line.rstrip("\r\n")
        if active_record is not None:
            if line == f"{_END_PREFIX}{active_record:04d} -->":
                active_record = None
        else:
            fence = _FENCE.match(line)
            if active_fence is not None:
                if (fence and fence.group(1)[0] == active_fence[0]
                        and len(fence.group(1)) >= len(active_fence)
                        and not line[fence.end():].strip()):
                    active_fence = None
            elif fence:
                active_fence = fence.group(1)
            elif match := _SAVED_HEADING.fullmatch(line):
                headings.append((offset, line, match))
            elif line.startswith(_META_PREFIX) and headings:
                active_record = int(headings[-1][2].group(1))
        offset += len(original_line)
    if text.strip() and not headings:
        raise ValueError(f"Existing output contains no recognizable Q&A records: {path}")
    records = []
    for index, (offset, line, match) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        block = text[offset + len(line):end].strip()
        metadata = None
        if block.startswith(_META_PREFIX):
            marker, _, block = block.partition("\n")
            if not marker.endswith(" -->"):
                raise ValueError(f"Invalid record metadata in {path}")
            try:
                metadata = json.loads(marker[len(_META_PREFIX):-4])
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid record metadata in {path}") from exc
            block = block.strip()
        labels = [(position, label) for position, label in _outside_lines(block)
                  if label in ("### 问题", "### 小库原始回答")]
        if len(labels) < 2 or labels[0] != (0, "### 问题") or labels[1][1] != "### 小库原始回答":
            raise ValueError(f"Missing question/answer for sample {match.group(1)} in {path}")
        answer_start = labels[1][0] + len(labels[1][1])
        prompt = block[len(labels[0][1]):labels[1][0]].strip()
        answer_block = block[answer_start:]
        answer = answer_block.strip()
        question = Question(int(match.group(1)), match.group(2), prompt)
        if metadata is not None:
            end_marker = f"{_END_PREFIX}{question.id:04d} -->"
            if not isinstance(metadata, dict):
                raise ValueError(f"Invalid record metadata in {path}")
            levels = metadata.get("answer_heading_levels")
            if "answer_heading_levels" in metadata:
                if (not isinstance(levels, list) or not levels
                        or any(type(level) is not int or not 1 <= level <= 6
                               for level in levels)):
                    raise ValueError(f"Invalid answer heading levels for {question.id:04d} in {path}")
            answer_length = metadata.get("answer_length")
            if "answer_length" in metadata:
                if (type(answer_length) is not int or answer_length < 0
                        or not answer_block.startswith("\n\n")):
                    raise ValueError(f"Invalid answer length for {question.id:04d} in {path}")
                if levels is not None:
                    answer_length += sum(_nested_levels(levels)) - sum(levels)
                answer = answer_block[2:2 + answer_length]
                if answer_block[2 + answer_length:] != "\n\n" + end_marker:
                    raise ValueError(f"Incomplete saved answer {question.id:04d} in {path}")
            elif answer.endswith(end_marker):
                # Accept records written before exact whitespace preservation.
                answer = answer[:-len(end_marker)].strip()
            else:
                raise ValueError(f"Incomplete saved answer {question.id:04d} in {path}")
            if levels is not None:
                if [level for _, level in _answer_headings(answer)] != _nested_levels(levels):
                    raise ValueError(f"Saved answer heading integrity check failed for {question.id:04d} in {path}")
                answer = _replace_heading_levels(answer, levels)
            answer_hash = _answer_digest(answer) if "answer_length" in metadata else _digest(answer)
            if (metadata.get("id") != question.id
                    or metadata.get("question_sha256") != question.sha256
                    or metadata.get("answer_sha256") != answer_hash):
                raise ValueError(f"Saved Q&A integrity check failed for {question.id:04d} in {path}")
        if not prompt or not answer.strip():
            raise ValueError(f"Empty saved question/answer {question.id:04d} in {path}")
        records.append(_Saved(question, answer, path, text[offset:end].encode("utf-8"), metadata))
    prefix = text[:headings[0][0]].encode("utf-8") if headings else text.encode("utf-8")
    if original.startswith(codecs.BOM_UTF8):
        prefix = codecs.BOM_UTF8 + prefix
    return prefix, records


def _saved_records(path: Path) -> list[_Saved]:
    return _read_batch(path)[1]


def _atomic_bytes(path: Path, content: bytes) -> None:
    """Publish a complete replacement; a failed write never exposes a partial file."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CollectionStore:
    """Keep completed answers in source order, filling batches before creating more.

    One collector process should own an output series at a time. The caller can
    hold a process lock for the duration of its browser session. Each save also
    reloads all outputs to detect changes made since this instance was created.
    Call recover() under that lock before opening an interrupted batch rewrite.
    """

    def __init__(self, output_path: str | Path, questions: Iterable[Question],
                 batch_size: int = 20):
        self.output_path = Path(output_path)
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        values = list(questions)
        self.questions = {question.id: question for question in values}
        self._order = {question.id: index for index, question in enumerate(values)}
        if len(values) != len(self.questions):
            raise ValueError("Duplicate source question IDs")
        self._reload()

    @property
    def completed_ids(self) -> set[int]:
        return set(self._saved)

    def pending(self, start: int = 19, end: int | None = None) -> list[Question]:
        return [question for question in self.questions.values()
                if question.id >= start and (end is None or question.id <= end)
                and question.id not in self._saved]

    def _batch_path(self, number: int) -> Path:
        if number == 1:
            return self.output_path
        return self.output_path.with_name(
            f"{self.output_path.stem}_{number:03d}{self.output_path.suffix}")

    @staticmethod
    def _journal_path(output_path: Path) -> Path:
        return output_path.with_name(f".{output_path.name}.ordering.json")

    @classmethod
    def recover(cls, output_path: str | Path) -> None:
        """Roll back an interrupted reorder; the caller must own the output lock."""
        output = Path(output_path)
        journal = cls._journal_path(output)
        if not journal.exists():
            return
        data = json.loads(journal.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("files"), list) or not data["files"]):
            raise ValueError(f"Invalid ordering recovery journal: {journal}")
        snapshots = []
        for number, entry in enumerate(data["files"], 1):
            path = output if number == 1 else output.with_name(
                f"{output.stem}_{number:03d}{output.suffix}")
            if (not isinstance(entry, dict) or entry.get("name") != path.name
                    or "before" not in entry
                    or (entry["before"] is not None and not isinstance(entry["before"], str))):
                raise ValueError(f"Invalid ordering recovery journal: {journal}")
            before = None if entry["before"] is None else base64.b64decode(
                entry["before"], validate=True)
            snapshots.append((path, before))
        # Validate the complete journal before restoring any file. Retain it if
        # restoration fails, so the next locked run can retry the rollback.
        for path, before in snapshots:
            if before is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_bytes() != before:
                _atomic_bytes(path, before)
        journal.unlink()

    def _publish_sorted(self, records: list[_Saved]) -> None:
        records = sorted(records, key=lambda record: self._order[record.question.id])
        updates = []
        for offset in range(0, len(records), self.batch_size):
            path = self._batch_path(offset // self.batch_size + 1)
            content = self._prefixes.get(path, (
                "# KRL工艺参数问答采集\n\n"
                "来源：https://www.kukacenter.com/chat 的 KRL编程模式。\n\n\n"
            ).encode("utf-8"))
            for record in records[offset:offset + self.batch_size]:
                if content and not content.endswith((b"\n\n", b"\r\n\r\n")):
                    content += b"\n\n"
                content += record.raw
            before = path.read_bytes() if path.exists() else None
            updates.append((path, before, content))
        if not any(before != after for _, before, after in updates):
            return
        journal = self._journal_path(self.output_path)
        snapshots = {"version": 1, "files": [
            {"name": path.name, "before": None if before is None else
             base64.b64encode(before).decode("ascii")}
            for path, before, _ in updates
        ]}
        _atomic_bytes(journal, json.dumps(snapshots).encode("utf-8"))
        try:
            for path, before, after in updates:
                if before != after:
                    _atomic_bytes(path, after)
            journal.unlink()
        except BaseException:
            # Cross-batch insertion can move existing records between files.
            # Roll back the whole series on failure/cancellation, not just the
            # last file, to avoid lost or duplicate completed answers.
            self.recover(self.output_path)
            raise

    def ensure_order(self) -> None:
        """Repair older completion-ordered batches without regenerating answers."""
        self._reload()
        if list(self._saved) != sorted(self._saved, key=self._order.__getitem__):
            self._publish_sorted(list(self._saved.values()))
            self._reload()

    def ensure_heading_hierarchy(self) -> None:
        """Upgrade saved heading layouts without requesting or changing answers."""
        self._reload()
        records = []
        changed = False
        for record in self._saved.values():
            levels = [level for _, level in _answer_headings(record.answer)]
            if levels and "answer_heading_levels" not in (record.metadata or {}):
                raw = _render_record(
                    record.question, record.answer, record.metadata,
                    preserve_legacy_hash=record.metadata is not None
                    and "answer_length" not in record.metadata,
                )
                record = _Saved(record.question, record.answer, record.path, raw)
                changed = True
            records.append(record)
        if changed:
            self._publish_sorted(records)
            self._reload()

    def _reload(self) -> None:
        if self._journal_path(self.output_path).exists():
            raise ValueError("Interrupted ordering update; rerun the collector to recover under the output lock")
        numbered = {}
        pattern = re.compile(rf"^{re.escape(self.output_path.stem)}_(\d+)"
                             rf"{re.escape(self.output_path.suffix)}$")
        for path in self.output_path.parent.glob(f"{self.output_path.stem}_*{self.output_path.suffix}"):
            if match := pattern.fullmatch(path.name):
                number = int(match.group(1))
                if number < 2 or path != self._batch_path(number) or number in numbered:
                    raise ValueError(f"Unexpected output batch filename: {path}")
                numbered[number] = path
        if self.output_path.exists():
            numbered[1] = self.output_path
        if numbered and sorted(numbered) != list(range(1, max(numbered) + 1)):
            raise ValueError("Output batch numbering has a gap")
        self._batches = []
        self._saved = {}
        self._prefixes = {}
        for number, path in sorted(numbered.items()):
            self._prefixes[path], records = _read_batch(path)
            if len(records) > self.batch_size or (number != max(numbered) and len(records) != self.batch_size):
                raise ValueError(f"Unexpected number of records in batch {path}: {len(records)}")
            for record in records:
                question = record.question
                source = self.questions.get(question.id)
                if source is None or source.title != question.title or source.sha256 != question.sha256:
                    raise ValueError(f"Saved question {question.id:04d} differs from the source in {path}")
                if question.id in self._saved:
                    raise ValueError(f"Duplicate saved question {question.id:04d} in {path}")
                self._saved[question.id] = record
            self._batches.append((path, len(records)))

    def save(self, question: Question, answer: str, metadata: dict | None = None) -> Path:
        """Atomically save one full answer, refusing changed prompts or duplicates."""
        source = self.questions.get(question.id)
        if source != question:
            raise ValueError(f"Question {question.id:04d} does not match the source")
        if not answer.strip():
            raise ValueError("Cannot save an empty answer")
        self._reload()
        if existing := self._saved.get(question.id):
            if _answer_digest(existing.answer) != _answer_digest(answer):
                raise ValueError(f"Refusing to replace an existing answer for {question.id:04d}")
            return existing.path
        if self._batches and self._batches[-1][1] < self.batch_size:
            path = self._batches[-1][0]
        else:
            path = self._batch_path(len(self._batches) + 1)
        block = _render_record(question, answer, metadata)
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered_ids = sorted(self._saved, key=self._order.__getitem__)
        if (list(self._saved) != ordered_ids
                or (ordered_ids and self._order[question.id] < self._order[ordered_ids[-1]])):
            self._publish_sorted([
                *self._saved.values(), _Saved(question, answer, path, block),
            ])
            self._reload()
            return self._saved[question.id].path
        previous = path.read_bytes() if path.exists() else (
            "# KRL工艺参数问答采集\n\n"
            "来源：https://www.kukacenter.com/chat 的 KRL编程模式。\n".encode("utf-8"))
        _atomic_bytes(path, previous + b"\n\n" + block)
        self._reload()
        return path
