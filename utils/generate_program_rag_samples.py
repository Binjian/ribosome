"""Generate reproducible, evidence-backed questions over saved program tasks.

This is a supplemental multi-question-per-task sample set. It deliberately does
not replace the exhaustive, one-question-per-task authoring bank. No model calls
or writes to the source bank/corpus are made.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata


VERSION = 1
BANK_RELATIVE = Path("db/task_rag_tests/question_with_tasks/bank.json")
PROJECT = Path(__file__).resolve().parents[1]
CATEGORIES = {
    "palletizing": "码垛", "arc_welding": "弧焊", "laser_welding": "激光焊",
    "material_handling": "搬运", "adhesive_dispensing": "涂胶",
    "stud_welding": "螺柱焊", "spr_riveting": "SPR铆接", "fds": "FDS",
    "tracking": "跟随",
}
PARAMETER = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]*)\s*=\s*([^\s,，;；:：]+)")
CONTROL = re.compile(r"^(?:IF|ELSE|ENDIF|WHILE|ENDWHILE|GOTO|SELECT|CALL|RET|END|L\d|L\s*=|IRQ)\b")
BOUNDARY = re.compile(r"^(?:NOP|END|RET|ENDIF|ENDWHILE|ELSE|L\d+|L\s*=\s*\d+)\s*;?$")
SOURCE_HINT = re.compile(
    r"根据手册|根据《|请依据|《[^》]*手册[^》]*》|SX\d{5,}|"
    r"第[\d.一二三四五六七八九十]+[章节]|\bV\d+\.\d+|"
    r"\.(?:md|json)\b|对象\s*ID|行号|[（(][ABC]-\d", re.I)
CONSTRAINT = re.compile(r"必须|不能|不得|需要(?:插入|使用|执行|添加|设置|配置|放置|调用)|应当|应该|不支持|限制|只能|需添加|需使用")


class TableParser(HTMLParser):
    """Read task cells without needing the application dependencies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []

    def handle_data(self, data):
        self.parts.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def plain(text):
    if not re.search(r"</?(?:table|tr|td|th|p|div|span|b|i|strong|em)\b", text):
        return " ".join(text.replace("**", "").split())
    parser = TableParser()
    parser.feed(text)
    return " ".join(" ".join(parser.parts).replace("**", "").split())


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def question_key(question):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", question)).casefold()


def relative_source(path):
    # The persisted bank was authored from the backend workspace and can contain
    # ../ribosome/assets. The public workspace assets links address the same file.
    value = str(path).replace("\\", "/")
    marker = "assets/code-docs."
    return value[value.index(marker):] if marker in value else value


def topic_for(case):
    titles = [re.sub(r"^\s*\d+(?:\.\d+)*\s*", "", title).strip()
              for title in case["heading_path"]]
    titles = [title for title in titles if not re.fullmatch(
        r"作业示例|程序示例|模板作业|作业模板|编程|指令、快捷键说明|SPR指令集|FDS指令集", title)]
    title = titles[-1] if titles else ""
    title = re.sub(r"（[^）]*）", "", title)
    category = CATEGORIES[case["category"]]
    if title == "工作流程&功能":
        title = "程序示例"
    if not title:
        return category + "作业"
    if category.lower() not in title.lower() and not (category.startswith("SPR") and title.startswith("SPR")):
        title = category + "的" + title
    return title if "作业" in title or title.endswith("流程") else title + "作业"


def task_rows(excerpt):
    parser = TableParser()
    parser.feed(excerpt)
    rows = []
    for index, cells in enumerate(parser.rows, 1):
        # Some sources add an empty/numbering column before the instruction.
        command_index = next((i for i, cell in enumerate(cells)
                              if re.match(r"^[A-Z][A-Za-z0-9_]*\b", cell)), None)
        if command_index is None:
            # A continuation, header, or module separator breaks an ordered span.
            rows.append(dict(index=index, code="", description="", text=" ".join(cells), selector=""))
            continue
        code = cells[command_index].strip()
        description = " ".join(cells[command_index+1:]).strip()
        selector = PARAMETER.split(code)[0].strip()
        # The split above contains only the prefix preceding the first assignment.
        selector = re.sub(r"\s+", " ", selector).rstrip(";:")
        rows.append(dict(index=index, code=code, description=description,
                         text=" ".join(" ".join(cells).split()), selector=selector))
    return rows


def candidate_questions(case):
    """Yield different factual targets; no cosmetic question paraphrases."""
    rows = task_rows(case["references"][0]["excerpt"])
    topic = topic_for(case)
    code_counts = Counter(row["code"] for row in rows if row["code"])
    selectors = Counter(row["selector"] for row in rows if row["selector"])
    roles = Counter(row["description"].strip("/ ") for row in rows if row["description"])
    result = defaultdict(list)

    def add(intent, question, selected, expected=None):
        if len(question) > 300 or SOURCE_HINT.search(question):
            return
        quotes = list(dict.fromkeys(row["text"] for row in selected if row["text"]))
        if not quotes:
            return
        result[intent].append(dict(question=question, intent=intent,
            source_rows=[row["index"] for row in selected], anchors=quotes,
            expected_facts=expected or quotes))

    for row_number, row in enumerate(rows):
        code, description, selector = row["code"], row["description"], row["selector"]
        if not code or BOUNDARY.fullmatch(code) or len(code) > 115:
            continue
        role = description.strip("/ ").rstrip("。；;")
        if code_counts[code] == 1 and re.search(r"[\u4e00-\u9fff]", role):
            add("instruction_role", f"在{topic}中，执行“{code}”对应哪项操作？", [row], [description])
            if CONSTRAINT.search(description):
                restrictions = [part.strip() for part in re.split(r"[。；]|注意[：:]", description)
                                if part.strip() and CONSTRAINT.search(part)]
                add("instruction_constraint", f"在{topic}中，使用“{code}”需要遵守哪些明确要求？", [row], restrictions)
        if (4 <= len(role) <= 48 and roles[role] == 1 and not re.search(
                r"上图|下图|界面|如下|必须|\?|？|前面|后面|上面|下面", role)):
            add("action_to_instruction", f"在{topic}中，“{role}”由哪条程序指令实现？", [row], [code])
        params = list(dict.fromkeys(match[0] for match in PARAMETER.findall(code)))
        if params and selector and selectors[selector] == 1 and not CONTROL.match(code):
            # Values are withheld from the question, so this tests retrieval.
            names = "、".join(params)
            distribution = "分别" if len(params) > 1 else ""
            qualifier = ""
            previous = rows[row_number-1] if row_number else None
            selected = [row]
            if (selector == "DELAY" and previous and previous["code"]
                    and code_counts[previous["code"]] == 1 and len(previous["code"]) <= 65):
                qualifier = f"紧接“{previous['code']}”之后的 "
                selected = [previous, row]
            add("parameter_values", f"在{topic}中，{qualifier}{selector} 指令的 {names} 参数{distribution}设为什么值？", selected, [code])
        branch = re.search(r"^IF\s+(.+?)\s+GOTO\s+(L\d+)\s*;?$", code)
        if branch and code_counts[code] == 1:
            variable = re.match(r"[A-Za-z][A-Za-z0-9_\[\]#]*", branch[1])
            if variable:
                add("branch_condition", f"在{topic}中，跳转至 {branch[2]} 的 IF 分支要求 {variable[0]} 满足什么条件？", [row], [code])
        if code.startswith("WAIT ") and role and roles[role] == 1 and len(role) <= 45:
            add("signal_wait", f"在{topic}中，为完成“{role}”，WAIT 指令应等待哪个信号状态并采用什么超时设置？", [row], [code])

    # Ordered spans never cross a branch, call, label, subprogram boundary, or
    # unparsed continuation. We ask about program order, not runtime causality.
    for start in range(len(rows)):
        for width in (3, 4, 5):
            span = rows[start:start+width]
            if len(span) != width or any(not row["code"] or CONTROL.match(row["code"])
                                        or BOUNDARY.fullmatch(row["code"]) for row in span):
                continue
            left, right = span[0], span[-1]
            if any(code_counts[row["code"]] != 1 for row in (left, right)):
                continue
            if max(len(left["code"]), len(right["code"])) > 75:
                continue
            add("instruction_sequence", f"在{topic}的程序编排中，“{left['code']}”与“{right['code']}”之间依次安排了哪些指令？", span,
                [row["code"] for row in span[1:-1]])

    parameter_rows = [row for row in rows if row["selector"] and selectors[row["selector"]] == 1
                      and not CONTROL.match(row["code"]) and PARAMETER.search(row["code"])]
    for left_index, left in enumerate(parameter_rows):
        left_parameters = dict(PARAMETER.findall(left["code"]))
        for right in parameter_rows[left_index+1:]:
            if left["code"].split()[0] != right["code"].split()[0]:
                continue
            right_parameters = dict(PARAMETER.findall(right["code"]))
            differing = [key for key in left_parameters if key in right_parameters
                         and left_parameters[key] != right_parameters[key]]
            if differing:
                add("parameter_comparison", f"在{topic}中，{left['selector']} 与 {right['selector']} 的 {'、'.join(differing)} 参数配置有什么差异？",
                    [left, right], [left["code"], right["code"]])

    # Round-robin intents before proceeding to additional rows in the same task.
    order = ["instruction_constraint", "branch_condition", "parameter_comparison",
             "instruction_sequence", "signal_wait", "parameter_values",
             "action_to_instruction", "instruction_role"]
    pools = [deque(result[intent]) for intent in order]
    output = []
    while any(pools):
        for pool in pools:
            if pool:
                output.append(pool.popleft())
    return output


def validate_sample(case):
    question = case["question"]
    if (not re.search(r"[\u4e00-\u9fff]", question) or "\n" in question
            or "\r" in question or len(question) > 1000
            or question[-1:] != "？" or len(re.findall(r"[?？]", question)) != 1
            or SOURCE_HINT.search(question)):
        raise ValueError(f"Invalid sample question: {case['id']}")
    reference = case["references"][0]
    text = plain(reference["excerpt"])
    if digest(reference["excerpt"]) != case["task_content_digest"]:
        raise ValueError(f"Changed task evidence: {case['id']}")
    for field in ("anchors", "expected_facts"):
        if not case[field] or any(not plain(quote) or plain(quote) not in text for quote in case[field]):
            raise ValueError(f"Ungrounded {field}: {case['id']}")


def generate(source_root, count=1000):
    source_root = Path(source_root).resolve()
    source_path = source_root / BANK_RELATIVE
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source_cases = source.get("cases", [])
    if not source.get("complete") or source.get("task_count") != len(source_cases):
        raise ValueError("Require the complete authored task bank")
    if count < len(source_cases):
        raise ValueError("Count must cover every source task at least once")
    sources = {(case["tasks_json"], case["references"][0]["sha256"]) for case in source_cases}
    for path, expected in sources:
        if sha256(source_root / relative_source(path)) != expected:
            raise ValueError(f"Task corpus changed since authoring: {path}")

    pools = [(case, deque(candidate_questions(case))) for case in source_cases]
    samples, used_questions = [], set()
    while len(samples) < count:
        previous = len(samples)
        for original, candidates in pools:
            while candidates:
                candidate = candidates.popleft()
                key = question_key(candidate["question"])
                if key in used_questions:
                    continue
                sample = deepcopy(original)
                sample.update(candidate)
                sample["id"] = "program_sample_" + digest([original["id"], candidate])[0:24]
                sample["source_bank_case_id"] = original["id"]
                sample["generation_method"] = "deterministic_task_fact_question"
                for field in ("source_md", "tasks_json"):
                    sample[field] = relative_source(sample[field])
                for reference in sample["references"]:
                    for field in ("markdown", "tasks_json"):
                        if field in reference:
                            reference[field] = relative_source(reference[field])
                validate_sample(sample)
                samples.append(sample)
                used_questions.add(key)
                break
            if len(samples) == count:
                break
        if previous == len(samples):
            raise ValueError(f"Only {len(samples)} distinct grounded questions available, requested {count}")
    source_ids = {sample["source_bank_case_id"] for sample in samples}
    if source_ids != {case["id"] for case in source_cases}:
        raise ValueError("Some source tasks lack a distinct sample")
    return dict(schema_version=1, dataset_type="program_rag_samples", sample_count=len(samples),
        source_task_count=len(source_ids), complete=True,
        provenance=dict(generator="utils/generate_program_rag_samples.py", generator_version=VERSION,
            source_bank=BANK_RELATIVE.as_posix(), source_bank_sha256=sha256(source_path),
            method="Deterministic questions about explicit task rows, parameter values, branch conditions and contiguous instruction sequences.",
            source_validation="All source task JSON hashes checked against the authored bank; all answer facts and anchors occur in full retained task evidence.",
            scope="Multiple questions per original task; this is not the exhaustive one-question-per-task bank.",
            source_task_count=len(source_cases), source_document_count=len(sources),
            distinct_task_content_count=len({case["task_content_digest"] for case in source_cases}),
            semantic_deduplication="Exact normalized question text; different intents may share source facts. Shared corpus profiles remain separate retrieval targets.",
            review_status="Mechanically grounded synthetic samples; no human acceptance or model-quality score is claimed."),
        distributions={field: dict(sorted(Counter(case[field] for case in samples).items()))
                       for field in ("category", "profile", "intent")}, cases=samples)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT / "evaluation_samples/program_rag_1000.json")
    parser.add_argument("--count", type=int, default=1000)
    args = parser.parse_args()
    root = args.source_root or next((path for path in [Path.cwd(), PROJECT, *PROJECT.parents]
                                     if (path / BANK_RELATIVE).is_file()), None)
    if root is None:
        parser.error("Supply --source-root pointing to the workspace with the saved task bank")
    bank = generate(root, args.count)
    atomic_json(args.output, bank)
    print(json.dumps(dict(output=str(args.output), sample_count=bank["sample_count"],
                          source_task_count=bank["source_task_count"], distributions=bank["distributions"]),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
