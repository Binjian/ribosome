"""Offline KRL程序理解 upload, training, routing, and resumable collection checks."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import requests
from utils.krl import collect_kqa_qa as kqa
from utils.krl import collect_krl_qa as collector
from utils.krl import collect_krlu_qa as krlu
from utils.krl import generate_krlu_questions as generator
from utils.krl.krl_collection_store import _saved_records

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "; FILE: MAIN.src\nDEF MAIN()\n  DECL INT count\n  count = 2\n  $OUT[10] = count > 1\nEND\n"
DATA = "; FILE: MAIN.dat\nDEFDAT MAIN\n  DECL INT offset = 1\nENDDAT\n"
PROMPT = f"请解释输出与变量的关系。\n\n```krl\n{SOURCE}```\n\n```krl\n{DATA}```\n"
ANSWER = "## 执行解释\n\n```krl\n  $OUT[10] = TRUE\n```\n\n| 变量 | 值 |\n| --- | --- |\n| count | 2 |\n"
QUESTION = collector.Question(1, "变量与输出", PROMPT)


def _response(data=None, *, answer=None, code="0"):
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Type": "application/json" if answer is None else "text/plain"}
    response.json.return_value = {"code": code, "data": data}
    response.iter_content.return_value = iter([answer.encode("utf-8")]) if answer is not None else iter([])
    response.__enter__.return_value = response
    return response


def _project_responses(*, training=2, file_status=0, session_id="project-session"):
    return [
        _response({"sessionId": session_id}),
        _response(["uploaded-main-src", "uploaded-main-dat"]),
        _response(code=1),
        _response({}),
        _response({"status": training}),
        _response([{"name": "MAIN.src", "status": file_status}, {"name": "MAIN.dat", "status": file_status}]),
    ]


def test_defaults_and_generated_bank_have_one_thousand_distinct_program_prompts():
    args = krlu.build_parser().parse_args([])
    assert args.questions == ROOT / "assets/KRLU/krlu_questions_1000.md"
    assert args.output == ROOT / "assets/KRLU/krlu_qa_collected.md"
    assert args.start == 1
    assert krlu.PROFILE.robot_model is None
    assert krlu.PROFILE.stream_path == "/api/krl/stream"
    questions = collector.parse_questions(args.questions)
    assert [question.id for question in questions] == list(range(1, 1001))
    assert args.questions.read_text(encoding="utf-8") == generator.render()
    assert len(generator.TOPICS) == 50
    assert all(sum(question.title.startswith(title + "｜") for question in questions) == 20
               for title, _ in generator.TOPICS)
    # Neither identifiers in metadata/module/helper names nor case labels can
    # turn identical program-and-question content into distinct examples.
    prompts = [re.sub(
        r"\b[A-Z]+_\d{4}\b|案例\d+", "IDENTIFIER",
        re.sub(r"【样本\d+[^】]*】", "", question.prompt),
    ) for question in questions]
    assert len(set(prompts)) == 1000
    for question in questions:
        assert krlu.program_files(question)
    assert collector.KRL_PROFILE.stream_path == kqa.PROFILE.stream_path == "/api/efs/stream"
    assert collector.build_parser().parse_args([]).start == 19
    assert args.output != kqa.PROFILE.output != collector.KRL_PROFILE.output


@pytest.mark.parametrize("module", [False, True])
def test_dry_run_and_help_work_from_another_directory_without_outputs(module, tmp_path):
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if module:
        environment["PYTHONPATH"] = str(ROOT)
        entry = ["-m", "utils.krl.collect_krlu_qa"]
    else:
        entry = [str(ROOT / "utils/krl/collect_krlu_qa.py")]
    for flags in (["--help"], ["--dry-run", "--concurrency", "1", "--output", str(tmp_path / "absent" / "qa.md")]):
        result = subprocess.run(
            [sys.executable, *entry, *flags], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "KRL程序理解" in result.stdout
        if "--dry-run" in flags:
            assert "Source: 1000 questions; saved: 0; pending: 1000" in result.stdout
            assert "stream: /api/krl/stream" in result.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("reused_project", [False, True])
def test_project_files_are_uploaded_verbatim_then_trained_before_streaming(reused_project, monkeypatch):
    sleeps = []
    monkeypatch.setattr(krlu.time, "sleep", sleeps.append)
    session = Mock()
    create, upload, previous, train, status, files = _project_responses()
    opening = [create]
    if reused_project:
        opening = [
            _response(code=1), _response({"TODAY": [{"sessionId": "project-session"}]}),
            _response({"questionInfos": []}), _response([]), _response(code="1"),
        ]
    session.request.side_effect = [
        *opening, upload, previous, train, _response(code=1), status, files,
        _response(answer=ANSWER), _response({"status": True}),
        _response({"answerContent": ANSWER, "id": "project-answer", "time": "server-time"}),
    ]
    answer, metadata = krlu.KrluClient(session, profile=krlu.PROFILE).ask(QUESTION)
    assert answer == ANSWER
    assert metadata["mode"] == "KRL程序理解"
    assert metadata["robot_model"] is None
    assert metadata["stream_path"] == "/api/krl/stream"
    assert metadata["answer_id"] == "project-answer"
    assert sleeps == [2.0]
    create, *lookup, upload, previous, train, pending, status, files, stream, check, persisted = (
        session.request.call_args_list
    )
    assert create.kwargs["json"] == {"sessionTitle": "新建对话"}
    if reused_project:
        assert [call.args[1].removeprefix(collector.CHAT_URL) for call in lookup] == [
            "/api/session/select", "/api/question/select", "/api/krl/selectFilesInfo", "/api/krl/queryStatus",
        ]
        assert lookup[1].kwargs["json"] == lookup[2].kwargs["json"] == {"sessionId": "project-session"}
        assert lookup[3].kwargs["json"] == {"requestId": "project-session"}
    else:
        assert not lookup
    assert upload.args == ("POST", collector.CHAT_URL + "/api/uploadFiles")
    assert upload.kwargs["files"] == [
        ("files", ("MAIN.src", SOURCE.encode("utf-8"), "application/octet-stream")),
        ("files", ("MAIN.dat", DATA.encode("utf-8"), "application/octet-stream")),
    ]
    assert "Content-Type" not in upload.kwargs["headers"]
    assert "json" not in upload.kwargs
    assert previous == pending == status
    assert status.kwargs["json"] == {"requestId": "project-session"}
    training = train.kwargs["json"]
    assert training["filesUrl"] == ["uploaded-main-src", "uploaded-main-dat"]
    assert training["sessionId"] == training["requestId"] == "project-session"
    assert training["trainType"] == 1
    assert re.fullmatch(r"[a-f0-9]{8}(?:_[a-f0-9]{4}){3}_[a-f0-9]{12}", training["skillId"])
    assert files.kwargs["json"] == {"sessionId": "project-session"}
    assert stream.args == ("POST", collector.CHAT_URL + "/api/krl/stream")
    payload = stream.kwargs["json"]
    assert set(payload) == {"question", "sessionId", "uuid", "requestId", "repeatFlag"}
    assert payload["question"] == PROMPT
    assert payload["sessionId"] == payload["requestId"] == "project-session"
    assert payload["repeatFlag"] is False
    assert check.kwargs["json"] == {"content": ANSWER, "uuid": payload["uuid"]}
    assert persisted.args == ("POST", collector.CHAT_URL + "/api/krl/selectQuestionInfo")
    assert persisted.kwargs["json"] == {
        "question": PROMPT, "content": PROMPT, "sessionId": "project-session",
        "requestId": "project-session", "uuid": payload["uuid"],
    }


@pytest.mark.parametrize("project_state", [
    ([{"name": "earlier.src", "status": 0}],),
    ({"files": []},),
    ([], {"status": 1}),
    ([], {"status": 2}),
    (None, {"status": 3}),
])
def test_project_recovery_preserves_existing_files_or_training(project_state):
    session = Mock()
    session.request.side_effect = [
        _response(code=1), _response({"TODAY": [{"sessionId": "project-session"}]}),
        _response({"questionInfos": []}), *(_response(state) for state in project_state),
    ]
    with pytest.raises(collector.CollectionError, match=r"saveOrUpdate.*code=1"):
        krlu.KrluClient(session).ask(QUESTION)
    paths = [call.args[1].removeprefix(collector.CHAT_URL) for call in session.request.call_args_list]
    assert paths[:4] == [
        "/api/session/saveOrUpdate", "/api/session/select", "/api/question/select", "/api/krl/selectFilesInfo",
    ]
    if len(project_state) == 2:
        assert paths[4:] == ["/api/krl/queryStatus"]
    else:
        assert len(paths) == 4


def test_pending_training_and_eventual_file_visibility_are_polled(monkeypatch):
    sleeps = []
    monkeypatch.setattr(krlu.time, "sleep", sleeps.append)
    client = krlu.KrluClient(Mock(), profile=krlu.PROFILE)
    client.api = Mock(side_effect=[
        ["src-url", "dat-url"], {"status": 1}, {}, {"status": "1"}, {"status": "2"}, [],
        [{"name": "MAIN.src"}, {"name": "MAIN.dat", "status": 0}],
        [{"name": "MAIN.src", "status": 0}, {"name": "MAIN.dat", "status": 0}],
    ])
    client.prepare_question(QUESTION, {"sessionId": "s", "requestId": "r"})
    assert sleeps == [2.0, 2.0, 2.0]
    assert client.api.call_args_list[2].args[1]["trainType"] == 2


@pytest.mark.parametrize("failure", ["training", "file", "upload", "status", "file_names"])
def test_failed_preparation_never_streams_or_checks_an_answer(failure):
    responses = _project_responses(training=3 if failure == "training" else 2,
                                   file_status=1 if failure == "file" else 0)
    if failure == "upload":
        responses[1] = _response([])
    if failure == "status":
        responses[4] = _response({"status": True})
    if failure == "file_names":
        responses[5] = _response([{"name": "other.src", "status": 0}])
    session = Mock()
    session.request.side_effect = responses
    with pytest.raises(collector.CollectionError):
        krlu.KrluClient(session, profile=krlu.PROFILE).ask(QUESTION)
    assert all(call.args[1] not in (collector.CHAT_URL + "/api/krl/stream", collector.CHAT_URL + "/api/check")
               for call in session.request.call_args_list)


def test_training_timeout_bounds_polling_and_request_timeouts(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(krlu.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(krlu.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    client = krlu.KrluClient(Mock(), answer_timeout=3, profile=krlu.PROFILE)
    client.api = Mock(side_effect=[["src", "dat"], None, {}, {"status": 1}, {"status": 1}])
    with pytest.raises(collector.CollectionError, match="preparation exceeded"):
        client.prepare_question(QUESTION, {"sessionId": "s", "requestId": "r"})
    assert now[0] == 3
    assert all(max(call.kwargs["timeout"]) <= 3 for call in client.api.call_args_list)


def test_progress_wrapper_without_an_answer_retries_generation():
    wrapper = "<div class='prompt'>\n</div>\n"
    complete = wrapper + ANSWER
    session = Mock()
    session.request.side_effect = [
        *_project_responses(session_id="empty-answer"), _response(answer=wrapper),
        *_project_responses(), _response(answer=complete), _response({"status": True}),
        _response({"answerContent": complete, "id": "verified-answer"}),
    ]
    answer, metadata = krlu.KrluClient(session, retries=1, retry_delay=0).ask(QUESTION)
    assert answer == complete
    assert metadata["session_id"] == "project-session"
    assert metadata["answer_id"] == "verified-answer"


def test_transient_upload_failure_restarts_fresh_session_but_fetch_retry_does_not_retrain(monkeypatch):
    monkeypatch.setattr(collector.time, "sleep", lambda _: None)
    session = Mock()
    session.request.side_effect = [
        _response({"sessionId": "abandoned"}), requests.ConnectionError("interrupted upload"),
        *_project_responses(), _response(answer=ANSWER), _response({"status": True}),
        _response({"answerContent": "partial"}), _response({"answerContent": ANSWER}),
    ]
    answer, metadata = krlu.KrluClient(session, retries=1, retry_delay=0, profile=krlu.PROFILE).ask(QUESTION)
    assert answer == ANSWER
    paths = [call.args[1].removeprefix(collector.CHAT_URL) for call in session.request.call_args_list]
    assert paths.count("/api/session/saveOrUpdate") == 2
    assert paths.count("/api/uploadFiles") == 2
    assert paths.count("/api/krl/train") == 1
    assert paths.count("/api/krl/stream") == paths.count("/api/check") == 1
    assert paths.count("/api/krl/selectQuestionInfo") == 2
    assert metadata["session_id"] == "project-session"


@pytest.mark.parametrize("error", [requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout,
                                  requests.exceptions.ReadTimeout])
def test_0416_stream_retry_reuses_training_only_if_submission_was_unsent(error, tmp_path, monkeypatch):
    question = next(q for q in collector.parse_questions(krlu.PROFILE.questions) if q.id == 416)
    output = tmp_path / "qa.md"
    expected = "本次调用后 `STATE=#RUNNING`、`COUNT=15`。同一次 `SWITCH` 只执行 `CASE #IDLE`。"

    def prepared(session_id):
        responses = _project_responses(session_id=session_id)
        responses[-1] = _response([
            {"name": name, "status": 0} for name, _ in krlu.program_files(question)
        ])
        return responses

    reused = error is not requests.exceptions.ReadTimeout
    request = Mock(side_effect=[
        *prepared("original-project"), error("private proxy details"),
        *([] if reused else prepared("fresh-project")),
        _response(answer=expected), _response({"status": True}),
        _response({"answerContent": expected, "id": "verified-0416"}),
    ])
    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(collector, "load_credentials", lambda _: ("test-account", "test-password"))
    monkeypatch.setattr(krlu.KrluClient, "login", lambda *args, **kwargs: None)
    assert krlu.main([
        "--start", "416", "--end", "416", "--output", str(output),
        "--concurrency", "1", "--retries", "1", "--retry-delay", "0",
    ]) == 0

    record, = _saved_records(output)
    assert record.question == question and record.answer == expected
    assert record.metadata["answer_id"] == "verified-0416"
    by_path = {}
    for call in request.call_args_list:
        by_path.setdefault(call.args[1].removeprefix(collector.CHAT_URL), []).append(call)
    preparation_count = 1 if reused else 2
    for path in ("/api/session/saveOrUpdate", "/api/uploadFiles", "/api/krl/train"):
        assert len(by_path[path]) == preparation_count
    first, second = by_path["/api/krl/stream"]
    original, retried = first.kwargs["json"], second.kwargs["json"]
    assert first.kwargs["allow_redirects"] is second.kwargs["allow_redirects"] is False
    assert original["question"] == retried["question"] == question.prompt
    if reused:
        assert original == retried
    else:
        assert retried["sessionId"] == "fresh-project" and original["uuid"] != retried["uuid"]
    check, = by_path["/api/check"]
    verified, = by_path["/api/krl/selectQuestionInfo"]
    assert check.kwargs["json"]["uuid"] == verified.kwargs["json"]["uuid"] == retried["uuid"]


@pytest.mark.parametrize("replacement", [
    "```krl\nDEF MAIN()\nEND\n```",
    "```krl\n; FILE: ../bad.src\nDEF MAIN()\nEND\n```",
    "```krl\n; FILE: MAIN.src\nDEF MAIN()\nEND\n",
    "```krl\n; FILE: MAIN.dat\nDEFDAT MAIN\nENDDAT\n```",
    PROMPT + "\n```krl\n; FILE: main.SRC\nDEF MAIN()\nEND\n```\n",
])
def test_invalid_source_is_rejected_offline_before_creating_any_output(replacement, tmp_path, monkeypatch):
    source = tmp_path / "questions.md"
    source.write_text("## 0001 检查\n\n" + replacement, encoding="utf-8")
    output = tmp_path / "absent" / "qa.md"
    monkeypatch.setattr(collector, "load_credentials", lambda *_: pytest.fail("invalid input requested login"))
    for flags in ([], ["--dry-run"]):
        assert krlu.main(["--questions", str(source), "--output", str(output), "--concurrency", "1", *flags]) == 1
        assert not output.parent.exists()


@pytest.mark.parametrize("concurrency", [1, 2])
def test_collection_keeps_krlu_client_in_workers_preserves_code_and_resumes(concurrency, tmp_path, monkeypatch):
    source = tmp_path / "questions.md"
    source.write_text("\n".join(f"## {number:04d} 程序理解{number}\n\n{PROMPT}" for number in range(1, 4)), encoding="utf-8")
    output = tmp_path / "answers" / "qa.md"
    arguments = ["--questions", str(source), "--output", str(output), "--batch-size", "1",
                 "--concurrency", str(concurrency), "--delay", "0"]
    asked, logins = [], []

    def login(client, username, password, *, accept_terms):
        assert isinstance(client, krlu.KrluClient)
        assert accept_terms is True
        logins.append(client.session)

    def ask(client, question):
        assert isinstance(client, krlu.KrluClient)
        assert client.profile == krlu.PROFILE
        assert krlu.program_files(question) == [("MAIN.src", SOURCE.encode()), ("MAIN.dat", DATA.encode())]
        asked.append(question.id)
        return ANSWER, {"mode": client.profile.mode, "robot_model": None}

    monkeypatch.setattr(requests.Session, "request", lambda *_, **__: pytest.fail("live request"))
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-account", "test-password"))
    monkeypatch.setattr(krlu.KrluClient, "login", login)
    monkeypatch.setattr(krlu.KrluClient, "ask", ask)
    assert krlu.main([*arguments, "--max-questions", "2"]) == 0
    before = {path: path.read_bytes() for path in output.parent.glob("qa*.md")}
    for path in before:
        record, = _saved_records(path)
        assert record.answer == ANSWER
        assert SOURCE in record.question.prompt and DATA in record.question.prompt
        assert record.metadata["mode"] == "KRL程序理解"
        assert path.read_text(encoding="utf-8").startswith("# KRLU程序理解问答采集\n")
    assert krlu.main(arguments) == 0
    assert sorted(asked) == [1, 2, 3]
    assert all(path.read_bytes() == content for path, content in before.items())
    assert krlu.main(arguments) == 0
    assert sorted(asked) == [1, 2, 3]
    assert len(logins) == 2


def test_create_timeouts_and_rejections_stop_without_blacklisting_questions(tmp_path, monkeypatch, caplog):
    source = tmp_path / "questions.md"
    source.write_text("\n".join(f"## {number:04d} 程序理解{number}\n\n{PROMPT}" for number in range(1, 5)),
                      encoding="utf-8")
    questions = collector.parse_questions(source)
    output = tmp_path / "qa.md"
    store = collector.CollectionStore(output, questions, title=krlu.PROFILE.title, mode=krlu.PROFILE.mode)
    store.save(questions[0], ANSWER, {"session_id": "saved-project"})
    previous = output.read_bytes()
    login = Mock()
    first_timeout = iter([requests.exceptions.ReadTimeout("private response details")])
    pauses = []

    def request(session, method, url, **kwargs):
        if url.endswith("/api/session/select"):
            return _response({})
        assert url.endswith("/api/session/saveOrUpdate")
        if timeout := next(first_timeout, None):
            raise timeout
        return _response(code=1)

    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(collector, "load_credentials", lambda _: ("test-account", "test-password"))
    monkeypatch.setattr(krlu.KrluClient, "login", login)
    monkeypatch.setattr(collector, "time", SimpleNamespace(sleep=pauses.append))
    assert krlu.main([
        "--questions", str(source), "--output", str(output), "--concurrency", "3",
        "--delay", "0", "--retries", "1", "--retry-delay", "0",
    ], retry_failed_runs=True) == 1

    assert login.call_count == 4
    assert [delay for delay in pauses if delay] == [10, 20, 40]
    assert "Continuing with concurrency 1" in caplog.text
    assert "POST /api/session/saveOrUpdate: HTTP read timeout" in caplog.text
    assert "private response details" not in caplog.text
    assert not output.with_suffix(".failed.json").exists()
    assert output.read_bytes() == previous


@pytest.mark.parametrize("concurrency", [1, 3])
def test_failed_questions_are_skipped_on_resume_and_can_be_retried(concurrency, tmp_path, monkeypatch, capsys):
    source = tmp_path / "questions.md"
    source.write_text("\n".join(f"## {number:04d} 程序理解{number}\n\n{PROMPT}" for number in range(1, 5)),
                      encoding="utf-8")
    questions = collector.parse_questions(source)
    output = tmp_path / "qa.md"
    report = output.with_suffix(".failed.json")
    arguments = ["--questions", str(source), "--output", str(output), "--concurrency", str(concurrency),
                 "--delay", "0", "--retries", "1", "--retry-delay", "0"]
    login = Mock()
    asked = []
    failing = {1, 3}

    def ask(client, question):
        asked.append(question.id)
        if question.id in failing:
            raise collector.UnverifiedAnswerError("Stream and saved answer differ")
        return ANSWER, {}

    monkeypatch.setattr(collector, "load_credentials", lambda _: ("test-account", "test-password"))
    monkeypatch.setattr(krlu.KrluClient, "login", login)
    monkeypatch.setattr(krlu.KrluClient, "ask", ask)
    assert krlu.main(arguments, retry_failed_runs=True) == 0
    assert sorted(asked) == [1, 1, 2, 3, 3, 4]
    assert json.loads(report.read_text(encoding="utf-8")) == [
        {"id": question.id, "title": question.title, "prompt": question.prompt,
         "reason": "Stream and saved answer differ"}
        for question in (questions[0], questions[2])
    ]
    assert [record.question.id for record in _saved_records(output)] == [2, 4]
    assert krlu.main([*arguments, "--dry-run"]) == 0
    assert "saved: 2; pending: 0" in capsys.readouterr().out
    asked.clear()
    assert krlu.main(arguments) == 0
    assert not asked and login.call_count == 1

    failing.clear()
    assert krlu.main([*arguments, "--retry-failed"]) == 0
    assert sorted(asked) == [1, 3]
    assert [record.question.id for record in _saved_records(output)] == [1, 2, 3, 4]
    assert json.loads(report.read_text(encoding="utf-8")) == []


def test_normal_resume_recovers_legacy_creation_failures_only(tmp_path, monkeypatch):
    source = tmp_path / "questions.md"
    source.write_text(f"## 0416 First\n\n{PROMPT}\n## 0417 Second\n\n{PROMPT}", encoding="utf-8")
    first, second = collector.parse_questions(source)
    output = tmp_path / "qa.md"
    report = collector.FailedQuestions(output)
    report.record(first, "/api/session/saveOrUpdate: API rejected request (code=1)")
    report.record(second, "Stream and saved answer differ")
    ask = Mock(return_value=(ANSWER, {}))
    monkeypatch.setattr(collector, "load_credentials", lambda _: ("test-account", "test-password"))
    monkeypatch.setattr(krlu.KrluClient, "login", Mock())
    monkeypatch.setattr(krlu.KrluClient, "ask", ask)
    assert krlu.main(["--questions", str(source), "--output", str(output), "--start", "416",
                      "--concurrency", "1"]) == 0
    ask.assert_called_once_with(first)
    assert [entry["id"] for entry in json.loads(report.path.read_text())] == [417]


@pytest.mark.parametrize("case", ["ready", "wrong-question", "missing-dat", "duplicate", "training"])
def test_recover_unasked_numbered_project_without_uploading_again(case):
    question = next(q for q in collector.parse_questions(krlu.PROFILE.questions) if q.id == 416)
    files = [{"name": name, "status": 0} for name, _ in krlu.program_files(question)]
    if case == "wrong-question":
        files[0]["name"] = "KRLU_0417.src"
    elif case == "missing-dat":
        files.pop()
    elif case == "duplicate":
        files[1] = files[0]
    responses = [_response(code=1), _response({"TODAY": [{"sessionId": "orphan"}]}),
                 _response({"questionInfos": []}), _response(files)]
    if case in ("ready", "training"):
        responses.append(_response({"status": 2 if case == "ready" else 1}))
    if case == "ready":
        responses.extend([_response(answer=ANSWER), _response({"status": True}), _response({"answerContent": ANSWER})])
    session = Mock()
    session.request.side_effect = responses
    client = krlu.KrluClient(session, retries=0)
    if case == "ready":
        answer, meta = client.ask(question)
        assert answer == ANSWER and meta["session_id"] == "orphan"
    else:
        with pytest.raises(collector.ApiRejectedError):
            client.ask(question)
    assert session.request.call_count == len(responses)


def test_interrupted_training_preserves_completed_pair_and_resume_retrains_only_missing(tmp_path, monkeypatch):
    source = tmp_path / "questions.md"
    source.write_text(f"## 0001 第一题\n\n{PROMPT}\n## 0002 第二题\n\n{PROMPT}", encoding="utf-8")
    output = tmp_path / "qa.md"
    arguments = ["--questions", str(source), "--output", str(output), "--batch-size", "1",
                 "--concurrency", "1", "--delay", "0"]
    successful = [*_project_responses(), _response(answer=ANSWER), _response({"status": True}),
                  _response({"answerContent": ANSWER})]
    interrupted = [*_project_responses(session_id="interrupted-project")[:4], KeyboardInterrupt()]
    request = Mock(side_effect=[*successful, *interrupted])
    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-account", "test-password"))
    monkeypatch.setattr(krlu.KrluClient, "login", lambda *args, **kwargs: None)
    assert krlu.main(arguments) == 130
    previous = output.read_bytes()
    record, = _saved_records(output)
    assert record.question.id == 1 and record.answer == ANSWER
    assert not output.with_suffix(".failed.json").exists()
    assert not output.with_name("qa_002.md").exists()
    paths = [call.args[1] for call in request.call_args_list]
    assert paths.count(collector.CHAT_URL + "/api/krl/train") == 2
    assert paths.count(collector.CHAT_URL + "/api/krl/stream") == 1

    request.reset_mock(side_effect=True)
    request.side_effect = [*_project_responses(), _response(answer=ANSWER), _response({"status": True}),
                           _response({"answerContent": ANSWER})]
    assert krlu.main(arguments) == 0
    assert output.read_bytes() == previous
    paths = [call.args[1] for call in request.call_args_list]
    assert paths.count(collector.CHAT_URL + "/api/session/saveOrUpdate") == 1
    assert paths.count(collector.CHAT_URL + "/api/uploadFiles") == 1
    assert paths.count(collector.CHAT_URL + "/api/krl/train") == 1
    saved, = _saved_records(output.with_name("qa_002.md"))
    assert saved.question.id == 2 and saved.answer == ANSWER
