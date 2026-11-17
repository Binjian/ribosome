"""Offline KUKA frontend protocol tests; no live login or model requests."""

import asyncio
import builtins
import hashlib
import subprocess
import sys
import textwrap
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import ReadTimeoutError
from utils.krl import collect_krl_qa as collector


class Response:
    def __init__(self, data=None, *, status=200, url="https://www.kukacenter.com/chat",
                 text="", chunks=(), content_type="text/plain", content=b""):
        self.data = data
        self.status_code = status
        self.url = url
        self.text = text
        self.chunks = chunks
        self.headers = {"Content-Type": content_type}
        self.content = content
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def iter_content(self, chunk_size):
        assert chunk_size > 0
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class Session:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, f"Unexpected network request: {method} {url}"
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def api(data, code="0"):
    return Response({"code": code, "data": data}, content_type="application/json")


QUESTION = collector.Question(19, "真空搬运", "完整问题：$OUT[10]，请提供 SRC/DAT。")
ANSWER = "### 参数\n\n| 参数 | 值 |\n| --- | --- |\n| 名称 | 真空 |\n\n```krl\n$OUT[10]=TRUE\n```\n"
FORM = '<form id="fm1"><input type="hidden" name="execution" value="flow&amp;token"><input type="hidden" name="_eventId" value="submit"></form>'
LOGIN_URL = "https://sso.kukacenter.com/cas/login?service=example"


def answer_session(*, answer=ANSWER, saved=None, check=True, chunks=None,
                   session_id="session-19"):
    if chunks is None:
        chunks = [answer.encode("utf-8")]
    responses = [api({"sessionId": session_id}), Response(chunks=chunks), api({"status": check})]
    if check:
        responses.append(api({"answerContent": answer if saved is None else saved,
                              "id": "answer-19", "time": "server-time"}))
    return Session(responses)


def test_utf8_stream_preserves_full_markdown_across_arbitrary_byte_boundaries():
    encoded = ANSWER.encode("utf-8")
    response = Response(chunks=[bytes([byte]) for byte in encoded])
    assert collector.read_stream(response, 10) == ANSWER


@pytest.mark.parametrize("chunks", [[], [b"", b" \n\t"]])
def test_empty_stream_cannot_be_a_completed_answer(chunks):
    with pytest.raises(collector.CollectionError, match="empty"):
        collector.read_stream(Response(chunks=chunks), 10)


@pytest.mark.parametrize("content_type", ["application/json", "text/html; charset=utf-8"])
def test_stream_error_document_is_rejected(content_type):
    with pytest.raises(collector.CollectionError, match="error/login"):
        collector.read_stream(Response(chunks=[b"error"], content_type=content_type), 10)


def test_stream_timeout_and_invalid_utf8_are_not_successes(monkeypatch):
    ticks = iter([0, 20])
    monkeypatch.setattr(collector.time, "monotonic", lambda: next(ticks))
    with pytest.raises(collector.CollectionError, match="answer-timeout"):
        collector.read_stream(Response(chunks=[b"partial"]), 10)
    monkeypatch.setattr(collector.time, "monotonic", lambda: 0)
    with pytest.raises(UnicodeDecodeError):
        collector.read_stream(Response(chunks=[b"\xe7"]), 10)


def test_ask_selects_krl_mode_checks_answer_and_fetches_matching_original():
    session = answer_session()
    answer, metadata = collector.KukaClient(session).ask(QUESTION)
    assert answer == ANSWER
    assert metadata["mode"] == "KRL编程"
    assert metadata["answer_id"] == "answer-19"
    assert metadata["answer_check_passed"] is True
    assert len(session.calls) == 4
    create, stream, check, persisted = session.calls
    assert all(call[2]["headers"] == {
        "Origin": collector.CHAT_URL, "Referer": collector.CHAT_URL + "/",
    } for call in session.calls)
    assert create[1].endswith("/api/session/saveOrUpdate")
    assert stream[:2] == ("POST", collector.CHAT_URL + "/api/efs/stream")
    payload = stream[2]["json"]
    assert payload["question"] == QUESTION.prompt
    assert payload["robotModel"] == "krl_question_robot"
    assert payload["requestId"] == payload["sessionId"] == "session-19"
    assert payload["repeatFlag"] is False and payload["networkSearchFlag"] is False
    assert stream[2]["stream"] is True
    assert check[2]["json"] == {"content": ANSWER, "uuid": payload["uuid"]}
    assert persisted[2]["json"] == {
        "question": QUESTION.prompt, "content": QUESTION.prompt,
        "sessionId": "session-19", "requestId": "session-19", "uuid": payload["uuid"],
    }
    assert not session.responses


def test_interrupted_stream_never_checks_or_fetches_a_partial_answer():
    session = answer_session(chunks=[b"partial", RuntimeError("connection interrupted")])
    with pytest.raises(RuntimeError, match="interrupted"):
        collector.KukaClient(session).ask(QUESTION)
    assert len(session.calls) == 2


def test_generation_uses_answer_timeout_instead_of_api_idle_timeout():
    session = answer_session()
    collector.KukaClient(session, request_timeout=13, answer_timeout=53).ask(QUESTION)
    assert session.calls[1][2]["timeout"] == (20, 53)
    assert all(call[2]["timeout"] == (20, 13)
               for index, call in enumerate(session.calls) if index != 1)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_failure_retries_generation(status):
    failed = Response(status=status)
    session = answer_session()
    session.responses.appendleft(failed)
    answer, _ = collector.KukaClient(session, retries=1, retry_delay=0).ask(QUESTION)
    assert answer == ANSWER
    assert failed.closed
    assert len(session.calls) == 5
    assert session.calls[0][1] == session.calls[1][1]
    assert not session.responses


@pytest.mark.parametrize("error", [
    requests.ConnectionError("private-token"),
    requests.Timeout("private-token"),
    requests.exceptions.ChunkedEncodingError("private-token"),
])
def test_transient_stream_failure_restarts_with_fresh_conversation(error):
    failed_stream = Response(chunks=[b"incomplete answer", error])
    session = answer_session(session_id="retry-session")
    session.responses.extendleft(reversed([
        api({"sessionId": "failed-session"}), failed_stream,
    ]))
    answer, metadata = collector.KukaClient(session, retries=1, retry_delay=0).ask(QUESTION)
    assert answer == ANSWER
    assert metadata["session_id"] == "retry-session"
    assert failed_stream.closed
    assert len(session.calls) == 6
    first, retry = session.calls[1][2]["json"], session.calls[3][2]["json"]
    assert first["sessionId"] == "failed-session"
    assert retry["sessionId"] == "retry-session"
    assert first["uuid"] != retry["uuid"]
    assert session.calls[4][2]["json"] == {"content": ANSWER, "uuid": retry["uuid"]}
    assert not session.responses


@pytest.mark.parametrize("stage", ["check", "persisted"])
@pytest.mark.parametrize("failure", [requests.ConnectionError("private-token"), Response(status=503)])
def test_answer_verification_retry_reuses_complete_stream_and_identifiers(stage, failure):
    session = answer_session()
    responses = list(session.responses)
    responses.insert(2 if stage == "check" else 3, failure)
    session.responses = deque(responses)
    answer, metadata = collector.KukaClient(session, retries=1, retry_delay=0).ask(QUESTION)
    assert answer == ANSWER
    assert metadata["session_id"] == "session-19"
    assert len(session.calls) == 5
    assert sum(call[1].endswith("/api/session/saveOrUpdate") for call in session.calls) == 1
    assert sum(call[1].endswith("/api/efs/stream") for call in session.calls) == 1
    retry_index = 2 if stage == "check" else 3
    assert session.calls[retry_index] == session.calls[retry_index + 1]
    assert not session.responses


def test_check_and_persisted_fetch_each_have_their_own_retry_budget():
    session = Session([
        api({"sessionId": "session-19"}), Response(chunks=[ANSWER.encode("utf-8")]),
        requests.Timeout("check failed"), api({"status": True}),
        requests.ConnectionError("fetch failed"), api({"answerContent": ANSWER}),
    ])
    answer, _ = collector.KukaClient(session, retries=1, retry_delay=0).ask(QUESTION)
    assert answer == ANSWER
    assert len(session.calls) == 6
    assert session.calls[2] == session.calls[3]
    assert session.calls[4] == session.calls[5]
    assert not session.responses


@pytest.mark.parametrize("status", [400, 401, 403, 404, 463])
def test_permanent_http_error_does_not_retry(status):
    failed = Response(status=status)
    session = Session([failed])
    with pytest.raises(collector.CollectionError, match=f"HTTP {status}"):
        collector.KukaClient(session, retries=3, retry_delay=0).ask(QUESTION)
    assert len(session.calls) == 1
    assert failed.closed


def test_ssl_error_does_not_retry_generation():
    session = Session([requests.exceptions.SSLError("private-token")])
    with pytest.raises(collector.CollectionError):
        collector.KukaClient(session, retries=3, retry_delay=0).ask(QUESTION)
    assert len(session.calls) == 1


def test_api_rejection_does_not_retry_generation():
    session = Session([api(None, code="error")])
    with pytest.raises(collector.CollectionError, match="API rejected"):
        collector.KukaClient(session, retries=3, retry_delay=0).ask(QUESTION)
    assert len(session.calls) == 1


def test_invalid_utf8_stream_does_not_retry_generation():
    session = answer_session(chunks=[b"\xe7"])
    with pytest.raises(UnicodeDecodeError):
        collector.KukaClient(session, retries=3, retry_delay=0).ask(QUESTION)
    assert len(session.calls) == 2


def test_nested_stream_read_timeout_is_identified_without_exposing_exception_text():
    timeout = ReadTimeoutError(None, "/api/login?signature=private-token", "private-password")
    response = Response(chunks=[b"partial", requests.ConnectionError(timeout)])
    with pytest.raises(collector.CollectionError) as error:
        collector.read_stream(response, 10)
    message = str(error.value).lower()
    assert "timeout" in message or "timed out" in message
    assert "private-token" not in message
    assert "private-password" not in message


def test_saved_answer_mismatch_prevents_publishing():
    session = answer_session(saved="truncated")
    with pytest.raises(collector.CollectionError, match="differ"):
        collector.KukaClient(session).ask(QUESTION)
    assert len(session.calls) == 4


def test_saved_answer_comparison_keeps_significant_first_line_indentation():
    session = answer_session(answer="    $OUT[10]=TRUE\n", saved="$OUT[10]=TRUE\n")
    with pytest.raises(collector.CollectionError, match="differ"):
        collector.KukaClient(session).ask(QUESTION)
    matching = answer_session(answer="    $OUT[10]=TRUE  \n")
    answer, _ = collector.KukaClient(matching).ask(QUESTION)
    assert answer == "    $OUT[10]=TRUE  \n"


def test_moderation_refusal_does_not_export_rejected_raw_answer():
    session = answer_session(answer="rejected raw answer", check=False)
    answer, metadata = collector.KukaClient(session).ask(QUESTION)
    assert answer == collector.REFUSAL
    assert "rejected raw answer" not in answer
    assert metadata["answer_check_passed"] is False
    assert len(session.calls) == 3


@pytest.mark.parametrize("code", [401, "error", None])
def test_json_error_codes_are_not_answers_or_logged_bodies(code):
    session = Session([Response({"code": code, "msg": "private-token"})])
    with pytest.raises(collector.CollectionError, match="API rejected") as error:
        collector.KukaClient(session).api("/api/session/saveOrUpdate", {})
    assert "private-token" not in str(error.value)


def test_http_failure_closes_response_and_hides_signed_url():
    response = Response(status=503)
    client = collector.KukaClient(Session([response]))
    with pytest.raises(collector.CollectionError, match="HTTP 503") as error:
        client.request("GET", collector.CHAT_URL + "/api/login?signature=private-token")
    assert response.closed
    assert "private-token" not in str(error.value)


def test_main_site_sign_api_uses_gateway_prefix_and_get_nonce():
    signature = {"uuid": "account-uuid", "timestamp": 123, "nonceStr": "nonce", "sign": "signature"}
    session = Session([api(signature)])
    result = collector.KukaClient(session).api(
        "/api-b/program_assistant/api/sign", {"nonceStr": "nonce"}, main_site=True,
    )
    assert result == signature
    assert len(session.calls) == 1
    method, url, options = session.calls[0]
    assert method == "GET"
    assert url == collector.MAIN_URL + "/api/api-b/program_assistant/api/sign"
    assert options["params"] == {"nonceStr": "nonce"}
    assert "json" not in options
    assert not session.responses


def test_sign_http_463_stops_before_chat_login_and_redacts_response():
    failed_sign = Response(
        status=463, text="private-body-token", url=collector.MAIN_URL + "/api/api-b/program_assistant/api/sign?nonceStr=private-query-token",
    )
    session = Session([Response(), failed_sign])
    with pytest.raises(collector.CollectionError, match="HTTP 463") as error:
        collector.KukaClient(session).login("test-user", "private-password")
    assert failed_sign.closed
    assert len(session.calls) == 2
    assert session.calls[-1][1] == collector.MAIN_URL + "/api/api-b/program_assistant/api/sign"
    assert "private-body-token" not in str(error.value)
    assert "private-query-token" not in str(error.value)
    assert "private-password" not in str(error.value)
    assert not session.responses


def test_request_failure_redacts_signed_url_and_credentials(monkeypatch):
    session = Session([])

    def fail(*args, **kwargs):
        raise OSError("request to /cas/login?signature=private-token failed; password=private-password")

    monkeypatch.setattr(session, "request", fail)
    with pytest.raises(collector.CollectionError, match="OSError; request failed") as error:
        collector.KukaClient(session).request(
            "POST", "https://sso.kukacenter.com/cas/login?signature=private-token",
            data={"password": "private-password"},
        )
    assert "private-token" not in str(error.value)
    assert "private-password" not in str(error.value)
    assert "/cas/login" in str(error.value)


def test_login_posts_md5_captcha_and_signed_chat_credentials(monkeypatch, capsys):
    signature = {"uuid": "account-uuid", "timestamp": 123, "nonceStr": "nonce", "sign": "signature"}
    session = Session([
        Response(url=LOGIN_URL, text=FORM),
        Response(content_type="image/png", content=b"captcha-image"),
        Response(), api(signature), api({}),
    ])
    answers = iter(["yes", "abcd"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))
    monkeypatch.setattr(collector.getpass, "getpass", lambda prompt: "test-secret")
    collector.KukaClient(session).login("test-user")
    login_post = session.calls[2]
    fields = login_post[2]["data"]
    assert login_post[:2] == ("POST", LOGIN_URL)
    assert fields["username"] == "test-user"
    assert fields["password"] == hashlib.md5(b"test-secret").hexdigest()
    assert fields["captcha"] == "abcd" and fields["execution"] == "flow&token"
    assert fields["uuid"] == session.calls[1][2]["params"]["uuid"]
    assert fields["loginType"] == "0"
    assert session.calls[3][0] == "GET"
    assert session.calls[3][1] == collector.MAIN_URL + "/api/api-b/program_assistant/api/sign"
    assert session.calls[4][2]["json"] == {
        "uuid": "account-uuid", "timestamp": "123", "nonceStr": "nonce", "signature": "signature",
    }
    assert session.calls[4][2]["headers"] == {
        "Origin": collector.CHAT_URL, "Referer": collector.CHAT_URL + "/",
    }
    output = capsys.readouterr().out
    assert "test-secret" not in output
    captcha_line = next(line for line in output.splitlines() if "CAPTCHA:" in line)
    assert not Path(captcha_line.split(": ", 1)[1]).exists()


def test_declining_terms_never_sends_credentials_or_requests_captcha(monkeypatch):
    session = Session([Response(url=LOGIN_URL, text=FORM)])
    monkeypatch.setattr(builtins, "input", lambda prompt: "no")
    with pytest.raises(collector.CollectionError, match="cancelled"):
        collector.KukaClient(session).login("test-user", "test-secret")
    assert len(session.calls) == 1


def test_unexpected_login_destination_never_receives_credentials():
    session = Session([Response(url="https://unexpected.example/cas/login", text=FORM)])
    with pytest.raises(collector.CollectionError, match="Unexpected login destination"):
        collector.KukaClient(session).login("test-user", "test-secret", accept_terms=True)
    assert len(session.calls) == 1


def test_dry_run_has_no_network_dependency_or_file_writes(tmp_path, monkeypatch, capsys):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n\n## 0020 夹取\n\n第二问题\n", encoding="utf-8")
    output = tmp_path / "uncreated" / "qa.md"
    original_import = builtins.__import__

    def no_requests(name, *args, **kwargs):
        assert name not in ("requests", "dotenv"), "Dry run must not load live dependencies"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_requests)
    assert collector.main(["--questions", str(questions), "--output", str(output), "--dry-run"]) == 0
    assert not output.parent.exists()
    text = capsys.readouterr().out
    assert "pending: 2" in text and "0019 真空搬运" in text


def test_cli_stops_on_second_question_failure_and_resumes_completed_pair(tmp_path, monkeypatch):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n\n## 0020 夹取\n\n第二问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    session = Session([])
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)
    calls = []

    def ask(client, question):
        calls.append(question.id)
        if question.id == 20:
            raise collector.CollectionError("interrupted")
        return ANSWER, {"mode": "KRL编程"}

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    arguments = ["--questions", str(questions), "--output", str(output), "--delay", "0"]
    assert collector.main(arguments) == 1
    saved = output.read_bytes()
    assert calls == [19, 20]
    assert "样本0019" in saved.decode("utf-8") and "样本0020" not in saved.decode("utf-8")
    calls.clear()
    assert collector.main(arguments) == 1
    assert calls == [20]
    assert output.read_bytes() == saved


def test_cli_sign_http_463_preserves_existing_completed_output(tmp_path, monkeypatch):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n\n## 0020 夹取\n\n第二问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    parsed = collector.parse_questions(questions)
    collector.CollectionStore(output, parsed, batch_size=20).save(parsed[0], ANSWER)
    saved = output.read_bytes()
    failed_sign = Response(status=463)
    session = Session([Response(), failed_sign])
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))

    assert collector.main(["--questions", str(questions), "--output", str(output)]) == 1
    assert output.read_bytes() == saved
    assert failed_sign.closed
    assert len(session.calls) == 2
    assert session.calls[-1][1] == collector.MAIN_URL + "/api/api-b/program_assistant/api/sign"
    assert not session.responses


def test_cli_response_read_connection_error_never_logs_signed_url(tmp_path, monkeypatch, caplog):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    session = answer_session(chunks=[
        b"partial answer",
        requests.ConnectionError("read failed for /api/login?signature=private-token; password=private-password"),
    ])
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)
    assert collector.main(["--questions", str(questions), "--output", str(output), "--retries", "0"]) == 1
    assert not output.exists()
    assert "ConnectionError" in caplog.text
    assert "private-token" not in caplog.text
    assert "private-password" not in caplog.text
    assert len(session.calls) == 2


def test_cli_retries_partial_answer_then_saves_once_and_continues(tmp_path, monkeypatch, caplog):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n\n## 0020 夹取\n\n第二问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    failed_stream = Response(chunks=[b"discarded partial answer", requests.ConnectionError("private-token")])
    session = Session([
        api({"sessionId": "failed-session"}), failed_stream,
        *answer_session(session_id="retry-session").responses,
        *answer_session(session_id="next-question").responses,
    ])
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)
    arguments = ["--questions", str(questions), "--output", str(output),
                 "--delay", "0", "--retry-delay", "0", "--retries", "1"]
    assert collector.main(arguments) == 0
    saved = output.read_text(encoding="utf-8")
    assert saved.count("## 样本0019：") == saved.count("## 样本0020：") == 1
    assert "discarded partial answer" not in saved
    store = collector.CollectionStore(output, collector.parse_questions(questions))
    assert store.completed_ids == {19, 20}
    assert failed_stream.closed
    assert "retry" in caplog.text.lower()
    assert "private-token" not in caplog.text
    assert not session.responses


def test_cli_retry_exhaustion_preserves_previous_bytes_and_rerun_resumes_failed_question(tmp_path, monkeypatch):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n\n## 0020 夹取\n\n第二问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    parsed = collector.parse_questions(questions)
    collector.CollectionStore(output, parsed).save(parsed[0], ANSWER)
    previous = output.read_bytes()
    failed_streams = [
        Response(chunks=[b"first partial", requests.ConnectionError("connection lost")]),
        Response(chunks=[b"second partial", requests.exceptions.ChunkedEncodingError("truncated")]),
    ]
    failed = Session([
        api({"sessionId": "first-attempt"}), failed_streams[0],
        api({"sessionId": "second-attempt"}), failed_streams[1],
    ])
    monkeypatch.setattr(requests, "Session", lambda: failed)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)
    arguments = ["--questions", str(questions), "--output", str(output),
                 "--delay", "0", "--retry-delay", "0", "--retries", "1"]
    assert collector.main(arguments) == 1
    assert output.read_bytes() == previous
    assert len(failed.calls) == 4
    assert all(response.closed for response in failed_streams)
    assert not failed.responses
    resumed = answer_session(session_id="resumed-session")
    monkeypatch.setattr(requests, "Session", lambda: resumed)
    assert collector.main(arguments) == 0
    assert output.read_bytes().startswith(previous)
    assert resumed.calls[1][2]["json"]["question"] == parsed[1].prompt
    assert len(resumed.calls) == 4
    assert collector.CollectionStore(output, parsed).completed_ids == {19, 20}


def test_cli_cancel_during_retry_backoff_preserves_completed_output(tmp_path, monkeypatch):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n\n## 0020 夹取\n\n第二问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    parsed = collector.parse_questions(questions)
    collector.CollectionStore(output, parsed).save(parsed[0], ANSWER)
    previous = output.read_bytes()
    failed = Response(chunks=[b"partial", requests.ConnectionError("connection lost")])
    session = Session([api({"sessionId": "failed-session"}), failed])
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)

    def cancel(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(collector.time, "sleep", cancel)
    assert collector.main(["--questions", str(questions), "--output", str(output)]) == 130
    assert output.read_bytes() == previous
    assert len(session.calls) == 2
    assert failed.closed


def test_retry_delay_grows_exponentially_and_is_capped(monkeypatch):
    responses = [Response(status=503) for _ in range(5)]
    session = Session([*responses, *answer_session().responses])
    sleeps = []
    monkeypatch.setattr(collector.time, "sleep", sleeps.append)
    answer, _ = collector.KukaClient(session, retries=5, retry_delay=5).ask(QUESTION)
    assert answer == ANSWER
    assert sleeps == [5, 10, 20, 40, 60]
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("option,value", [
    ("--retries", "-1"), ("--retry-delay", "-1"),
    ("--retry-delay", "nan"), ("--retry-delay", "inf"),
    ("--request-timeout", "inf"), ("--answer-timeout", "nan"),
])
def test_cli_rejects_invalid_retry_and_timeout_settings_before_network(option, value):
    with pytest.raises(SystemExit) as error:
        collector.main([option, value, "--dry-run"])
    assert error.value.code == 2


def test_credentials_use_local_dotenv_from_another_directory(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    local_env = root / "utils/krl/.env"
    local_env.parent.mkdir(parents=True)
    local_env.write_text(
        'KUKACENTER_ACCOUT="file-account"\n'
        'KUKACENTER_PASSWORD=\'literal ${NOT_EXPANDED} #@password\'\n'
        'UNRELATED_KRL_TEST_VARIABLE="not-exported"\n', encoding="utf-8",
    )
    (root / ".env").write_text(
        "KUKACENTER_ACCOUT=root-account\nKUKACENTER_PASSWORD=root-password\n", encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "KUKACENTER_ACCOUT=cwd-account\nKUKACENTER_PASSWORD=cwd-password\n", encoding="utf-8",
    )
    monkeypatch.setattr(collector, "ROOT", root)
    monkeypatch.setattr(collector, "ENV_FILE", local_env)
    monkeypatch.delenv("KUKACENTER_ACCOUT", raising=False)
    monkeypatch.delenv("KUKACENTER_PASSWORD", raising=False)
    monkeypatch.delenv("UNRELATED_KRL_TEST_VARIABLE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert collector.load_credentials() == (
        "file-account", "literal ${NOT_EXPANDED} #@password",
    )
    assert "KUKACENTER_PASSWORD" not in collector.os.environ
    assert "UNRELATED_KRL_TEST_VARIABLE" not in collector.os.environ


def test_credential_overrides_and_missing_dotenv_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    local_env = tmp_path / "utils/krl/.env"
    local_env.parent.mkdir(parents=True)
    monkeypatch.setattr(collector, "ENV_FILE", local_env)
    (tmp_path / ".env").write_text(
        "KUKACENTER_ACCOUT=root-account\nKUKACENTER_PASSWORD=root-password\n", encoding="utf-8",
    )
    monkeypatch.delenv("KUKACENTER_ACCOUT", raising=False)
    monkeypatch.delenv("KUKACENTER_PASSWORD", raising=False)
    assert collector.load_credentials() == ("13918961550", None)
    local_env.write_text(
        "KUKACENTER_ACCOUT=file-account\nKUKACENTER_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KUKACENTER_ACCOUT", "environment-account")
    monkeypatch.setenv("KUKACENTER_PASSWORD", "environment-password")
    assert collector.load_credentials() == ("environment-account", "environment-password")
    assert collector.load_credentials("cli-account") == ("cli-account", "environment-password")


def test_cli_passes_dotenv_credentials_to_login_without_logging_secrets(tmp_path, monkeypatch, caplog, capsys):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    local_env = tmp_path / "utils/krl/.env"
    local_env.parent.mkdir(parents=True)
    monkeypatch.setattr(collector, "ENV_FILE", local_env)
    monkeypatch.delenv("KUKACENTER_ACCOUT", raising=False)
    monkeypatch.delenv("KUKACENTER_PASSWORD", raising=False)
    local_env.write_text(
        "KUKACENTER_ACCOUT=file-account\nKUKACENTER_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n", encoding="utf-8")
    session = Session([])
    monkeypatch.setattr(requests, "Session", lambda: session)
    logins = []
    monkeypatch.setattr(collector.KukaClient, "login",
                        lambda client, username, password, **kwargs: logins.append((username, password)))
    monkeypatch.setattr(collector.KukaClient, "ask", lambda *args: (ANSWER, {"mode": "KRL编程"}))
    assert collector.main(["--questions", str(questions), "--output", str(tmp_path / "qa.md")]) == 0
    assert logins == [("file-account", "file-password")]
    assert "file-password" not in caplog.text + capsys.readouterr().out


def test_default_credentials_file_is_beside_collector():
    assert collector.ENV_FILE == Path(collector.__file__).resolve().with_name(".env")
    assert collector.ENV_FILE == collector.ROOT / "utils/krl/.env"


@pytest.fixture
def concurrent_cli(tmp_path, monkeypatch):
    questions = tmp_path / "questions.md"
    questions.write_text("\n".join(
        f"## {number:04d} 工艺{number}\n\n问题{number}\n"
        for number in range(19, 25)
    ), encoding="utf-8")
    state = SimpleNamespace(
        questions=collector.parse_questions(questions), output=tmp_path / "qa.md",
        sessions=[], logins=[], saved=[], draining=threading.Event(),
        saved_events={number: threading.Event() for number in range(19, 25)},
    )
    state.arguments = [
        "--questions", str(questions), "--output", str(state.output),
        "--concurrency", "2", "--delay", "0",
    ]

    class TrackedSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.closed = False
            state.sessions.append(self)

        def request(self, *args, **kwargs):
            raise AssertionError("Unexpected network request in offline test")

        def close(self):
            self.closed = True
            super().close()

    def login(client, username, password, **kwargs):
        state.logins.append(client.session)
        client.session.cookies.set(
            "auth", "main-cookie", domain="www.kukacenter.com", path="/chat",
        )
        client.session.cookies.set_cookie(requests.cookies.create_cookie(
            "auth", "chat-cookie", domain="chat.kukacenter.com", path="/api",
            secure=True, rest={"HttpOnly": True},
        ))

    owner = threading.get_ident()
    state.original_save = collector.CollectionStore.save

    def save(store, question, answer, metadata=None):
        assert threading.get_ident() == owner, "Only the coordinator may write"
        path = state.original_save(store, question, answer, metadata)
        state.saved.append(question.id)
        state.saved_events[question.id].set()
        return path

    original_error = collector.LOG.error

    def error(message, *args, **kwargs):
        original_error(message, *args, **kwargs)
        if "draining" in message:
            state.draining.set()

    monkeypatch.setattr(requests, "Session", TrackedSession)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", login)
    monkeypatch.setattr(collector.CollectionStore, "save", save)
    monkeypatch.setattr(collector.LOG, "error", error)
    return state


@pytest.mark.parametrize("value", ["0", "-1", "1.5"])
def test_cli_rejects_invalid_concurrency_before_network(value):
    with pytest.raises(SystemExit) as error:
        collector.main(["--concurrency", value, "--dry-run"])
    assert error.value.code == 2


def test_dry_run_reports_concurrency_without_network_or_writes(concurrent_cli, capsys):
    state = concurrent_cli
    assert collector.main([*state.arguments, "--dry-run"]) == 0
    assert "Concurrency: 2" in capsys.readouterr().out
    assert not state.sessions
    assert not state.output.exists()
    assert not state.output.with_name(".qa.lock").exists()


def test_concurrent_calls_overlap_with_isolated_authenticated_sessions(concurrent_cli, monkeypatch):
    state = concurrent_cli
    barrier = threading.Barrier(2)
    mutex = threading.Lock()
    active = set()
    peak = 0

    def ask(client, question):
        nonlocal peak
        session = client.session
        assert not session.closed
        assert session is not state.logins[0]
        assert session.headers == state.logins[0].headers
        assert session.cookies.get("auth", domain="www.kukacenter.com", path="/chat") == "main-cookie"
        assert session.cookies.get("auth", domain="chat.kukacenter.com", path="/api") == "chat-cookie"
        chat_cookie = next(cookie for cookie in session.cookies if cookie.domain == "chat.kukacenter.com")
        assert chat_cookie.secure and chat_cookie.has_nonstandard_attr("HttpOnly")
        assert client.request_timeout == 17 and client.answer_timeout == 31
        assert client.retries == 2 and client.retry_delay == 0
        with mutex:
            assert session not in active, "A worker session was shared concurrently"
            active.add(session)
            peak = max(peak, len(active))
        try:
            barrier.wait(timeout=5)
            return ANSWER, {"mode": "KRL编程"}
        finally:
            with mutex:
                active.remove(session)

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    assert collector.main([
        *state.arguments, "--request-timeout", "17", "--answer-timeout", "31",
        "--retries", "2", "--retry-delay", "0",
    ]) == 0
    assert peak == 2 and len(state.logins) == 1 and len(state.sessions) == 3
    assert set(state.saved) == set(range(19, 25))
    assert all(session.closed for session in state.sessions)
    assert len({id(session.cookies) for session in state.sessions}) == 3
    for adapter in ("https://", "http://"):
        assert len({id(session.adapters[adapter]) for session in state.sessions}) == 3
    state.sessions[1].cookies.set("local-change", "worker-one")
    assert all("local-change" not in session.cookies for session in (state.sessions[0], state.sessions[2]))


def test_concurrent_completion_order_batches_and_resume(concurrent_cli, monkeypatch):
    state = concurrent_cli
    calls = []

    def ask(client, question):
        calls.append(question.id)
        if question.id == 19:
            assert state.saved_events[22].wait(5)
        return ANSWER, {"mode": "KRL编程"}

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    arguments = [*state.arguments, "--max-questions", "4", "--batch-size", "2"]
    assert collector.main(arguments) == 0
    assert sorted(calls) == [19, 20, 21, 22]
    assert state.saved == [20, 21, 22, 19]
    first = state.output.read_bytes()
    second_path = state.output.with_name("qa_002.md")
    second = second_path.read_bytes()
    assert first.index("样本0020".encode()) < first.index("样本0021".encode())
    assert second.index("样本0022".encode()) < second.index("样本0019".encode())
    store = collector.CollectionStore(state.output, state.questions, batch_size=2)
    assert store.completed_ids == {19, 20, 21, 22}
    calls.clear()
    assert collector.main(arguments) == 0
    assert sorted(calls) == [23, 24]
    assert state.output.read_bytes() == first and second_path.read_bytes() == second
    assert collector.CollectionStore(state.output, state.questions, batch_size=2).completed_ids == set(range(19, 25))


def test_global_delay_spaces_concurrent_starts_without_waiting_for_answers(concurrent_cli, monkeypatch):
    state = concurrent_cli
    starts = []

    def ask(client, question):
        starts.append(time.monotonic())
        if question.id == 19:
            assert state.saved_events[20].wait(5)
        return ANSWER, {}

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    assert collector.main([
        *state.arguments, "--max-questions", "3", "--delay", "0.04",
    ]) == 0
    assert len(starts) == 3
    assert all(later - earlier >= 0.035 for earlier, later in zip(starts, starts[1:]))


@pytest.mark.parametrize("delay", ["0", "10"])
def test_terminal_failure_stops_dispatch_drains_success_and_resumes(concurrent_cli, monkeypatch, delay):
    state = concurrent_cli
    second_started = threading.Event()
    calls = []

    def ask(client, question):
        calls.append(question.id)
        if question.id == 19:
            # With delay=10, fail before the second question is dispatched.
            if delay == "0":
                assert second_started.wait(5)
            raise collector.CollectionError("terminal failure")
        second_started.set()
        assert state.draining.wait(5)
        assert not client.session.closed
        return ANSWER, {}

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    assert collector.main([*state.arguments, "--delay", delay]) == 1
    assert sorted(calls) == ([19, 20] if delay == "0" else [19])
    completed = collector.CollectionStore(state.output, state.questions).completed_ids
    assert completed == ({20} if delay == "0" else set())
    assert all(session.closed for session in state.sessions)
    calls.clear()

    def resumed_ask(client, question):
        calls.append(question.id)
        return ANSWER, {}

    monkeypatch.setattr(collector.KukaClient, "ask", resumed_ask)
    assert collector.main(state.arguments) == 0
    assert set(calls) == set(range(19, 25)) - completed


def test_write_failure_preserves_output_stops_publication_and_drains(concurrent_cli, monkeypatch):
    state = concurrent_cli
    state.original_save(collector.CollectionStore(state.output, state.questions), state.questions[0], ANSWER)
    before = state.output.read_bytes()
    second_started = threading.Event()
    calls = []
    save_attempts = []

    def ask(client, question):
        calls.append(question.id)
        if question.id == 20:
            assert second_started.wait(5)
        else:
            second_started.set()
            assert state.draining.wait(5)
            assert not client.session.closed
        return ANSWER, {}

    def fail_save(store, question, answer, metadata=None):
        save_attempts.append(question.id)
        raise OSError("disk full")

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    monkeypatch.setattr(collector.CollectionStore, "save", fail_save)
    assert collector.main(state.arguments) == 1
    assert sorted(calls) == [20, 21] and save_attempts == [20]
    assert state.output.read_bytes() == before
    assert all(session.closed for session in state.sessions)


def test_concurrent_stage_retries_preserve_generation_and_verification_boundaries(concurrent_cli, monkeypatch):
    state = concurrent_cli
    attempts = {}
    mutex = threading.Lock()

    def attempt(client, stage):
        with mutex:
            counts = attempts.setdefault(client, {"generation": 0, "check": 0, "fetch": 0})
            counts[stage] += 1
            if counts[stage] == 1:
                raise collector.RetryableCollectionError("transient failure")

    def generate(client, question):
        attempt(client, "generation")
        return ANSWER, {"sessionId": f"session-{question.id}", "requestId": f"session-{question.id}", "uuid": f"uuid-{question.id}"}

    def api_call(client, path, payload):
        stage = "check" if path == "/api/check" else "fetch"
        attempt(client, stage)
        return {"status": True} if stage == "check" else {"answerContent": ANSWER}

    # Force both worker clients to participate even though fake stages are fast.
    barrier = threading.Barrier(2)
    original_ask = collector.KukaClient.ask

    def ask(client, question):
        barrier.wait(timeout=5)
        return original_ask(client, question)

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    monkeypatch.setattr(collector.KukaClient, "generate", generate)
    monkeypatch.setattr(collector.KukaClient, "api", api_call)
    assert collector.main([
        *state.arguments, "--max-questions", "2", "--retries", "1", "--retry-delay", "0",
    ]) == 0
    assert len(attempts) == 2
    assert all(counts == {"generation": 2, "check": 2, "fetch": 2} for counts in attempts.values())
    saved = state.output.read_text(encoding="utf-8")
    assert '"session_id": "session-19"' in saved and '"session_id": "session-20"' in saved


def test_coordinator_cancellation_drains_workers_before_raising_interrupt(concurrent_cli, monkeypatch):
    state = concurrent_cli
    both_started = threading.Barrier(2)
    release = threading.Event()
    calls = []

    async def run():
        loop = asyncio.get_running_loop()
        coordinator = asyncio.current_task()

        def ask(client, question):
            calls.append(question.id)
            both_started.wait(timeout=5)
            if question.id == 19:
                loop.call_soon_threadsafe(coordinator.cancel)
                loop.call_soon_threadsafe(release.set)
            assert release.wait(5)
            return ANSWER, {}

        monkeypatch.setattr(collector.KukaClient, "ask", ask)
        clients = [collector.KukaClient(object()), collector.KukaClient(object())]
        store = collector.CollectionStore(state.output, state.questions)
        await collector._collect_concurrently(clients, state.questions, store, 0)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run())
    assert sorted(calls) == [19, 20] and set(state.saved) == {19, 20}


@pytest.mark.skipif(sys.platform == "win32", reason="Uses POSIX SIGINT delivery")
def test_real_sigint_drains_saves_and_closes_sessions_before_releasing_output_lock(tmp_path):
    script = textwrap.dedent('''
        import os
        import signal
        import sys
        import threading
        from pathlib import Path
        import requests
        from utils.krl import collect_krl_qa as collector

        root = Path(sys.argv[1])
        questions = root / "questions.md"
        questions.write_text("".join(f"## {i:04d} Q{i}\\n\\nprompt{i}\\n\\n" for i in range(19, 23)))
        output = root / "qa.md"
        sessions = []
        barrier = threading.Barrier(3)
        release = threading.Event()
        calls = []

        class Session(requests.Session):
            def __init__(self):
                super().__init__()
                self.closed = False
                sessions.append(self)

            def close(self):
                self.closed = True
                super().close()

        def ask(client, question):
            calls.append(question.id)
            barrier.wait(timeout=5)
            assert release.wait(5)
            assert not client.session.closed
            return "complete answer", {}

        def interrupt():
            barrier.wait(timeout=5)
            os.kill(os.getpid(), signal.SIGINT)
            release.set()

        requests.Session = Session
        collector.load_credentials = lambda username=None: ("user", "password")
        collector.KukaClient.login = lambda *args, **kwargs: None
        collector.KukaClient.ask = ask
        original_save = collector.CollectionStore.save

        def save(store, *args, **kwargs):
            try:
                with collector.output_lock(output):
                    raise AssertionError("Output lock released before save")
            except collector.CollectionError:
                pass
            return original_save(store, *args, **kwargs)

        collector.CollectionStore.save = save
        sender = threading.Thread(target=interrupt)
        sender.start()
        status = collector.main(["--questions", str(questions), "--output", str(output), "--concurrency", "2", "--delay", "0"])
        sender.join(timeout=5)
        assert status == 130, status
        assert sorted(calls) == [19, 20], calls
        assert all(session.closed for session in sessions)
        assert collector.CollectionStore(output, collector.parse_questions(questions)).completed_ids == {19, 20}
        with collector.output_lock(output):
            pass
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=collector.ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
