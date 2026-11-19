"""Offline KUKA frontend protocol tests; no live login or model requests."""

import asyncio
import builtins
import hashlib
import json
import os
import ssl
import subprocess
import sys
import textwrap
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import MaxRetryError, ReadTimeoutError
from urllib3.exceptions import SSLError as Urllib3SSLError
from urllib3.response import HTTPResponse as Urllib3Response
from urllib3.util.ssl_match_hostname import CertificateError
from utils.krl import collect_krl_qa as collector
from utils.krl.krl_collection_store import _saved_records


@pytest.fixture(autouse=True)
def isolate_concurrency_environment(monkeypatch):
    # Keep protocol tests independent of the developer's local collection size.
    monkeypatch.setenv("COLLECT_CONCURRENCY", "1")


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


def wrapped_tls_error(cause):
    # MaxRetryError keeps the underlying exception in .reason, outside .args.
    return requests.exceptions.SSLError(MaxRetryError(
        None, "/api/session/saveOrUpdate?signature=private-token", Urllib3SSLError(cause),
    ))


def openssl_error(reason):
    error = ssl.SSLError(ssl.SSL_ERROR_SSL, "private-token")
    error.reason = reason
    return error


def test_utf8_stream_preserves_full_markdown_across_arbitrary_byte_boundaries():
    encoded = ANSWER.encode("utf-8")
    response = Response(chunks=[bytes([byte]) for byte in encoded])
    assert collector.read_stream(response, 10) == ANSWER


@pytest.mark.parametrize("complete", [True, False], ids=["complete", "truncated"])
def test_chunked_answer_requires_the_terminal_http_chunk(complete):
    answer = "### 参数\n\n私有回答\n"
    body = answer.encode("utf-8")
    wire = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/plain\r\n\r\n"
        + f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n"
    )
    if complete:
        wire += b"0\r\n\r\n"
    # Feed the real requests parser in memory without a network connection.
    source = HTTPResponse(SimpleNamespace(makefile=lambda *_: BytesIO(wire)), method="POST")
    source.begin()
    response = requests.Response()
    response.status_code = source.status
    response.headers.update(source.getheaders())
    response.raw = Urllib3Response(
        body=source, headers=source.getheaders(), preload_content=False, original_response=source,
    )
    session = answer_session(answer=answer)
    session.responses[1] = response
    client = collector.KukaClient(session, retries=0)

    if complete:
        assert client.ask(QUESTION)[0] == answer
        assert not session.responses
    else:
        with pytest.raises(collector.StreamRetryExhaustedError) as error:
            client.ask(QUESTION)
        assert isinstance(error.value.__cause__, collector.ChunkedAnswerStreamError)
        message = str(error.value)
        assert "ChunkedEncodingError" in message
        assert f"{len(body)} bytes" in message
        assert "私有回答" not in message
        assert len(session.calls) == 2


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


@pytest.mark.parametrize("error, diagnostic", [
    (requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING private-token"), "TLS error"),
    (wrapped_tls_error(openssl_error("WRONG_VERSION_NUMBER")), "TLS error"),
    (wrapped_tls_error(ssl.SSLCertVerificationError(ssl.SSL_ERROR_SSL, "private-token")),
     "TLS certificate verification failed"),
    (wrapped_tls_error(CertificateError("private-token")), "TLS certificate verification failed"),
])
def test_permanent_and_unknown_tls_failures_stop_generation(error, diagnostic, caplog):
    session = Session([error])
    with pytest.raises(collector.CollectionError) as raised:
        collector.KukaClient(session, retries=3, retry_delay=0).ask(QUESTION)
    assert not isinstance(raised.value, collector.RetryableCollectionError)
    assert f"{diagnostic} (SSLError)" in str(raised.value)
    assert "private-token" not in str(raised.value) + caplog.text
    assert len(session.calls) == 1


@pytest.mark.parametrize("cause", [
    openssl_error("UNEXPECTED_EOF_WHILE_READING"),
    ssl.SSLError(ssl.SSL_ERROR_EOF, "private-token"),
    ssl.SSLZeroReturnError(ssl.SSL_ERROR_ZERO_RETURN, "private-token"),
    ConnectionResetError("private-token"),
    ConnectionAbortedError("private-token"),
    BrokenPipeError("private-token"),
])
def test_nested_tls_disconnects_are_retryable(cause):
    failure = collector.http_failure(wrapped_tls_error(cause), "TLS request")
    assert isinstance(failure, collector.RetryableCollectionError)
    assert "TLS connection closed or reset (SSLError)" in str(failure)
    assert "private-token" not in str(failure)


def test_cli_retries_tls_session_creation_then_saves_verified_answer(tmp_path, monkeypatch, caplog):
    source = tmp_path / "questions.md"
    source.write_text("## 0188 工艺步骤\n\n完整问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    incoming = ssl.MemoryBIO()
    connection = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).wrap_bio(
        incoming, ssl.MemoryBIO(), server_hostname="test.example",
    )
    incoming.write_eof()
    with pytest.raises(ssl.SSLEOFError) as disconnected:
        connection.do_handshake()
    session = answer_session(session_id="retried-conversation")
    session.responses.appendleft(wrapped_tls_error(disconnected.value))
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)

    assert collector.main([
        "--questions", str(source), "--output", str(output),
        "--start", "188", "--retries", "1", "--retry-delay", "0",
    ]) == 0
    saved, = _saved_records(output)
    assert saved.question.id == 188 and saved.answer == ANSWER
    assert saved.metadata["answer_check_passed"] is True
    assert saved.metadata["session_id"] == "retried-conversation"
    assert session.calls[0] == session.calls[1]
    assert session.calls[0][1].endswith("/api/session/saveOrUpdate")
    assert len(session.calls) == 5 and not session.responses
    assert all(options.get("verify", True) is True for _, _, options in session.calls)
    assert "TLS connection closed or reset (SSLError)" in caplog.text
    assert "private-token" not in caplog.text


def test_api_rejection_does_not_retry_generation():
    session = Session([api(None, code="error")])
    with pytest.raises(collector.CollectionError, match="API rejected"):
        collector.KukaClient(session, retries=3, retry_delay=0).ask(QUESTION)
    assert len(session.calls) == 1


def test_create_rejection_reuses_latest_empty_conversation_only_once():
    history = {
        "TODAY": [], "SEVEN_DAYS": [{"sessionId": "empty-217"}],
        "THIRTY_DAYS": [{"sessionId": "older-conversation"}],
    }
    rejection = api(None, code=1)
    session = Session([
        rejection, api(history), api({"questionInfos": []}),
        Response(chunks=[ANSWER.encode("utf-8")]), api({"status": True}),
        api({"answerContent": ANSWER}), api(None, code=1), api(history),
    ])
    client = collector.KukaClient(session, retries=0)

    answer, metadata = client.ask(collector.Question(217, "喷涂", "喷涂问题"))
    assert answer == ANSWER
    assert metadata["session_id"] == "empty-217"
    create, history_call, details, stream, _, saved = session.calls
    assert create[1].endswith("/api/session/saveOrUpdate") and rejection.closed
    assert history_call[1].endswith("/api/session/select")
    assert details[1].endswith("/api/question/select")
    assert details[2]["json"] == {"sessionId": "empty-217"}
    assert stream[2]["json"]["requestId"] == saved[2]["json"]["sessionId"] == "empty-217"

    with pytest.raises(collector.CollectionError, match=r"saveOrUpdate.*code=1"):
        client.ask(collector.Question(218, "喷涂", "另一个喷涂问题"))
    assert len(session.calls) == 8
    assert not session.responses


@pytest.mark.parametrize("question_infos", [None, [{"question": "Earlier browser question"}]])
def test_create_rejection_requires_explicitly_empty_conversation(question_infos):
    session = Session([
        api(None, code="1"),
        api({"TODAY": [{"sessionId": "newest"}, {"sessionId": "older"}]}),
        api({"questionInfos": question_infos}),
    ])
    with pytest.raises(collector.CollectionError, match=r"saveOrUpdate.*code=1"):
        collector.KukaClient(session, retries=0).ask(QUESTION)
    assert session.calls[-1][1].endswith("/api/question/select")
    assert session.calls[-1][2]["json"] == {"sessionId": "newest"}
    assert not session.responses


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
        collector.KukaClient(session, retries=0).ask(QUESTION)
    assert len(session.calls) == 4


def test_saved_answer_comparison_keeps_significant_first_line_indentation():
    session = answer_session(answer="    $OUT[10]=TRUE\n", saved="$OUT[10]=TRUE\n")
    with pytest.raises(collector.CollectionError, match="differ"):
        collector.KukaClient(session, retries=0).ask(QUESTION)
    matching = answer_session(answer="    $OUT[10]=TRUE  \n")
    answer, _ = collector.KukaClient(matching).ask(QUESTION)
    assert answer == "    $OUT[10]=TRUE  \n"


@pytest.mark.parametrize("stale", [
    None, {}, {"answerContent": None}, {"answerContent": ""},
    {"answerContent": " \n\t"}, {"answerContent": ANSWER[:20]},
])
def test_saved_answer_not_ready_retries_only_fetch_until_matching(stale):
    session = answer_session()
    session.responses.insert(3, api(stale))
    answer, metadata = collector.KukaClient(session, retries=1, retry_delay=0).ask(QUESTION)
    assert answer == ANSWER
    assert metadata["answer_id"] == "answer-19"
    assert metadata["server_time"] == "server-time"
    assert len(session.calls) == 5
    assert sum(call[1].endswith("/api/session/saveOrUpdate") for call in session.calls) == 1
    assert sum(call[1].endswith("/api/efs/stream") for call in session.calls) == 1
    assert sum(call[1].endswith("/api/check") for call in session.calls) == 1
    assert session.calls[3] == session.calls[4]
    identifiers = session.calls[1][2]["json"]
    assert session.calls[3][2]["json"] == {
        "question": QUESTION.prompt, "content": QUESTION.prompt,
        **{key: identifiers[key] for key in ("sessionId", "requestId", "uuid")},
    }
    assert not session.responses


@pytest.mark.parametrize("saved", [[], "unexpected response", 3, {"answerContent": []},
                                  {"answerContent": 3}, {"answerContent": False}])
def test_malformed_saved_answer_remains_terminal(saved):
    session = answer_session()
    session.responses[-1] = api(saved)
    with pytest.raises(collector.CollectionError):
        collector.KukaClient(session, retries=3, retry_delay=0).ask(QUESTION)
    assert len(session.calls) == 4
    assert not session.responses


def test_saved_answer_mismatch_exhausts_default_fetch_budget_with_safe_diagnostics(caplog):
    streamed = "stream-secret-contents"
    saved = "saved-secret-contents"
    session = answer_session(answer=streamed, saved=saved)
    session.responses.extend(api({"answerContent": saved}) for _ in range(3))
    with pytest.raises(collector.UnverifiedAnswerError, match="Stream and saved answer differ") as error:
        collector.KukaClient(session, retry_delay=0).ask(QUESTION)
    assert len(session.calls) == 7
    assert all(call == session.calls[3] for call in session.calls[3:])
    message = str(error.value)
    assert "4 attempt(s)" in message
    assert f"stream_chars={len(streamed)}" in message
    assert f"saved_chars={len(saved)}" in message
    assert "first_difference=1" in message
    assert streamed not in message + caplog.text
    assert saved not in message + caplog.text
    assert not session.responses


def test_saved_answer_http_and_mismatch_share_one_fetch_retry_budget():
    session = answer_session(saved=ANSWER[:20])
    session.responses.insert(3, Response(status=503))
    session.responses.append(api({"answerContent": ANSWER}))
    with pytest.raises(collector.CollectionError, match="2 attempt\\(s\\)"):
        collector.KukaClient(session, retries=1, retry_delay=0).ask(QUESTION)
    assert len(session.calls) == 5
    assert session.calls[3] == session.calls[4]
    # A matching response must not receive an extra retry budget after HTTP recovery.
    assert len(session.responses) == 1


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_saved_answer_comparison_accepts_line_endings_and_returns_original_saved_text(newline):
    streamed = "\n    first line  \n\nsecond line\n"
    saved = "\n\n" + streamed.replace("\n", newline) + "\n"
    session = answer_session(answer=streamed, saved=saved)
    answer, _ = collector.KukaClient(session, retries=0).ask(QUESTION)
    assert answer == saved
    assert len(session.calls) == 4


@pytest.mark.parametrize("saved", ["first line  \nsecond line\n",
                                  "    first line\nsecond line\n"])
def test_saved_answer_comparison_rejects_significant_spaces_after_newline_normalization(saved):
    session = answer_session(answer="    first line  \r\nsecond line\r\n", saved=saved)
    with pytest.raises(collector.CollectionError, match="differ"):
        collector.KukaClient(session, retries=0).ask(QUESTION)
    assert len(session.calls) == 4


def test_moderation_refusal_does_not_export_rejected_raw_answer():
    session = answer_session(answer="rejected raw answer", check=False)
    answer, metadata = collector.KukaClient(session).ask(QUESTION)
    assert answer == collector.REFUSAL
    assert "rejected raw answer" not in answer
    assert metadata["answer_check_passed"] is False
    assert len(session.calls) == 3


@pytest.mark.parametrize("code", [401, "-42", "error", None, "private-token", {"secret": "private-token"}])
def test_json_error_codes_are_not_answers_or_logged_bodies(code):
    session = Session([Response({"code": code, "msg": "private-token"})])
    with pytest.raises(collector.CollectionError, match="API rejected") as error:
        collector.KukaClient(session).api("/api/session/saveOrUpdate", {})
    assert "private-token" not in str(error.value)
    expected_code = str(code) if code in (401, "-42") else "unavailable"
    assert f"code={expected_code}" in str(error.value)


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


@pytest.mark.parametrize("retry_gets", [False, True], ids=["first-attempt", "retry-login-gets"])
def test_login_posts_md5_captcha_and_signed_chat_credentials(retry_gets, monkeypatch, capsys, caplog):
    signature = {"uuid": "account-uuid", "timestamp": 123, "nonceStr": "nonce", "sign": "signature"}
    responses = [
        Response(url=LOGIN_URL, text=FORM),
        Response(content_type="image/png", content=b"captcha-image"),
        Response(), api(signature), api({}),
    ]
    if retry_gets:
        for stage in (3, 1, 0):
            responses.insert(stage, wrapped_tls_error(ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "private-token")))
    session = Session(responses)
    answers = iter(["yes", "abcd"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))
    monkeypatch.setattr(collector.getpass, "getpass", lambda prompt: "test-secret")
    collector.KukaClient(session, retries=1, retry_delay=0).login("test-user")
    if retry_gets:
        first_page, page, first_captcha, captcha, login_post, first_sign, sign, chat_login = session.calls
        assert first_page == page
        assert first_captcha[:2] == captcha[:2]
        assert first_captcha[2]["params"]["uuid"] != captcha[2]["params"]["uuid"]
        assert first_sign[:2] == sign[:2]
        assert first_sign[2]["params"]["nonceStr"] != sign[2]["params"]["nonceStr"]
        assert caplog.text.count("TLS connection closed or reset (SSLError)") == 3
        assert "private-token" not in caplog.text
    else:
        page, captcha, login_post, sign, chat_login = session.calls
    assert page[:2] == ("GET", collector.MAIN_URL + "/chat")
    fields = login_post[2]["data"]
    assert login_post[:2] == ("POST", LOGIN_URL)
    assert fields["username"] == "test-user"
    assert fields["password"] == hashlib.md5(b"test-secret").hexdigest()
    assert fields["captcha"] == "abcd" and fields["execution"] == "flow&token"
    assert fields["uuid"] == captcha[2]["params"]["uuid"]
    assert fields["loginType"] == "0"
    assert sign[:2] == ("GET", collector.MAIN_URL + "/api/api-b/program_assistant/api/sign")
    assert chat_login[:2] == ("POST", collector.CHAT_URL + "/api/login")
    assert chat_login[2]["json"] == {
        "uuid": "account-uuid", "timestamp": "123", "nonceStr": "nonce", "signature": "signature",
    }
    assert chat_login[2]["headers"] == {
        "Origin": collector.CHAT_URL, "Referer": collector.CHAT_URL + "/",
    }
    assert not session.responses
    output = capsys.readouterr().out
    assert "test-secret" not in output
    captcha_line = next(line for line in output.splitlines() if "CAPTCHA:" in line)
    assert not Path(captcha_line.split(": ", 1)[1]).exists()


@pytest.mark.parametrize("retries, cause, attempts, diagnostic", [
    (0, ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "private-token"), 1, "1 attempt"),
    (1, ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "private-token"), 2, "2 attempt"),
    (3, ssl.SSLCertVerificationError(ssl.SSL_ERROR_SSL, "private-token"), 1,
     "TLS certificate verification failed"),
])
def test_login_page_retries_are_bounded_and_keep_certificate_errors_terminal(
    retries, cause, attempts, diagnostic, caplog,
):
    session = Session([wrapped_tls_error(cause) for _ in range(attempts)])
    with pytest.raises(collector.CollectionError, match=diagnostic) as error:
        collector.KukaClient(session, retries=retries, retry_delay=0).login("test-user", "test-secret")
    assert [call[:2] for call in session.calls] == [("GET", collector.MAIN_URL + "/chat")] * attempts
    assert "private-token" not in str(error.value) + caplog.text


@pytest.mark.parametrize("stage", ["cas", "chat"])
def test_login_post_is_never_replayed_after_a_tls_disconnect(stage, monkeypatch, caplog):
    signature = {"uuid": "account-uuid", "timestamp": 123, "nonceStr": "nonce", "sign": "signature"}
    responses = [Response(url=LOGIN_URL, text=FORM), Response(content_type="image/png", content=b"captcha-image")]
    if stage == "chat":
        responses.extend([Response(), api(signature)])
    responses.append(wrapped_tls_error(ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "private-token")))
    session = Session(responses)
    monkeypatch.setattr(builtins, "input", lambda prompt: "abcd")
    with pytest.raises(collector.CollectionError, match="TLS connection closed or reset") as error:
        collector.KukaClient(session, retries=3, retry_delay=0).login("test-user", "test-secret", accept_terms=True)
    expected = [LOGIN_URL] if stage == "cas" else [LOGIN_URL, collector.CHAT_URL + "/api/login"]
    assert [url for method, url, _ in session.calls if method == "POST"] == expected
    assert "private-token" not in str(error.value) + caplog.text


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


def test_cli_saved_answer_mismatch_preserves_output_and_resumes_missing_question(tmp_path, monkeypatch):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题\n\n## 0020 夹取\n\n第二问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    parsed = collector.parse_questions(questions)
    collector.CollectionStore(output, parsed).save(parsed[0], ANSWER)
    previous = output.read_bytes()
    failed = answer_session(saved="incomplete saved answer", session_id="failed-session")
    failed.responses.append(api({"answerContent": "still incomplete"}))
    repeated = answer_session(saved="still incomplete", session_id="second-failed-session")
    repeated.responses.append(api({"answerContent": "still incomplete"}))
    failed.responses.extend(repeated.responses)
    monkeypatch.setattr(requests, "Session", lambda: failed)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)
    arguments = ["--questions", str(questions), "--output", str(output),
                 "--delay", "0", "--retry-delay", "0", "--retries", "1"]
    assert collector.main(arguments) == 1
    assert output.read_bytes() == previous
    assert collector.CollectionStore(output, parsed).completed_ids == {19}
    assert len(failed.calls) == 10
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


@pytest.mark.parametrize("outcome", ["success", "persistent", "disabled"])
def test_sequential_cli_restarts_unverified_generation_once_and_preserves_records(
    tmp_path, monkeypatch, outcome,
):
    source = tmp_path / "questions.md"
    source.write_text(
        "## 0019 已完成\n\n原有问题\n\n## 0020 待完成\n\n新问题\n\n"
        "## 0021 后续问题\n\n后续问题正文\n", encoding="utf-8",
    )
    output = tmp_path / "qa.md"
    questions = collector.parse_questions(source)
    collector.CollectionStore(output, questions).save(
        questions[0], ANSWER, {"session_id": "preserved-session"},
    )
    original, = _saved_records(output)
    before = output.read_bytes()
    streamed, saved = "Stream-notice-" + "x" * 21, "saved-notice-" + "y" * 15
    assert (len(streamed), len(saved)) == (35, 28)
    fetch_attempts = 1 if outcome == "disabled" else 4

    def failed_generation(session_id):
        result = answer_session(answer=streamed, saved=saved, session_id=session_id)
        result.responses.extend(api({"answerContent": saved}) for _ in range(fetch_attempts - 1))
        return list(result.responses)

    responses = failed_generation("first-unverified-session")
    if outcome == "success":
        responses.extend(answer_session(session_id="verified-session").responses)
        responses.extend(answer_session(session_id="next-question-session").responses)
    elif outcome == "persistent":
        responses.extend(failed_generation("second-unverified-session"))
    session = Session(responses)
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)
    arguments = [
        "--questions", str(source), "--output", str(output), "--concurrency", "1",
        "--delay", "0", "--retry-delay", "0",
    ]
    if outcome == "disabled":
        arguments.extend(["--retries", "0"])
    assert collector.main(arguments) == (0 if outcome == "success" else 1)
    streams = [call[2]["json"] for call in session.calls if call[1].endswith("/api/efs/stream")]
    assert len(streams) == {"success": 3, "persistent": 2, "disabled": 1}[outcome]
    assert all(payload["question"] != questions[0].prompt for payload in streams)
    if outcome != "disabled":
        assert streams[0]["sessionId"] != streams[1]["sessionId"]
        assert streams[0]["uuid"] != streams[1]["uuid"]
        assert streams[0]["question"] == streams[1]["question"] == questions[1].prompt
    records = _saved_records(output)
    preserved = records[0]
    assert (preserved.question, preserved.answer, preserved.metadata) == (
        original.question, original.answer, original.metadata,
    )
    if outcome == "success":
        assert [record.question.id for record in records] == [19, 20, 21]
        assert all(record.answer == ANSWER for record in records)
    else:
        assert output.read_bytes() == before
        assert collector.CollectionStore(output, questions).completed_ids == {19}
        assert all(payload["question"] == questions[1].prompt for payload in streams)
    assert streamed not in output.read_text(encoding="utf-8")
    assert saved not in output.read_text(encoding="utf-8")
    assert not session.responses


def test_mismatch_diagnostic_captures_first_pair_privately_without_request_data(
    tmp_path, caplog,
):
    directory = tmp_path / "private-diagnostics"
    streamed, saved = "stream-secret-body", "saved-secret-body"
    session = answer_session(answer=streamed, saved=saved)
    session.headers = {"Authorization": "private-auth", "Cookie": "private-cookie"}
    session.responses.extend(api({"answerContent": saved}) for _ in range(3))
    client = collector.KukaClient(session, retry_delay=0, diagnostics_dir=directory)
    with pytest.raises(collector.UnverifiedAnswerError):
        client.ask(QUESTION)
    path, = directory.glob("*.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["question_id"] == 19
    assert payload["streamed_answer"] == streamed
    assert payload["saved_answer"] == saved
    assert payload["stream_chars"] == len(streamed)
    assert payload["saved_chars"] == len(saved)
    assert not {"question", "prompt", "sessionId", "requestId", "uuid", "headers", "cookies"} & payload.keys()
    assert all(secret not in path.read_text(encoding="utf-8") for secret in (
        QUESTION.prompt, "private-auth", "private-cookie",
    ))
    assert streamed not in caplog.text and saved not in caplog.text
    assert not list(directory.glob("*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert directory.stat().st_mode & 0o777 == 0o700


def test_mismatch_diagnostic_failure_preserves_original_error_and_hides_exception(
    tmp_path, monkeypatch, caplog,
):
    directory = tmp_path / "diagnostics-is-a-file"
    directory.write_text("do not replace", encoding="utf-8")
    session = answer_session(saved="different saved answer")
    with pytest.raises(collector.UnverifiedAnswerError, match="Stream and saved answer differ"):
        collector.KukaClient(session, retries=0, diagnostics_dir=directory).ask(QUESTION)
    assert directory.read_text(encoding="utf-8") == "do not replace"
    assert "diagnostic" in caplog.text.lower()


def test_successful_answer_does_not_create_diagnostics_directory(tmp_path):
    directory = tmp_path / "unneeded-diagnostics"
    answer, _ = collector.KukaClient(answer_session(), diagnostics_dir=directory).ask(QUESTION)
    assert answer == ANSWER
    assert not directory.exists()


def test_diagnostics_publish_complete_private_json_atomically_with_unique_names(
    tmp_path, monkeypatch,
):
    directory = tmp_path / "diagnostics"
    original_replace = collector.os.replace
    publications = []

    def replace(source, destination):
        source, destination = Path(source), Path(destination)
        assert not destination.exists()
        payload = json.loads(source.read_text(encoding="utf-8"))
        assert payload["state"] == "unverified"
        assert payload["streamed_answer"].startswith("stream-")
        assert payload["saved_answer"].startswith("saved-")
        if os.name != "nt":
            assert source.stat().st_mode & 0o777 == 0o600
        publications.append(destination)
        return original_replace(source, destination)

    monkeypatch.setattr(collector.os, "replace", replace)

    def capture(number):
        client = collector.KukaClient(object(), diagnostics_dir=directory)
        identifiers = {"uuid": f"private-generation-{number}"}
        client._write_mismatch_diagnostic(
            QUESTION, f"stream-{number}", f"saved-{number}", identifiers, 1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(capture, range(2)))
    assert len(set(publications)) == 2
    paths = list(directory.glob("*.json"))
    assert set(paths) == set(publications)
    assert {json.loads(path.read_text())["streamed_answer"] for path in paths} == {"stream-0", "stream-1"}
    assert not list(directory.glob("*.tmp"))


def test_diagnostic_publication_failure_cleans_temporary_file_and_hides_error_body(
    tmp_path, monkeypatch, caplog,
):
    directory = tmp_path / "diagnostics"

    def failed_replace(*args):
        raise OSError("signed-url?signature=private-token; private-password")

    monkeypatch.setattr(collector.os, "replace", failed_replace)
    session = answer_session(saved="different saved answer")
    with pytest.raises(collector.UnverifiedAnswerError, match="Stream and saved answer differ"):
        collector.KukaClient(session, retries=0, diagnostics_dir=directory).ask(QUESTION)
    assert not list(directory.iterdir())
    assert "private-token" not in caplog.text
    assert "private-password" not in caplog.text


def test_cli_no_diagnostics_keeps_failure_bodies_out_of_files(tmp_path, monkeypatch):
    source = tmp_path / "questions.md"
    source.write_text("## 0019 问题\n\n待回答问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    session = answer_session(saved="private mismatching body")
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(collector, "load_credentials", lambda username=None: ("test-user", "test-secret"))
    monkeypatch.setattr(collector.KukaClient, "login", lambda *args, **kwargs: None)
    assert collector.main([
        "--questions", str(source), "--output", str(output), "--concurrency", "1",
        "--retries", "0", "--no-diagnostics",
    ]) == 1
    assert not output.exists()
    assert not list(tmp_path.rglob("*.json"))


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


def test_concurrency_uses_script_local_dotenv_from_another_directory(tmp_path, monkeypatch, capsys):
    local_env = tmp_path / "checkout/utils/krl/.env"
    local_env.parent.mkdir(parents=True)
    local_env.write_text('COLLECT_CONCURRENCY="3"\nUNRELATED_KRL_SETTING=not-exported\n', encoding="utf-8")
    (tmp_path / ".env").write_text("COLLECT_CONCURRENCY=9\n", encoding="utf-8")
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 工艺\n\n问题\n", encoding="utf-8")
    output = tmp_path / "uncreated/qa.md"
    monkeypatch.setattr(collector, "ENV_FILE", local_env)
    monkeypatch.delenv("COLLECT_CONCURRENCY")
    monkeypatch.delenv("UNRELATED_KRL_SETTING", raising=False)
    monkeypatch.chdir(tmp_path)

    assert collector.main([
        "--questions", str(questions), "--output", str(output), "--dry-run",
    ]) == 0
    assert "Concurrency: 3" in capsys.readouterr().out
    assert "COLLECT_CONCURRENCY" not in collector.os.environ
    assert "UNRELATED_KRL_SETTING" not in collector.os.environ
    assert not output.parent.exists()


def test_concurrency_environment_and_cli_precedence(tmp_path, monkeypatch, capsys):
    local_env = tmp_path / ".env"
    local_env.write_text("COLLECT_CONCURRENCY=invalid-file-setting\n", encoding="utf-8")
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 工艺\n\n问题\n", encoding="utf-8")
    arguments = ["--questions", str(questions), "--output", str(tmp_path / "qa.md"), "--dry-run"]
    monkeypatch.setattr(collector, "ENV_FILE", local_env)
    monkeypatch.setenv("COLLECT_CONCURRENCY", "4")
    assert collector.main(arguments) == 0
    assert "Concurrency: 4" in capsys.readouterr().out
    monkeypatch.setenv("COLLECT_CONCURRENCY", "invalid-environment-setting")
    assert collector.main([*arguments, "--concurrency", "2"]) == 0
    assert "Concurrency: 2" in capsys.readouterr().out


@pytest.mark.parametrize("contents", [None, "UNRELATED_KRL_SETTING=value\n"])
def test_unset_concurrency_defaults_to_one(tmp_path, monkeypatch, contents):
    local_env = tmp_path / ".env"
    if contents is not None:
        local_env.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(collector, "ENV_FILE", local_env)
    monkeypatch.delenv("COLLECT_CONCURRENCY")
    assert collector.load_concurrency() == 1


@pytest.mark.parametrize("source", ["environment", "dotenv"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "", "private-invalid-value"])
def test_invalid_concurrency_setting_is_rejected_without_echoing_value(tmp_path, monkeypatch, capsys, source, value):
    local_env = tmp_path / ".env"
    monkeypatch.setattr(collector, "ENV_FILE", local_env)
    if source == "environment":
        monkeypatch.setenv("COLLECT_CONCURRENCY", value)
    else:
        monkeypatch.delenv("COLLECT_CONCURRENCY")
        local_env.write_text(f"COLLECT_CONCURRENCY='{value}'\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        collector.main(["--dry-run"])
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "COLLECT_CONCURRENCY must be a positive integer" in stderr
    assert "private-invalid-value" not in stderr


def test_help_does_not_load_dotenv(monkeypatch):
    def fail_load():
        raise AssertionError("Help must not load local settings")

    monkeypatch.setattr(collector, "_load_local_settings", fail_load)
    monkeypatch.delenv("COLLECT_CONCURRENCY")
    with pytest.raises(SystemExit) as error:
        collector.main(["--help"])
    assert error.value.code == 0


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
        state.login_client = client
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
        assert client._conversation_lock is state.login_client._conversation_lock
        assert client._used_conversations is state.login_client._used_conversations
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


def test_concurrent_source_order_batches_despite_completion_order_and_resume(concurrent_cli, monkeypatch):
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
    assert first.index("样本0019".encode()) < first.index("样本0020".encode())
    assert second.index("样本0021".encode()) < second.index("样本0022".encode())
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
def test_terminal_failure_stops_dispatch_drains_success_and_resumes(concurrent_cli, monkeypatch, caplog, delay):
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
        assert "Question 0019 failed: terminal failure" in caplog.text
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
    assert [record.question.id for record in _saved_records(state.output)] == list(range(19, 25))


@pytest.mark.parametrize("make_error", [
    pytest.param(lambda: collector.UnverifiedAnswerError("Stream and saved answer differ"), id="answer-mismatch"),
    pytest.param(lambda: collector.ApiRejectedError("/api/session/saveOrUpdate", "1"), id="conversation-rejected"),
    pytest.param(lambda: collector.StreamRetryExhaustedError("Interrupted answer stream"), id="chunked-stream-exhausted"),
])
@pytest.mark.parametrize("recovery", ["success", "repeat-failure", "disabled"])
def test_concurrent_failures_drain_before_bounded_serial_recovery(
    concurrent_cli, monkeypatch, recovery, make_error,
):
    state = concurrent_cli
    started = {number: threading.Event() for number in (19, 21)}
    draining = threading.Event()
    calls = []

    # Recovery can use either log level; synchronize on the drain transition,
    # not scheduling delays or the exact diagnostic text.
    for level in ("warning", "error"):
        original = getattr(collector.LOG, level)

        def log(message, *args, original=original, **kwargs):
            original(message, *args, **kwargs)
            if "draining" in message:
                draining.set()

        monkeypatch.setattr(collector.LOG, level, log)

    def ask(client, question):
        assert not client.session.closed
        calls.append(question.id)
        if question.id in started:
            started[question.id].set()
            assert draining.wait(5), "Active answers must drain before recovery"
            assert set(calls) == {19, 20, 21}, "New questions started while draining"
            return ANSWER, {}
        if question.id == 20:
            if calls.count(20) == 1:
                assert all(event.wait(5) for event in started.values())
                raise make_error()
            assert state.saved_events[19].is_set() and state.saved_events[21].is_set()
            assert calls.count(20) == 2, "Recovery must be bounded to one fresh conversation"
            assert set(calls) == {19, 20, 21}, "Pending questions preceded recovery"
            if recovery == "repeat-failure":
                raise make_error()
            return ANSWER, {}
        # Once the failed question recovers, keep the remaining work serial.
        assert state.saved_events[question.id - 1].is_set()
        return ANSWER, {}

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    arguments = [*state.arguments, "--concurrency", "3", "--retry-delay", "0"]
    if recovery == "disabled":
        arguments.extend(["--retries", "0"])
    assert collector.main(arguments) == (0 if recovery == "success" else 1)
    assert all(session.closed for session in state.sessions)
    completed = collector.CollectionStore(state.output, state.questions).completed_ids
    if recovery == "success":
        assert calls[3:] == [20, 22, 23, 24]
        assert completed == set(range(19, 25))
        assert state.saved.index(21) < state.saved.index(20)
    else:
        assert calls.count(20) == (1 if recovery == "disabled" else 2)
        assert set(calls) == {19, 20, 21}
        assert completed == {19, 21}
        before = {
            record.question.id: (record.question, record.answer, record.metadata)
            for record in _saved_records(state.output)
        }
        calls.clear()

        def resumed_ask(client, question):
            calls.append(question.id)
            return ANSWER, {}

        monkeypatch.setattr(collector.KukaClient, "ask", resumed_ask)
        assert collector.main(arguments) == 0
        assert set(calls) == {20, 22, 23, 24}
        after = {
            record.question.id: (record.question, record.answer, record.metadata)
            for record in _saved_records(state.output)
        }
        assert all(after[number] == saved for number, saved in before.items())
    assert [record.question.id for record in _saved_records(state.output)] == list(range(19, 25))


@pytest.mark.parametrize("failure", ["terminal", "output-write"])
def test_terminal_failure_during_recovery_drain_discards_queued_retry(
    concurrent_cli, monkeypatch, failure,
):
    state = concurrent_cli
    state.original_save(
        collector.CollectionStore(state.output, state.questions), state.questions[0],
        ANSWER, {"session_id": "already-complete"},
    )
    before = state.output.read_bytes()
    original_record, = _saved_records(state.output)
    started = {number: threading.Event() for number in (21, 22)}
    recovery_draining = threading.Event()
    terminal_seen = threading.Event()
    calls = []
    save_attempts = []

    original_warning = collector.LOG.warning
    original_error = collector.LOG.error

    def warning(message, *args, **kwargs):
        original_warning(message, *args, **kwargs)
        if "draining" in message:
            recovery_draining.set()

    def error(message, *args, **kwargs):
        original_error(message, *args, **kwargs)
        if "Output write failed" in message or (
            "Question %04d failed" in message and args[0] == 21
        ):
            terminal_seen.set()

    original_save = collector.CollectionStore.save

    def save(store, question, answer, metadata=None):
        save_attempts.append(question.id)
        if failure == "output-write" and question.id == 21:
            raise OSError("simulated output failure")
        return original_save(store, question, answer, metadata)

    def ask(client, question):
        calls.append(question.id)
        if question.id == 20:
            assert all(event.wait(5) for event in started.values())
            raise collector.UnverifiedAnswerError("Stream and saved answer differ")
        assert question.id in started, "A queued retry or new question was dispatched"
        started[question.id].set()
        assert recovery_draining.wait(5)
        if question.id == 21:
            if failure == "terminal":
                raise collector.CollectionError("terminal failure during drain")
            return ANSWER, {}
        assert terminal_seen.wait(5), "The late answer must follow the terminal error"
        return ANSWER, {}

    monkeypatch.setattr(collector.LOG, "warning", warning)
    monkeypatch.setattr(collector.LOG, "error", error)
    monkeypatch.setattr(collector.CollectionStore, "save", save)
    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    assert collector.main([
        *state.arguments, "--concurrency", "3", "--retry-delay", "0",
    ]) == 1
    assert sorted(calls) == [20, 21, 22]
    assert all(session.closed for session in state.sessions)
    records = _saved_records(state.output)
    first = next(record for record in records if record.question.id == 19)
    assert (first.question, first.answer, first.metadata) == (
        original_record.question, original_record.answer, original_record.metadata,
    )
    if failure == "output-write":
        assert save_attempts == [21], "Publication continued after the output write failed"
        assert state.output.read_bytes() == before
    else:
        assert save_attempts == [22]
        assert {record.question.id for record in records} == {19, 22}


def test_unknown_worker_exception_is_logged_immediately_without_sensitive_text(concurrent_cli, monkeypatch, caplog):
    state = concurrent_cli
    second_started = threading.Event()

    def ask(client, question):
        if question.id == 19:
            assert second_started.wait(5)
            raise requests.ConnectionError(
                "https://example.test/api/login?signature=private-token; password=private-password"
            )
        second_started.set()
        assert state.draining.wait(5)
        assert "Question 0019 failed: ConnectionError" in caplog.text
        assert "private-token" not in caplog.text
        assert "private-password" not in caplog.text
        return ANSWER, {}

    monkeypatch.setattr(collector.KukaClient, "ask", ask)
    assert collector.main(state.arguments) == 1
    assert state.saved == [20]
    assert "private-token" not in caplog.text and "private-password" not in caplog.text


def test_live_run_repairs_previous_completion_order_before_login(concurrent_cli):
    state = concurrent_cli
    store = collector.CollectionStore(state.output, state.questions)
    for question in state.questions:
        state.original_save(store, question, ANSWER, {"session_id": f"session-{question.id}"})
    records = _saved_records(state.output)
    prefix = store._prefixes[state.output]
    state.output.write_bytes(prefix + b"\n\n".join(record.raw for record in reversed(records)))
    before = state.output.read_bytes()

    assert collector.main([*state.arguments, "--dry-run"]) == 0
    assert state.output.read_bytes() == before
    assert collector.main(state.arguments) == 0
    assert [record.question.id for record in _saved_records(state.output)] == list(range(19, 25))
    assert all(record.answer == ANSWER for record in _saved_records(state.output))
    assert not state.sessions and not state.logins


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
        return ANSWER, {
            "sessionId": f"session-{question.id}", "requestId": f"session-{question.id}",
            "uuid": f"uuid-{question.id}",
        }, None

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


def _legacy_heading_collection(tmp_path, first_id=19):
    questions = tmp_path / "questions.md"
    questions.write_text(f"## {first_id:04d} 工艺\n\n完整问题\n\n"
                         f"## {first_id + 1:04d} 后续工艺\n\n后续问题\n", encoding="utf-8")
    output = tmp_path / "qa.md"
    answer = "## 主节\n\n### 子节\n原始正文  \n\n```krl\n# 代码内容保持原样\nEND\n```"
    output.write_text(f"# 历史集合\n\n## 样本{first_id:04d}：工艺\n\n"
                      "### 问题\n\n完整问题\n\n### 小库原始回答\n\n" + answer, encoding="utf-8")
    return questions, output, answer


@pytest.mark.parametrize("first_id", [1, 19])
def test_repair_headings_is_offline_and_independent_of_local_settings(tmp_path, monkeypatch, first_id):
    questions, output, answer = _legacy_heading_collection(tmp_path, first_id)
    source_before = questions.read_bytes()
    original_import = builtins.__import__

    def offline_import(name, *args, **kwargs):
        if name == "requests" or name.startswith("requests."):
            raise ModuleNotFoundError("requests intentionally unavailable")
        assert name != "dotenv", "Heading repair must not import dotenv"
        return original_import(name, *args, **kwargs)

    def fail_live_setup(*args, **kwargs):
        raise AssertionError("Offline repair must not read settings, credentials, or log in")

    monkeypatch.setattr(builtins, "__import__", offline_import)
    monkeypatch.delenv("COLLECT_CONCURRENCY")
    monkeypatch.setattr(collector, "_load_local_settings", fail_live_setup)
    monkeypatch.setattr(collector, "load_credentials", fail_live_setup)
    monkeypatch.setattr(collector.KukaClient, "login", fail_live_setup)
    monkeypatch.setattr(collector.KukaClient, "ask", fail_live_setup)
    arguments = ["--questions", str(questions), "--output", str(output), "--repair-headings"]

    # The unsaved question must remain pending: repair never starts collection.
    assert collector.main(arguments) == 0
    repaired = output.read_bytes()
    assert "#### 主节\n\n##### 子节".encode("utf-8") in repaired
    records = _saved_records(output)
    assert [record.question.id for record in records] == [first_id]
    assert records[0].answer == answer
    assert questions.read_bytes() == source_before
    assert collector.main(arguments) == 0
    assert output.read_bytes() == repaired


def test_repair_headings_cannot_be_combined_with_dry_run(tmp_path, capsys):
    questions, output, _ = _legacy_heading_collection(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(SystemExit) as error:
        collector.main(["--questions", str(questions), "--output", str(output),
                        "--repair-headings", "--dry-run"])
    assert error.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_regular_cli_repairs_legacy_headings_before_no_pending_exit(tmp_path, monkeypatch):
    questions, output, answer = _legacy_heading_collection(tmp_path)

    def fail_network(*args, **kwargs):
        raise AssertionError("Already saved questions must not load credentials or log in")

    monkeypatch.setattr(collector, "load_credentials", fail_network)
    monkeypatch.setattr(requests, "Session", fail_network)
    monkeypatch.setattr(collector.KukaClient, "login", fail_network)
    arguments = ["--questions", str(questions), "--output", str(output), "--end", "19"]
    assert collector.main(arguments) == 0
    repaired = output.read_bytes()
    assert "#### 主节\n\n##### 子节".encode("utf-8") in repaired
    records = _saved_records(output)
    assert [(record.question.id, record.answer) for record in records] == [(19, answer)]
    assert collector.main(arguments) == 0
    assert output.read_bytes() == repaired


def test_dry_run_leaves_legacy_heading_hierarchy_and_files_untouched(tmp_path, monkeypatch, capsys):
    questions, output, answer = _legacy_heading_collection(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}

    def fail_repair(*args, **kwargs):
        raise AssertionError("Dry runs must not repair files or begin collection")

    monkeypatch.setattr(collector.CollectionStore, "ensure_heading_hierarchy", fail_repair)
    monkeypatch.setattr(collector, "load_credentials", fail_repair)
    monkeypatch.setattr(requests, "Session", fail_repair)
    assert collector.main(["--questions", str(questions), "--output", str(output), "--dry-run"]) == 0
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert "saved: 1; pending: 1" in capsys.readouterr().out
    assert _saved_records(output)[0].answer == answer
