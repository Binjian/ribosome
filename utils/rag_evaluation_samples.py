"""Load and audit supplemental evaluation samples without running a model."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unicodedata


DATASETS = {"regular_object_rag_samples", "program_rag_samples"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)


def plain(value):
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    # A code quotation can contain operators such as <>. Parsing already-plain
    # code as HTML would split these into separate text fragments.
    if not re.search(r"</?(?:table|tbody|tr|td|th|p|div|span|b|i|strong|em|ul|ol|li|br|pre|code)\b", value):
        return " ".join(unescape(value).replace("**", "").split())
    parser = _Text()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).replace("**", "").split())


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _task_contents(tree):
    """Index task identities/content, including mirrored wrapper occurrences."""
    found = set()
    if isinstance(tree, dict):
        if tree.get("type") == "task" and "id" in tree:
            found.add((digest(tree["id"]), digest(tree.get("content", ""))))
        for value in tree.values():
            found.update(_task_contents(value))
    elif isinstance(tree, list):
        for value in tree:
            found.update(_task_contents(value))
    return found


def load_sample_bank(path, *, source_root=None):
    """Validate a supplemental bank; optionally verify referenced files and lines.

    Repeated source tasks are intentional. This is separate from the exhaustive
    one-question-per-task bank accepted by task_rag_questions.load_task_bank.
    """
    bank = json.loads(Path(path).read_text(encoding="utf-8"))
    if bank.get("schema_version") != 1 or bank.get("dataset_type") not in DATASETS:
        raise ValueError("Expected a supplemental RAG sample bank, schema_version=1")
    cases = bank.get("cases")
    if not isinstance(cases, list) or not cases or bank.get("sample_count") != len(cases):
        raise ValueError("Sample count does not match nonempty cases")
    ids, questions, sources = set(), set(), {}
    task_identities, task_contents = set(), {}
    required = {"id", "category", "kind", "profile", "revision", "question",
                "heading_path", "source_md", "anchors", "expected_facts", "references"}
    forbidden = re.compile(
        r"根据手册|根据《|请依据|《[^》]*手册[^》]*》|SX\d{5,}|"
        r"第[\d.一二三四五六七八九十]+[章节]|\bV\d+\.\d+|"
        r"\.(?:md|json|docx)\b|(?:对象\s*ID|行号)|[（(][ABC]-\d", re.I)
    for case in cases:
        case_id = case.get("id", "<missing>")

        def require(condition, message):
            if not condition:
                raise ValueError(f"{case_id}: {message}")

        require(required <= case.keys(), "Missing evaluation fields")
        require(isinstance(case["category"], str) and case["category"], "Missing category")
        require(case["profile"] in ("4.0", "5.0"), "Unknown corpus profile")
        require(isinstance(case["heading_path"], list)
                and all(isinstance(title, str) for title in case["heading_path"]),
                "Invalid heading path")
        require(isinstance(case_id, str) and case_id and case_id not in ids, "Duplicate/empty ID")
        ids.add(case_id)
        question = case["question"]
        require(isinstance(question, str) and 8 <= len(question) <= 1000,
                "Question must contain 8–1000 characters")
        require("\n" not in question and "\r" not in question, "Question must be one line")
        require(bool(re.search(r"[\u4e00-\u9fff]", question)), "Expected a Chinese question")
        require(question.endswith(("?", "？")) and len(re.findall(r"[?？]", question)) == 1,
                "Expected one complete question")
        require(not forbidden.search(question), "Question leaks a source address")
        normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", question))
        require(normalized not in questions, "Duplicate question after normalization")
        questions.add(normalized)
        references = case["references"]
        require(isinstance(references, list) and references, "Missing source references")
        evidence = " ".join(plain(ref["excerpt"]) for ref in references)
        require(evidence, "Empty evidence")
        for key in ("anchors", "expected_facts"):
            require(isinstance(case[key], list), f"{key} must be a list")
            require(key != "expected_facts" or case[key], "Missing expected facts")
            require(all(isinstance(value, str) and plain(value) and plain(value) in evidence
                        for value in case[key]), f"{key} not grounded in reference excerpt")
        for ref in references:
            require(ref.get("markdown") == case["source_md"], "Reference Markdown differs from case")
            source = ref.get("tasks_json") or ref.get("markdown")
            require(source and re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", "")),
                    "Missing source path/SHA-256")
            if source_root is not None:
                if source not in sources:
                    source_path = Path(source_root) / source
                    raw = source_path.read_bytes()
                    sources[source] = (hashlib.sha256(raw).hexdigest(), raw)
                actual_hash, raw = sources[source]
                require(actual_hash == ref["sha256"], f"Source hash mismatch: {source}")
                if not ref.get("tasks_json") and ref.get("line_start") is not None:
                    lines = raw.decode("utf-8-sig").splitlines()
                    start, end = ref["line_start"], ref["line_end"]
                    require(isinstance(start, int) and isinstance(end, int)
                            and 1 <= start <= end <= len(lines), "Invalid source line range")
                    require("\n".join(lines[start - 1:end]).strip() == ref["excerpt"].strip(),
                            "Excerpt does not match the exact source line range")
        if bank["dataset_type"] == "program_rag_samples":
            require(all(case.get(key) for key in
                        ("task_object_id", "tasks_json", "task_content_digest")),
                    "Missing task identity")
            require("task_id" in case and case["kind"] == "task", "Invalid task case")
            require(digest(references[0]["excerpt"]) == case["task_content_digest"],
                    "Task content digest mismatch")
            require(references[0].get("tasks_json") == case["tasks_json"]
                    and references[0].get("task_id") == case["task_id"]
                    and references[0].get("object_id") == case["task_object_id"],
                    "Task identity differs between case and reference")
            if source_root is not None:
                source = case["tasks_json"]
                if source not in task_contents:
                    task_contents[source] = _task_contents(json.loads(sources[source][1]))
                require((digest(case["task_id"]), case["task_content_digest"])
                        in task_contents[source], "Task ID/content absent from source JSON")
            task_identities.add((case["tasks_json"], case["profile"], digest(case["task_id"])))
    if bank["dataset_type"] == "program_rag_samples":
        if bank.get("source_task_count") != len(task_identities):
            raise ValueError("Source task count does not match task identities")
    return bank


def audit_summary(bank):
    cases = bank["cases"]
    return dict(dataset_type=bank["dataset_type"], sample_count=len(cases),
                unique_questions=len({case["question"] for case in cases}),
                source_count=len({case["source_md"] for case in cases}),
                categories=dict(sorted(Counter(case["category"] for case in cases).items())),
                profiles=dict(sorted(Counter(case["profile"] for case in cases).items())),
                kinds=dict(sorted(Counter(case["kind"] for case in cases).items())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("banks", nargs="+", type=Path)
    parser.add_argument("--source-root", type=Path,
                        help="Root containing assets; also verify file hashes and exact excerpts")
    args = parser.parse_args()
    for path in args.banks:
        bank = load_sample_bank(path, source_root=args.source_root)
        print(json.dumps(dict(path=str(path), **audit_summary(bank)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
