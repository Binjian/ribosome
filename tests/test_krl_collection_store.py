"""Collection resumes only complete, unchanged Q&A records across atomic batches."""

import base64
import codecs
import hashlib
import json

import pytest
from utils.krl import krl_collection_store as store_module
from utils.krl.krl_collection_store import CollectionStore, Question, parse_questions


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


def batch_ids(store):
    return [[record.question.id for record in store_module._saved_records(path)]
            for path, _ in store._batches]


def test_insert_earlier_answer_rebatches_in_source_order_preserving_content(tmp_path):
    source = questions(4)
    path = tmp_path / "collected.md"
    prefix = codecs.BOM_UTF8 + "# 历史说明\r\n\r\n".encode("utf-8")
    legacy = legacy_record(source[1]).encode("utf-8")
    path.write_bytes(prefix + legacy)
    store = CollectionStore(path, source, batch_size=2)
    exact = "\n    DEF MAIN()\r\n    END\r\n\n中文末尾空格  \n\n"
    store.save(source[2], exact, {"session_id": "keep-this-session", "custom": "keep-metadata"})
    second_path = store.save(source[3], "末尾答案")
    second_prefix, _ = store_module._read_batch(second_path)
    existing = [record for batch, _ in store._batches for record in store_module._saved_records(batch)]

    assert store.save(source[0], "缺失的第一个答案") == path
    assert batch_ids(store) == [[1, 2], [3, 4]]
    assert path.read_bytes().startswith(prefix)
    assert second_path.read_bytes().startswith(second_prefix)
    combined = path.read_bytes() + second_path.read_bytes()
    assert legacy in combined
    for record in existing:
        assert record.raw in combined
    assert store._saved[3].answer == exact
    assert b'"session_id": "keep-this-session"' in second_path.read_bytes()
    assert b'"custom": "keep-metadata"' in second_path.read_bytes()
    assert store.completed_ids == {1, 2, 3, 4}
    assert not store._journal_path(path).exists()


def test_repair_old_completion_order_is_explicit_and_idempotent(tmp_path):
    source = questions(4)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source, batch_size=2)
    for question in source:
        store.save(question, f"答案{question.id}", {"session_id": f"session-{question.id}"})
    records = dict(store._saved)
    for (batch, _), order in zip(store._batches, ([4, 2], [3, 1])):
        batch.write_bytes(store._prefixes[batch] + b"\n\n".join(records[number].raw for number in order))
    before = {batch: batch.read_bytes() for batch, _ in store._batches}
    inspected = CollectionStore(path, source, batch_size=2)
    assert batch_ids(inspected) == [[4, 2], [3, 1]]
    assert all(batch.read_bytes() == content for batch, content in before.items())

    inspected.ensure_order()
    assert batch_ids(inspected) == [[1, 2], [3, 4]]
    assert all(inspected._saved[number].answer == record.answer for number, record in records.items())
    after = {batch: batch.read_bytes() for batch, _ in inspected._batches}
    inspected.ensure_order()
    assert all(batch.read_bytes() == content for batch, content in after.items())


@pytest.mark.parametrize("failure", [OSError("disk error"), KeyboardInterrupt()])
def test_failed_cross_batch_insertion_rolls_back_all_files(tmp_path, monkeypatch, failure):
    source = questions(5)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source, batch_size=2)
    for question in source[1:]:
        store.save(question, f"答案{question.id}")
    before = {batch: batch.read_bytes() for batch, _ in store._batches}
    second_path = path.with_name("collected_002.md")
    original_atomic = store_module._atomic_bytes
    failed = False

    def fail_second_batch(target, content):
        nonlocal failed
        if target == second_path and not failed:
            failed = True
            # The first batch has already changed, so restoring only this
            # failing file would lose or duplicate completed records.
            assert path.read_bytes() != before[path]
            raise failure
        original_atomic(target, content)

    monkeypatch.setattr(store_module, "_atomic_bytes", fail_second_batch)
    with pytest.raises(type(failure)):
        store.save(source[0], "新答案")
    assert all(batch.read_bytes() == content for batch, content in before.items())
    assert not path.with_name("collected_003.md").exists()
    assert not store._journal_path(path).exists()
    assert CollectionStore(path, source, batch_size=2).completed_ids == {2, 3, 4, 5}
    store.save(source[0], "新答案")
    assert batch_ids(store) == [[1, 2], [3, 4], [5]]


def test_recover_crashed_reorder_restores_snapshot_and_removes_new_batch(tmp_path):
    source = questions(5)
    path = tmp_path / "collected.md"
    store = CollectionStore(path, source, batch_size=2)
    for question in source[1:]:
        store.save(question, f"答案{question.id}")
    before = {batch: batch.read_bytes() for batch, _ in store._batches}
    extra = path.with_name("collected_003.md")
    snapshots = {"version": 1, "files": [
        {"name": batch.name, "before": base64.b64encode(content).decode("ascii")}
        for batch, content in before.items()
    ] + [{"name": extra.name, "before": None}]}
    journal = store._journal_path(path)
    journal.write_text(json.dumps(snapshots), encoding="utf-8")
    path.write_bytes(b"Interrupted rewrite")
    extra.write_bytes(b"New batch from interrupted rewrite")

    with pytest.raises(ValueError, match="Interrupted ordering update"):
        CollectionStore(path, source, batch_size=2)
    assert path.read_bytes() == b"Interrupted rewrite"
    assert extra.exists() and journal.exists()
    CollectionStore.recover(path)
    assert all(batch.read_bytes() == content for batch, content in before.items())
    assert not extra.exists() and not journal.exists()
    assert CollectionStore(path, source, batch_size=2).completed_ids == {2, 3, 4, 5}


def test_order_follows_source_sequence_instead_of_completion_or_numeric_order(tmp_path):
    values = questions(3)
    source = [values[2], values[0], values[1]]
    store = CollectionStore(tmp_path / "collected.md", source, batch_size=2)
    for question in reversed(source):
        store.save(question, f"答案{question.id}")
    assert batch_ids(store) == [[3, 1], [2]]


def _record_metadata(path):
    text = path.read_bytes().decode("utf-8-sig")
    return [json.loads(line[len("<!-- krl-qa-record "):-4])
            for line in text.splitlines() if line.startswith("<!-- krl-qa-record ")]


def _original_marked_record(question, answer, *, with_length):
    """Create a pre-heading-normalization record, independent of current save()."""
    hashed = answer if with_length else answer.replace("\r\n", "\n").replace("\r", "\n").strip()
    metadata = {
        "id": question.id,
        "question_sha256": question.sha256,
        "answer_sha256": hashlib.sha256(hashed.encode("utf-8")).hexdigest(),
        "session_id": "original-session",
        "custom": {"retain": [1, "原始元数据"]},
    }
    if with_length:
        metadata["answer_length"] = len(answer)
    marker = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    return (f"## 样本{question.id:04d}：{question.title}\n"
            f"<!-- krl-qa-record {marker} -->\n\n"
            f"### 问题\n\n{question.prompt}\n\n"
            f"### 小库原始回答\n\n{answer}\n\n"
            f"<!-- krl-qa-complete {question.id:04d} -->\n"), metadata


@pytest.mark.parametrize("root_level", [2, 3])
def test_answer_headings_nest_under_answer_label_and_roundtrip_exactly(tmp_path, root_level):
    source = questions(1)
    path = tmp_path / "collected.md"
    root = "#" * root_level
    child = "#" * (root_level + 1)
    answer = (f"\n  {root} 一级 ##  \r\n\r\n"
              "正文尾部空格  \r\n"
              f"{child}\t二级\r\n"
              "```markdown\r\n## 代码中的标题\r\n~~~\r\n```\r\n"
              "~~~~markdown\r\n# 另一段代码中的标题\r\n```\r\n~~~~\r\n"
              "    ## 缩进代码\r\n"
              "##没有分隔空格\r\n正文中的 # 字符\r\n\n")
    rendered = answer.replace(f"  {root} 一级", "  #### 一级").replace(
        f"{child}\t二级", "#####\t二级")

    store = CollectionStore(path, source)
    store.save(source[0], answer)
    assert ("### 小库原始回答\n\n" + rendered + "\n\n<!-- krl-qa-complete").encode("utf-8") in path.read_bytes()
    metadata = _record_metadata(path)[0]
    assert metadata["answer_heading_levels"] == [root_level, root_level + 1]
    assert metadata["answer_sha256"] == hashlib.sha256(answer.encode("utf-8")).hexdigest()
    assert metadata["answer_length"] == len(answer)
    assert store_module._saved_records(path)[0].answer == answer
    before = path.read_bytes()
    CollectionStore(path, source).save(source[0], answer)
    assert path.read_bytes() == before


def test_heading_depth_clamps_at_six_without_losing_original_levels(tmp_path):
    source = questions(1)
    path = tmp_path / "collected.md"
    answer = "# 主节\n\n## 次节\n\n##### 深节\n\n###### 最深节\n"
    rendered = "#### 主节\n\n##### 次节\n\n###### 深节\n\n###### 最深节\n"
    store = CollectionStore(path, source)
    store.save(source[0], answer)
    assert rendered.encode("utf-8") in path.read_bytes()
    assert _record_metadata(path)[0]["answer_heading_levels"] == [1, 2, 5, 6]
    assert store_module._saved_records(path)[0].answer == answer
    CollectionStore(path, source).save(source[0], answer)


@pytest.mark.parametrize("before,after", [("#### 一级", "### 一级"), ("##### 二级", "#### 二级")])
def test_modified_visible_heading_fails_integrity_validation(tmp_path, before, after):
    source = questions(1)
    path = tmp_path / "collected.md"
    CollectionStore(path, source).save(source[0], "## 一级\n\n### 二级\n正文")
    original = path.read_bytes()
    assert before.encode("utf-8") in original
    path.write_bytes(original.replace(before.encode("utf-8"), after.encode("utf-8")))
    with pytest.raises(ValueError):
        CollectionStore(path, source)


@pytest.mark.parametrize("with_length", [False, True])
def test_migrate_legacy_and_old_marked_headings_preserves_answers_and_metadata(tmp_path, with_length):
    source = questions(2)
    path = tmp_path / "collected.md"
    prefix = codecs.BOM_UTF8 + "# 历史说明\r\n\r\n".encode("utf-8")
    legacy_answer = "### 旧一级\n\n正文  \n\n#### 旧二级\n内容"
    marked_answer = "## 原始一级\r\n\r\n### 原始二级\r\n原始正文  \r\n结尾"
    marked, metadata = _original_marked_record(source[1], marked_answer, with_length=with_length)
    original = prefix + (legacy_record(source[0], legacy_answer) + "\n\n" + marked).encode("utf-8")
    path.write_bytes(original)

    store = CollectionStore(path, source)
    # Opening an old collection remains read-only, including during dry runs.
    assert path.read_bytes() == original
    store.ensure_heading_hierarchy()
    migrated = path.read_bytes()
    assert migrated.startswith(prefix)
    assert "#### 旧一级\n\n正文  \n\n##### 旧二级".encode("utf-8") in migrated
    assert "#### 原始一级\r\n\r\n##### 原始二级".encode("utf-8") in migrated
    assert [record.answer for record in store_module._saved_records(path)] == [legacy_answer, marked_answer]
    migrated_metadata = {value["id"]: value for value in _record_metadata(path)}
    assert all(migrated_metadata[2][key] == value for key, value in metadata.items())
    if not with_length:
        assert "answer_length" not in migrated_metadata[2]
    assert store.completed_ids == {1, 2}
    assert store.pending(start=1) == []
    CollectionStore(path, source).ensure_heading_hierarchy()
    assert path.read_bytes() == migrated
    assert not CollectionStore._journal_path(path).exists()


@pytest.mark.parametrize("failure", [OSError("migration write failed"), KeyboardInterrupt()])
def test_heading_migration_rolls_back_all_batches_on_failure(tmp_path, monkeypatch, failure):
    source = questions(2)
    first = tmp_path / "collected.md"
    second = tmp_path / "collected_002.md"
    answers = ["## 一级\n\n### 二级\n第一个正文", "### 主节\n第二个正文"]
    for path, question, answer in zip((first, second), source, answers):
        path.write_text("# 批次说明\n\n" + legacy_record(question, answer), encoding="utf-8")
    before = {path: path.read_bytes() for path in (first, second)}
    original_atomic = store_module._atomic_bytes
    failed = False

    def fail_second_batch(path, content):
        nonlocal failed
        if path == second and not failed:
            failed = True
            assert first.read_bytes() != before[first]
            raise failure
        original_atomic(path, content)

    monkeypatch.setattr(store_module, "_atomic_bytes", fail_second_batch)
    store = CollectionStore(first, source, batch_size=1)
    with pytest.raises(type(failure)):
        store.ensure_heading_hierarchy()
    assert failed
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not CollectionStore._journal_path(first).exists()
    resumed = CollectionStore(first, source, batch_size=1)
    assert [resumed._saved[question.id].answer for question in source] == answers
    resumed.ensure_heading_hierarchy()
    assert all(path.read_bytes() != content for path, content in before.items())
    assert [record.answer for path in (first, second)
            for record in store_module._saved_records(path)] == answers
