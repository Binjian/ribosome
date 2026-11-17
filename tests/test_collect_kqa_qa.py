"""Offline checks for the KUKA Center 智能问答 collection entry point."""

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
import requests

from utils.krl import collect_kqa_qa as kqa
from utils.krl import collect_krl_qa as collector
from utils.krl.krl_collection_store import _saved_records


ROOT = Path(__file__).resolve().parents[1]
ANSWER = "## 检查步骤\n\n先确认控制柜型号与 KSS 版本。  \n\n| 项目 | 记录 |\n| --- | --- |\n| 软件 | 待核实 |\n"


def _response(data=None, *, answer=None):
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Type": "application/json" if answer is None else "text/plain"}
    response.json.return_value = {"code": "0", "data": data}
    response.iter_content.return_value = iter([answer.encode("utf-8")]) if answer is not None else iter([])
    response.__enter__.return_value = response
    return response


def test_kqa_defaults_and_question_bank_are_complete_and_distinct():
    args = kqa.build_parser().parse_args([])
    assert args.questions == ROOT / "assets/KQA/kqa_questions_1000.md"
    assert args.output == ROOT / "assets/KQA/kqa_qa_collected.md"
    assert args.start == 1
    assert kqa.PROFILE.robot_model == "intelligent_customer_service_robot"
    assert kqa.PROFILE.answer_path == "/api/krl/selectQuestionInfo"
    questions = collector.parse_questions(args.questions)
    assert [question.id for question in questions] == list(range(1, 1001))
    # Sample identifiers alone must not make repeated prompts appear distinct.
    prompts = [re.sub(r"【样本\d+[^】]*】", "", question.prompt) for question in questions]
    assert len(set(prompts)) == 1000
    assert all(question.title and question.prompt.strip() for question in questions)
    krl_args = collector.build_parser().parse_args([])
    assert krl_args.start == 19
    assert krl_args.output == ROOT / "assets/KRL/krl_qa_collected.md"


@pytest.mark.parametrize("module", [False, True])
def test_kqa_dry_run_from_another_directory_does_not_create_output(module, tmp_path):
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if module:
        environment["PYTHONPATH"] = str(ROOT)
        entry = ["-m", "utils.krl.collect_kqa_qa"]
    else:
        entry = [str(ROOT / "utils/krl/collect_kqa_qa.py")]
    output = tmp_path / "absent" / "qa.md"
    result = subprocess.run(
        [sys.executable, *entry, "--dry-run", "--concurrency", "1", "--output", str(output)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Source: 1000 questions; saved: 0; pending: 1000" in result.stdout
    assert "Next: 0001 " in result.stdout
    assert not list(tmp_path.iterdir())


def test_kqa_help_works_without_local_settings_from_another_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "utils/krl/collect_kqa_qa.py"), "--help"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "智能问答" in result.stdout
    assert "--concurrency" in result.stdout
    assert not list(tmp_path.iterdir())


def test_kqa_ask_selects_smart_mode_and_checks_persisted_answer():
    question = collector.Question(1, "控制系统版本", "如何确认控制柜型号与 KSS 版本？")
    session = Mock()
    session.request.side_effect = [
        _response({"sessionId": "smart-session"}),
        _response(answer=ANSWER),
        _response({"status": True}),
        _response({"answerContent": ANSWER, "id": "smart-answer", "time": "server-time"}),
    ]
    answer, metadata = collector.KukaClient(session, profile=kqa.PROFILE).ask(question)
    assert answer == ANSWER
    assert metadata["mode"] == "智能问答"
    assert metadata["robot_model"] == "intelligent_customer_service_robot"
    assert metadata["answer_check_passed"] is True
    assert metadata["answer_id"] == "smart-answer"
    create, stream, check, persisted = session.request.call_args_list
    assert create.args == ("POST", collector.CHAT_URL + "/api/session/saveOrUpdate")
    assert stream.args == ("POST", collector.CHAT_URL + "/api/efs/stream")
    payload = stream.kwargs["json"]
    assert payload["robotModel"] == "intelligent_customer_service_robot"
    assert payload["question"] == question.prompt
    assert payload["requestId"] == payload["sessionId"] == "smart-session"
    assert payload["networkSearchFlag"] is False
    assert payload["repeatFlag"] is False
    assert check.kwargs["json"] == {"content": ANSWER, "uuid": payload["uuid"]}
    assert persisted.args == ("POST", collector.CHAT_URL + "/api/krl/selectQuestionInfo")
    assert persisted.kwargs["json"] == {
        "question": question.prompt, "content": question.prompt,
        "sessionId": "smart-session", "requestId": "smart-session", "uuid": payload["uuid"],
    }


@pytest.mark.parametrize("concurrency", [1, 2])
def test_kqa_collects_from_one_and_resumes_with_matching_mode_for_all_workers(
    concurrency, tmp_path, monkeypatch,
):
    source = tmp_path / "questions.md"
    source.write_text("\n".join(
        f"## {number:04d} 操作问题{number}\n\n如何检查设备项目{number}？\n"
        for number in range(1, 4)
    ), encoding="utf-8")
    output = tmp_path / "answers" / "qa.md"
    arguments = [
        "--questions", str(source), "--output", str(output), "--batch-size", "1",
        "--concurrency", str(concurrency), "--delay", "0",
    ]
    asked = []
    logins = []

    def fail_network(*args, **kwargs):
        raise AssertionError("Offline KQA test attempted an HTTP request")

    def login(client, *args, **kwargs):
        assert client.profile == kqa.PROFILE
        logins.append(client.session)

    def ask(client, question):
        assert client.profile == kqa.PROFILE
        asked.append(question.id)
        return ANSWER, {"mode": client.profile.mode, "robot_model": client.profile.robot_model}

    monkeypatch.setattr(requests.Session, "request", fail_network)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-account", "test-password"))
    monkeypatch.setattr(collector.KukaClient, "login", login)
    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    assert kqa.main([*arguments, "--max-questions", "2"]) == 0
    before = {path: path.read_bytes() for path in output.parent.glob("qa*.md")}
    assert len(before) == 2
    for path in before:
        text = path.read_text(encoding="utf-8")
        assert "KQA" in text.splitlines()[0]
        assert "智能问答模式" in text
        assert "KRL编程模式" not in text
        record, = _saved_records(path)
        assert record.answer == ANSWER
        assert record.metadata["mode"] == "智能问答"
        assert record.metadata["robot_model"] == kqa.PROFILE.robot_model

    assert kqa.main(arguments) == 0
    assert sorted(asked) == [1, 2, 3]
    assert len(logins) == 2
    assert all(path.read_bytes() == content for path, content in before.items())
    assert len(list(output.parent.glob("qa*.md"))) == 3
    assert kqa.main(arguments) == 0
    assert sorted(asked) == [1, 2, 3]
    assert len(logins) == 2


def test_kqa_gap_insertion_preserves_existing_intro_and_labels_new_batches(tmp_path):
    questions = [collector.Question(number, f"检查{number}", f"如何检查项目{number}？")
                 for number in range(1, 4)]
    output = tmp_path / "qa.md"
    store = collector.CollectionStore(
        output, questions, batch_size=1, title=kqa.PROFILE.title, mode=kqa.PROFILE.mode,
    )
    metadata = {"mode": "智能问答", "robot_model": "intelligent_customer_service_robot"}
    store.save(questions[2], ANSWER, metadata)
    old_text = output.read_text(encoding="utf-8")
    old_text = old_text.replace("\n\n", "\n\n保留本地采集说明。\n\n", 1)
    output.write_text(old_text, encoding="utf-8")
    prefix = old_text.split("## 样本", 1)[0]
    store.save(questions[0], "第一条完整回答", metadata)
    store.save(questions[1], "第二条完整回答", metadata)
    assert output.read_text(encoding="utf-8").startswith(prefix)
    records = []
    for path in sorted(tmp_path.glob("qa*.md")):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# " + kqa.PROFILE.title + "\n")
        assert "智能问答模式" in text
        assert "KRL编程模式" not in text
        if path != output:
            assert "保留本地采集说明。" not in text
        records.extend(_saved_records(path))
    assert [record.question.id for record in records] == [1, 2, 3]
    assert records[-1].answer == ANSWER
    assert records[-1].metadata["robot_model"] == "intelligent_customer_service_robot"
