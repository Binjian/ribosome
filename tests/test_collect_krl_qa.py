"""Offline KUKA frontend protocol tests; no live login or model requests."""

import builtins
import hashlib
from collections import deque
from pathlib import Path

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
