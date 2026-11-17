"""Collection resumes only complete, unchanged Q&A records across atomic batches."""

import hashlib
import json

import pytest
from utils import krl_collection_store as store_module
from utils.krl_collection_store import CollectionStore, Question, parse_questions


def questions(count=5):
    return [Question(index, f"工艺{index}", f"问题{index}\n\n参数：$OUT[10]")
            for index in range(1, count + 1)]


def legacy_record(question, answer="旧答案\n\n```krl\nEND\n```"):
    return (f"## 样本{question.id:04d}：{question.title}\n\n"
            f"### 问题\n\n{question.prompt}\n\n"
            f"### 小库原始回答\n\n{answer}")


def edit_metadata(path, change):
    before, record = path.read_bytes().decode("utf-8").split("<!-- krl-qa-record ", 1)
    marker, after = record.split(" -->", 1)
    metadata = json.loads(marker)
    change(metadata)
    path.write_bytes((before + "<!-- krl-qa-record " + json.dumps(metadata) + " -->" + after).encode("utf-8"))


def test_parse_ignores_heading_inside_fence_and_preserves_multiline_prompt(tmp_path):
    path = tmp_path / "questions.md"
    prompt = "第一段\n\n```markdown\n## 9999 伪问题\n```\n\n第二段"
    path.write_text(f"# 清单\n\n## 0019 真空搬运\n\n{prompt}\n\n"
                    "## 0020 其他工艺\n\n最后一个问题\n", encoding="utf-8")
    parsed = parse_questions(path)
    assert parsed == [Question(19, "真空搬运", prompt), Question(20, "其他工艺", "最后一个问题")]


@pytest.mark.parametrize("body", ["## 0001 空问题\n", "## 0001 A\n\nq\n## 0001 B\nq"])
def test_parse_rejects_empty_or_duplicate_questions(tmp_path, body):
    path = tmp_path / "questions.md"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        parse_questions(path)


def test_resume_legacy_then_roll_over_without_changing_existing_bytes(tmp_path):
    source = questions()
    path = tmp_path / "krl_qa_collected.md"
    before = ("# 历史说明\r\n\r\n" + legacy_record(source[0])).encode("utf-8")
    path.write_bytes(before)
    store = CollectionStore(path, source, batch_size=2)
    assert store.completed_ids == {1}
    assert store.pending(start=2) == source[1:]
    assert store.save(source[1], "# 新答案\n\n|A|B|\n|-|-|\n|1|2|") == path
    assert path.read_bytes().startswith(before)
    second = store.save(source[2], "```krl\nPTP HOME\n```\n\n**完成**")
    assert second.name == "krl_qa_collected_002.md"
    resumed = CollectionStore(path, source, batch_size=2)
    assert resumed.completed_ids == {1, 2, 3}
    assert resumed.pending(start=2, end=4) == [source[3]]
    assert "```krl\nPTP HOME\n```" in second.read_text(encoding="utf-8")


def test_answer_can_contain_record_heading_and_labels(tmp_path):
    source = questions(2)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source)
    answer = "## 样本0001：工艺1\n\n### 问题\n重复标题\n\n### 小库原始回答\n完整正文"
    store.save(source[0], answer)
    store.save(source[1], "另一个答案")
    assert CollectionStore(path, source).completed_ids == {1, 2}


def test_unclosed_answer_code_fence_does_not_hide_next_record(tmp_path):
    source = questions(2)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source)
    store.save(source[0], "完整但格式有误的原始回答\n\n```krl\nEND")
    store.save(source[1], "另一个答案")
    assert CollectionStore(path, source).completed_ids == {1, 2}


def test_preserve_exact_leading_indentation_trailing_spaces_and_newlines(tmp_path):
    source = questions(2)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source)
    answer = "\n    DEF A()\r\n    END\r\n\n段落末尾两个空格保留换行  \n下一行\n\n"
    store.save(source[0], answer)
    assert ("### 小库原始回答\n\n" + answer + "\n\n<!-- krl-qa-complete").encode("utf-8") in path.read_bytes()
    resumed = CollectionStore(path, source)
    before = path.read_bytes()
    resumed.save(source[0], answer)
    assert path.read_bytes() == before
    resumed.save(source[1], "下一个答案")
    assert CollectionStore(path, source).completed_ids == {1, 2}


def test_older_marked_record_without_answer_length_still_resumes(tmp_path):
    source = questions(2)
    path = tmp_path / "collected.md"
    CollectionStore(path, source).save(source[0], "旧记录的答案")
    edit_metadata(path, lambda metadata: metadata.pop("answer_length"))
    resumed = CollectionStore(path, source)
    assert resumed.completed_ids == {1}
    resumed.save(source[1], "后续答案")
    assert CollectionStore(path, source).completed_ids == {1, 2}


def test_exact_hash_cannot_fall_back_to_canonical_hash(tmp_path):
    source = questions(1)
    path = tmp_path / "collected.md"
    answer = "    DEF MAIN()\n    END\n\n"
    CollectionStore(path, source).save(source[0], answer)
    edit_metadata(path, lambda metadata: metadata.update(
        answer_sha256=hashlib.sha256(answer.strip().encode("utf-8")).hexdigest()))
    with pytest.raises(ValueError, match="integrity check failed"):
        CollectionStore(path, source)


@pytest.mark.parametrize("length", [None, True, -1, "5"])
def test_invalid_answer_length_cannot_downgrade_to_old_format(tmp_path, length):
    source = questions(1)
    path = tmp_path / "collected.md"
    CollectionStore(path, source).save(source[0], "完整答案")
    edit_metadata(path, lambda metadata: metadata.update(answer_length=length))
    with pytest.raises(ValueError, match="Invalid answer length"):
        CollectionStore(path, source)


def test_no_duplicate_and_no_silent_overwrite_or_changed_source(tmp_path):
    source = questions(1)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source)
    store.save(source[0], "完整答案")
    before = path.read_bytes()
    store.save(source[0], "完整答案")
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="replace"):
        store.save(source[0], "不同答案")
    with pytest.raises(ValueError, match="differs"):
        CollectionStore(path, [Question(1, source[0].title, "修改的问题")])
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", [
    lambda value: value.replace("完整答案", "被改答案"),
    lambda value: value.split("<!-- krl-qa-complete")[0],
])
def test_integrity_or_truncation_failure_is_not_completed(tmp_path, mutation):
    source = questions(1)
    path = tmp_path / "collected.md"
    CollectionStore(path, source).save(source[0], "完整答案")
    damaged = mutation(path.read_text(encoding="utf-8"))
    path.write_text(damaged, encoding="utf-8")
    with pytest.raises(ValueError, match="integrity|Incomplete"):
        CollectionStore(path, source)
    assert path.read_text(encoding="utf-8") == damaged


def test_failed_publication_preserves_previous_and_retries(tmp_path, monkeypatch):
    source = questions(2)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source)
    store.save(source[0], "旧答案")
    before = path.read_bytes()
    original = store_module.os.replace

    def fail_replace(*args):
        raise OSError("simulated disk error")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        store.save(source[1], "新答案")
    assert path.read_bytes() == before
    assert store.completed_ids == {1}
    assert list(tmp_path.glob("*.tmp")) == []
    monkeypatch.setattr(store_module.os, "replace", original)
    store.save(source[1], "新答案")
    assert CollectionStore(path, source).completed_ids == {1, 2}


def test_detect_external_update_before_save(tmp_path):
    source = questions(2)
    path = tmp_path / "collected.md"
    first = CollectionStore(path, source)
    second = CollectionStore(path, source)
    second.save(source[0], "第一个答案")
    first.save(source[1], "第二个答案")
    assert CollectionStore(path, source).completed_ids == {1, 2}


def test_reject_changed_source_and_truncated_or_modified_answers(tmp_path):
    source = questions(2)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source)
    store.save(source[0], "原始答案")
    before = path.read_bytes()
    changed = [Question(1, source[0].title, "输入已修改"), source[1]]
    with pytest.raises(ValueError, match="differs from the source"):
        CollectionStore(path, changed)
    with pytest.raises(ValueError, match="replace an existing answer"):
        store.save(source[0], "覆盖答案")
    assert path.read_bytes() == before
    path.write_bytes(before.replace("原始答案".encode("utf-8"), "已改答案".encode("utf-8")))
    with pytest.raises(ValueError, match="integrity check failed"):
        CollectionStore(path, source)
    path.write_bytes(before[:before.index(b"<!-- krl-qa-complete")])
    with pytest.raises(ValueError, match="Incomplete saved answer"):
        CollectionStore(path, source)


def test_reject_gapped_or_overfull_batches(tmp_path):
    source = questions(3)
    path = tmp_path / "collected.md"
    path.with_name("collected_002.md").write_text(legacy_record(source[0]), encoding="utf-8")
    with pytest.raises(ValueError, match="gap"):
        CollectionStore(path, source)
    path.with_name("collected_002.md").unlink()
    path.write_text("\n\n".join(legacy_record(value) for value in source), encoding="utf-8")
    with pytest.raises(ValueError, match="number of records"):
        CollectionStore(path, source, batch_size=2)
