#!/usr/bin/env python3
"""Collect original KUKA Center KRL answers into resumable Markdown batches.

Run ``python utils/krl/collect_krl_qa.py --dry-run`` first. Live runs read the
KUKACENTER_ACCOUT and KUKACENTER_PASSWORD values from utils/krl/.env beside this
script. COLLECT_CONCURRENCY in that file sets the default concurrent questions
(otherwise 1). Existing environment values take precedence. Use --username and
--concurrency to override their settings. The CLI automatically accepts the
site's login policies and prompts for the image CAPTCHA. Use --auto-captcha
to recognize and submit the four characters with the optional local OCR engine.

Uses the same signed login, KRL model, streaming, answer check, and saved-answer
endpoints as the site's chat frontend. No browser dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import getpass
import hashlib
import json
import logging
import math
import os
import re
import ssl
import sys
import tempfile
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

if __package__:
    from .krl_collection_store import CollectionStore, Question, parse_questions
    from .solve_captcha import CaptchaError, solve_captcha
else:
    from krl_collection_store import CollectionStore, Question, parse_questions
    from solve_captcha import CaptchaError, solve_captcha


ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = Path(__file__).resolve().with_name(".env")
MAIN_URL = "https://www.kukacenter.com"
CHAT_URL = "https://chat.kukacenter.com"
MODEL = "krl_question_robot"  # The frontend's KRL编程 tab.


@dataclass(frozen=True)
class CollectionProfile:
    """Frontend mode and local defaults shared by the chatbot collectors."""

    key: str
    mode: str
    robot_model: str | None
    questions: Path
    output: Path
    start: int
    title: str
    answer_path: str
    stream_path: str = "/api/efs/stream"


KRL_PROFILE = CollectionProfile(
    key="krl",
    mode="KRL编程",
    robot_model=MODEL,
    questions=ROOT / "assets/KRL/krl_questions_1000.md",
    output=ROOT / "assets/KRL/krl_qa_collected.md",
    start=19,
    title="KRL工艺参数问答采集",
    answer_path="/api/krl/selectQuestionInfo",
)

REFUSAL = "**输出答案** 非常抱歉！由于回答中可能涉及敏感词，无法提供答案。请重新输入问答，感谢您的理解和支持！"
FAQ_STREAM_PREFIX = "##FAQ##"
TERMS = (
    f"{MAIN_URL}/policy/kuka_center_legal_statement.html",
    f"{MAIN_URL}/policy/kuka_center_privacy_policy.html",
    f"{MAIN_URL}/policy/kuka_bbs_policy.html",
)
LOG = logging.getLogger("collect_krl_qa")
MAX_FAILED_RUNS_WITHOUT_PROGRESS = 4


class CollectionError(RuntimeError):
    """A failure for which the current answer must not be published."""


class LoginCancelled(CollectionError):
    """The user explicitly cancelled at a login prompt."""


class ApiRejectedError(CollectionError):
    """An API rejection with a sanitized application code."""

    def __init__(self, path, code):
        self.path = path
        self.code = code
        super().__init__(f"{path}: API rejected request (code={code})")


class RetryableCollectionError(CollectionError):
    """A transient transport or persistence failure; retry the affected stage."""


class ConversationTransportError(RetryableCollectionError):
    """The connection failed while opening a new conversation."""


class UnsentStreamRequestError(RetryableCollectionError):
    """The stream connection failed before the HTTP POST could be sent."""


class ChunkedAnswerStreamError(RetryableCollectionError):
    """An answer arrived with incomplete or invalid HTTP framing."""

    def __init__(self, received_bytes: int, elapsed: float):
        self.received_bytes = received_bytes
        super().__init__(
            f"Answer stream interrupted {elapsed:.1f}s after headers with "
            f"{received_bytes} bytes delivered (ChunkedEncodingError). Discarded the stream"
        )


class StreamRetryExhaustedError(CollectionError):
    """Generation retries ended with an interrupted chunked stream."""


class AnswerMismatchError(RetryableCollectionError):
    """The persisted answer does not yet match the completed stream."""


class UnverifiedAnswerError(CollectionError):
    """Fetch retries ended with a mismatch; generation may need to be restarted."""


def _answer_for_comparison(answer: str) -> str:
    """Ignore newline encoding and outer newlines, retaining Markdown spaces."""
    return answer.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def http_failure(exc: Exception, context: str, *, opening_stream=False) -> CollectionError | None:
    """Classify requests errors without exposing URLs, credentials, or bodies."""
    from requests.exceptions import (
        ChunkedEncodingError,
        ConnectionError,
        ConnectTimeout,
        ProxyError,
        ReadTimeout,
        RequestException,
        SSLError,
        Timeout,
    )
    from urllib3.exceptions import (
        ConnectTimeoutError,
        NewConnectionError,
        ReadTimeoutError,
    )
    from urllib3.util.ssl_match_hostname import CertificateError

    if not isinstance(exc, RequestException):
        return None
    # Requests wraps urllib3 and OpenSSL failures. In particular, urllib3's
    # MaxRetryError can keep the original exception in its reason attribute.
    # Inspect types and fixed error codes to avoid exposing request data.
    causes = [exc]
    seen = set()
    timeout = None
    tls_closed = False
    certificate_failed = False
    while causes:
        cause = causes.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, (ReadTimeout, ReadTimeoutError)):
            timeout = "HTTP read timeout"
        elif isinstance(cause, (ConnectTimeout, ConnectTimeoutError)) and not isinstance(
            cause, NewConnectionError
        ):
            timeout = "HTTP connect timeout"
        elif isinstance(cause, Timeout) and timeout is None:
            timeout = "HTTP read/connect timeout"
        reason = getattr(cause, "reason", None)
        if isinstance(reason, BaseException):
            causes.append(reason)
        if isinstance(cause, (ssl.SSLCertVerificationError, CertificateError)):
            certificate_failed = True
        elif isinstance(cause, ssl.SSLError):
            if reason == "CERTIFICATE_VERIFY_FAILED":
                certificate_failed = True
            elif (
                isinstance(cause, (ssl.SSLEOFError, ssl.SSLZeroReturnError))
                or cause.errno in (ssl.SSL_ERROR_EOF, ssl.SSL_ERROR_ZERO_RETURN)
                or reason == "UNEXPECTED_EOF_WHILE_READING"
            ):
                tls_closed = True
        elif isinstance(cause, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            tls_closed = True
        causes.extend(arg for arg in cause.args if isinstance(arg, BaseException))
        causes.extend(
            nested
            for nested in (cause.__cause__, cause.__context__)
            if nested is not None
        )
    if isinstance(exc, SSLError):
        if certificate_failed:
            return CollectionError(f"{context}: TLS certificate verification failed (SSLError)")
        if tls_closed:
            return RetryableCollectionError(f"{context}: TLS connection closed or reset (SSLError)")
        return CollectionError(f"{context}: TLS error (SSLError)")
    retryable = isinstance(exc, (ConnectionError, Timeout, ChunkedEncodingError))
    error_type = RetryableCollectionError if retryable else CollectionError
    if opening_stream and isinstance(exc, (ConnectTimeout, ProxyError)):
        # A connect timeout or failure establishing the HTTPS proxy tunnel
        # occurs before the POST. Read failures can occur after submission.
        error_type = UnsentStreamRequestError
    reason = timeout or type(exc).__name__
    if timeout and isinstance(exc, ProxyError):
        reason += " via proxy"
    return error_type(f"{context}: {reason}")


def _load_local_settings():
    """Read the script-local .env without interpolation or environment exports."""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise CollectionError(
            "Install python-dotenv: python -m pip install python-dotenv"
        ) from exc
    # Credentials can contain literal ${...}; do not interpolate password text.
    return dotenv_values(ENV_FILE, interpolate=False)


def load_concurrency() -> int:
    """Resolve the default from the process environment, local .env, then 1."""
    if "COLLECT_CONCURRENCY" in os.environ:
        value = os.environ["COLLECT_CONCURRENCY"]
    else:
        value = (
            _load_local_settings().get("COLLECT_CONCURRENCY")
            if ENV_FILE.is_file() else None
        )
    if value is None:
        return 1
    try:
        concurrency = int(value)
    except (TypeError, ValueError):
        raise CollectionError("COLLECT_CONCURRENCY must be a positive integer") from None
    if concurrency < 1:
        raise CollectionError("COLLECT_CONCURRENCY must be a positive integer")
    return concurrency


def load_credentials(username=None):
    """Read login settings without exporting unrelated .env values or logging secrets."""
    settings = _load_local_settings()
    account = (
        username
        or os.environ.get("KUKACENTER_ACCOUT")
        or settings.get("KUKACENTER_ACCOUT")
        or "13918961550"
    )
    password = os.environ.get(
        "KUKACENTER_PASSWORD", settings.get("KUKACENTER_PASSWORD")
    )
    return account, password


class LoginForm(HTMLParser):
    """Extract the CAS form's hidden flow fields, without running page scripts."""

    def __init__(self, html: str):
        super().__init__()
        self.in_form = False
        self.found = False
        self.fields: dict[str, str] = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and attributes.get("id") == "fm1":
            self.in_form = self.found = True
        elif tag == "input" and self.in_form:
            if attributes.get("type") == "hidden" and attributes.get("name"):
                self.fields[attributes["name"]] = attributes.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


@contextmanager
def output_lock(output: Path):
    """Hold an OS lock, released even after a crash, for this output series."""
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_name(f".{output.stem}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CollectionError(f"Another collector owns {output}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CollectionError(f"Another collector owns {output}") from exc
        locked = True
        yield
    finally:
        if locked and os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)
        # Keep the lock inode: unlinking permits another process to evade it.


def read_stream(response, timeout: float) -> str:
    """Decode full raw Markdown; an interrupted stream is never a success."""
    from requests.exceptions import ChunkedEncodingError

    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type or "html" in content_type:
        raise CollectionError(
            "Chat returned an error/login document instead of an answer"
        )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    parts = []
    received_bytes = 0
    started = time.monotonic()
    deadline = started + timeout
    try:
        for chunk in response.iter_content(chunk_size=4096):
            if time.monotonic() > deadline:
                raise CollectionError("Answer exceeded --answer-timeout; nothing saved")
            if chunk:
                received_bytes += len(chunk)
                parts.append(decoder.decode(chunk))
    except ChunkedEncodingError as exc:
        raise ChunkedAnswerStreamError(
            received_bytes, time.monotonic() - started
        ) from exc
    except Exception as exc:
        failure = http_failure(
            exc, "Answer stream interrupted; incomplete answer discarded"
        )
        if failure is None:
            raise
        raise failure from exc
    parts.append(decoder.decode(b"", final=True))
    answer = "".join(parts)
    if not answer.strip():
        raise CollectionError("Chat returned an empty answer; nothing saved")
    return answer


class KukaClient:
    def __init__(
        self, session, request_timeout=180.0, answer_timeout=900.0,
        retries=3, retry_delay=5.0, *, profile=KRL_PROFILE, diagnostics_dir=None,
        recover_saved_answer=False, auto_captcha=False,
    ):
        self.session = session
        self.request_timeout = request_timeout
        self.answer_timeout = answer_timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.profile = profile
        self.diagnostics_dir = Path(diagnostics_dir) if diagnostics_dir is not None else None
        self.recover_saved_answer = recover_saved_answer and profile.key == "kqa"
        self.auto_captcha = auto_captcha
        self._diagnosed_generations = set()
        self._conversation_lock = Lock()
        self._used_conversations = set()

    def retry(self, operation, action):
        """Bound transport/persistence retries with a sanitized reason and backoff."""
        delay = min(self.retry_delay, 60.0)
        for attempt in range(self.retries + 1):
            if attempt:
                LOG.info("Retrying %s (attempt %d/%d)", operation, attempt + 1, self.retries + 1)
            try:
                return action()
            except RetryableCollectionError as exc:
                if attempt == self.retries:
                    if isinstance(exc, ChunkedAnswerStreamError):
                        error_type = StreamRetryExhaustedError
                    elif isinstance(exc, AnswerMismatchError):
                        error_type = UnverifiedAnswerError
                    elif isinstance(exc, ConversationTransportError):
                        error_type = ConversationTransportError
                    else:
                        error_type = CollectionError
                    raise error_type(
                        f"{operation} failed after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                LOG.warning(
                    "%s failed (attempt %d/%d): %s; retrying in %g seconds",
                    operation, attempt + 1, self.retries + 1, exc, delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 60.0)

    def request(self, method, url, **kwargs):
        # No blanket POST retries: callers choose stage boundaries explicitly.
        kwargs.setdefault("timeout", (20, self.request_timeout))
        if urlsplit(url).hostname == "chat.kukacenter.com":
            kwargs.setdefault(
                "headers", {"Origin": CHAT_URL, "Referer": CHAT_URL + "/"}
            )
        try:
            response = self.session.request(method, url, **kwargs)
        except Exception as exc:
            address = urlsplit(url)
            failure = http_failure(
                exc, f"{method} {address.path}",
                opening_stream=(address.scheme == "https" and kwargs.get("stream", False)
                                and kwargs.get("allow_redirects") is False),
            )
            if failure is not None:
                raise failure from exc
            raise CollectionError(
                f"{method} {urlsplit(url).path}: {type(exc).__name__}; request failed"
            ) from exc
        if not 200 <= response.status_code < 300:
            response.close()
            error_type = (
                RetryableCollectionError
                if response.status_code in (408, 429, 500, 502, 503, 504)
                else CollectionError
            )
            raise error_type(
                f"{method} {urlsplit(url).path}: HTTP {response.status_code}"
            )
        return response

    def api(self, path, payload=None, *, main_site=False, files=None, timeout=None):
        # The main frontend's HTTP wrapper prefixes service paths such as
        # /api-b/... with /api. The unprefixed sign URL returns HTTP 463.
        # Chat has its own /api/... routes and does not use that proxy prefix.
        host = MAIN_URL + "/api" if main_site else CHAT_URL
        options = {} if timeout is None else {"timeout": timeout}
        if main_site:
            response = self.request("GET", host + path, params=payload, **options)
        elif files is not None:
            # requests supplies the multipart boundary and Content-Type.
            response = self.request("POST", host + path, files=files, **options)
        else:
            response = self.request("POST", host + path, json=payload or {}, **options)
        with response:
            try:
                data = response.json()
            except ValueError as exc:
                raise CollectionError(
                    f"{path}: expected JSON; login may have expired"
                ) from exc
        code = str(data.get("code")) if isinstance(data, dict) else ""
        # The frontend reads the training status payload even when this lookup
        # returns code=1, including when there is no training record yet.
        status_lookup = path == "/api/krl/queryStatus" and code == "1"
        if code not in ("0", "200", "ok") and not status_lookup:
            # Only bounded numeric API codes are safe to echo. Never include
            # arbitrary server messages, signed URLs, or response bodies.
            safe_code = code if re.fullmatch(r"-?[0-9]{1,10}", code) else "unavailable"
            raise ApiRejectedError(path, safe_code)
        return data.get("data")

    def _fetch_captcha(self, login_url: str) -> tuple[str, bytes]:
        captcha_uuid = str(uuid.uuid4())
        with self.request(
            "GET",
            "https://sso.kukacenter.com/cas/captcha",
            params={"uuid": captcha_uuid},
            headers={"Referer": login_url},
        ) as response:
            if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                raise CollectionError("Login CAPTCHA endpoint did not return an image")
            return captcha_uuid, response.content

    def login(self, username, password=None, *, accept_terms=False):
        with self.retry(
            "Open KUKA Center login page",
            lambda: self.request("GET", MAIN_URL + "/chat"),
        ) as response:
            login_url = response.url
            form = LoginForm(response.text)
        if form.found:
            address = urlsplit(login_url)
            if (
                address.scheme != "https"
                or address.hostname != "sso.kukacenter.com"
                or address.path != "/cas/login"
            ):
                raise CollectionError(
                    "Unexpected login destination; credentials were not sent"
                )
            if not form.fields.get("execution"):
                raise CollectionError(
                    "Login flow token is missing; site interface may have changed"
                )
            if not accept_terms:
                print("KUKA Center login requires agreement to these site policies:")
                print("\n".join(TERMS))
                if (
                    input("Type yes to accept and continue login: ").strip().lower()
                    != "yes"
                ):
                    raise LoginCancelled("Login cancelled before sending credentials")
            if password is None:
                password = getpass.getpass("KUKA Center password: ")
            if not password:
                raise CollectionError("A password is required")
            with tempfile.TemporaryDirectory(prefix=f"{self.profile.key}-login-") as temporary:
                captcha_path = Path(temporary) / "captcha.png"
                captcha_uuid, image = self.retry(
                    "Fetch KUKA Center CAPTCHA",
                    lambda: self._fetch_captcha(login_url),
                )
                captcha_path.write_bytes(image)
                captcha_path.chmod(0o600)
                if self.auto_captcha:
                    try:
                        captcha = solve_captcha(image)
                    except CaptchaError as exc:
                        raise CollectionError(str(exc)) from exc
                    LOG.info("Read four CAPTCHA characters locally; submitting login")
                else:
                    print(
                        f"Open this image and read the site's CAPTCHA: {captcha_path}",
                        flush=True,
                    )
                    captcha = input("CAPTCHA (4 characters, or blank to cancel): ").strip()
                    if not captcha:
                        raise LoginCancelled("Login cancelled at the CAPTCHA prompt")
                if len(captcha) != 4:
                    raise CollectionError("A four-character CAPTCHA is required")
                fields = dict(form.fields)
                fields.update(
                    username=username,
                    # The site's beforeSubmit() applies md5 to the password.
                    password=hashlib.md5(
                        password.encode("utf-8"), usedforsecurity=False
                    ).hexdigest(),
                    loginType="0",
                    uuid=captcha_uuid,
                    captcha=captcha,
                    _eventId="submit",
                )
                with self.request(
                    "POST",
                    login_url,
                    data=fields,
                    headers={
                        "Origin": "https://sso.kukacenter.com",
                        "Referer": login_url,
                    },
                ) as response:
                    if (
                        LoginForm(response.text).found
                        or urlsplit(response.url).hostname == "sso.kukacenter.com"
                    ):
                        raise CollectionError(
                            "Login failed. Check credentials/CAPTCHA, then rerun"
                        )
        # The main site signs its embedded chat login; CAS cookies alone do not
        # authenticate the chat host. Each requests.Session keeps both in memory.
        signature = self.retry(
            "Fetch KUKA Center chat signature",
            lambda: self.api(
                "/api-b/program_assistant/api/sign",
                {"nonceStr": str(uuid.uuid4())},
                main_site=True,
            ),
        )
        if not isinstance(signature, dict) or not all(
            signature.get(key) is not None and str(signature[key]) != ""
            for key in ("uuid", "timestamp", "nonceStr", "sign")
        ):
            raise CollectionError(
                "Signed chat login response is missing required fields"
            )
        self.api(
            "/api/login",
            {
                **{
                    key: str(signature[key])
                    for key in ("uuid", "timestamp", "nonceStr")
                },
                "signature": str(signature["sign"]),
            },
        )

    @staticmethod
    def validate_question(question: Question) -> None:
        """Check mode-specific input offline, before authentication or writes."""

    def prepare_question(self, question: Question, identifiers: dict) -> None:
        """Prepare conversation context, when required by a frontend mode."""

    def _create_conversation(self):
        session = self.api("/api/session/saveOrUpdate", {"sessionTitle": "新建对话"})
        session_id = session.get("sessionId") if isinstance(session, dict) else None
        if type(session_id) not in (str, int) or not session_id:
            raise CollectionError("Chat did not create a session")
        return session_id

    def _latest_empty_conversation(self):
        """Follow the frontend's history order and confirm the newest chat is empty."""
        history = self.api("/api/session/select")
        if not isinstance(history, dict):
            return None
        for period in ("TODAY", "SEVEN_DAYS", "THIRTY_DAYS"):
            conversations = history.get(period)
            if not isinstance(conversations, list):
                return None
            if conversations:
                break
        else:
            return None
        newest = conversations[0]
        session_id = newest.get("sessionId") if isinstance(newest, dict) else None
        if type(session_id) not in (str, int) or not session_id:
            return None
        if str(session_id) in self._used_conversations:
            return None
        details = self.api("/api/question/select", {"sessionId": session_id})
        if isinstance(details, dict) and details.get("questionInfos") == []:
            return session_id
        return None

    def _open_conversation(self):
        # Workers share the account's history. Keep creation and claiming an
        # empty conversation exclusive; generation runs outside the lock.
        with self._conversation_lock:
            try:
                session_id = self._create_conversation()
            except RetryableCollectionError as exc:
                raise ConversationTransportError(str(exc)) from exc
            except ApiRejectedError as exc:
                if exc.code != "1":
                    raise
                try:
                    session_id = self._latest_empty_conversation()
                except CollectionError as lookup_error:
                    LOG.warning("Could not check for an empty conversation (%s)", lookup_error)
                    raise exc from lookup_error
                if session_id is None:
                    raise
                LOG.info("Reusing KUKA Center's newest empty conversation")
            else:
                if str(session_id) in self._used_conversations:
                    raise CollectionError("Chat returned a conversation already used in this run")
            self._used_conversations.add(str(session_id))
            return session_id

    def generate(self, question: Question) -> tuple[str, dict, dict | None]:
        """Start a question in a conversation without prior turns."""
        return self._generate_prepared(question, self._prepare_generation(question))

    def _prepare_generation(self, question: Question) -> dict:
        session_id = self._open_conversation()
        identifiers = {
            "sessionId": session_id,
            "uuid": str(uuid.uuid4()),
            "requestId": session_id,
        }
        self.prepare_question(question, identifiers)
        return identifiers

    def _generate_prepared(self, question, identifiers):
        payload = dict(
            identifiers,
            question=question.prompt,
            repeatFlag=False,
        )
        if self.profile.robot_model is not None:
            payload.update(robotModel=self.profile.robot_model, networkSearchFlag=False)
        # With redirects disabled, a connect failure cannot be from a follow-up
        # request after the original POST reached the server.
        options = {"allow_redirects": False} if self.profile.key == "krlu" else {}
        try:
            with self.request(
                "POST",
                CHAT_URL + self.profile.stream_path,
                json=payload,
                stream=True,
                # Reasoning can be silent longer than ordinary API calls.
                timeout=(20, self.answer_timeout),
                **options,
            ) as response:
                streamed = read_stream(response, self.answer_timeout)
        except ChunkedAnswerStreamError as exc:
            if self.recover_saved_answer and exc.received_bytes == 0:
                LOG.warning("Question %04d: %s. Looking up the same attempt", question.id, exc)
                recovered = self._recover_saved_answer(question, identifiers)
                if recovered is not None:
                    return recovered["answerContent"], identifiers, recovered
            raise
        return streamed, identifiers, None

    def _write_mismatch_diagnostic(self, question, streamed, saved, identifiers, first_difference):
        """Keep one private response snapshot per generation, outside completed output."""
        generation = identifiers["uuid"]
        if self.diagnostics_dir is None or generation in self._diagnosed_generations:
            return
        self._diagnosed_generations.add(generation)
        temporary = None
        try:
            self.diagnostics_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            diagnostic = {
                "schema_version": 1,
                "state": "unverified",
                "question_id": question.id,
                "stream_chars": len(streamed),
                "saved_chars": len(saved),
                "first_difference": first_difference,
                "streamed_answer": streamed,
                "saved_answer": saved,
            }
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".question-{question.id:04d}-", suffix=".tmp",
                dir=self.diagnostics_dir,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(diagnostic, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            target = self.diagnostics_dir / f"question-{question.id:04d}-{uuid.uuid4().hex}.json"
            os.replace(temporary, target)
            LOG.warning("Unverified response diagnostic saved -> %s", target)
        except OSError as exc:
            LOG.warning(
                "Could not save verification diagnostics (%s); retaining the original mismatch",
                type(exc).__name__,
            )
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _fetch_saved_answer(self, question, identifiers):
        """Fetch nonblank Markdown for the exact submitted question and UUID."""
        saved = self.api(
            self.profile.answer_path,
            dict(identifiers, question=question.prompt, content=question.prompt),
        )
        # A completed stream need not be visible in persisted storage yet. Keep
        # these checks inside the fetch retry, sharing its bounded HTTP budget.
        if saved is None or (
            isinstance(saved, dict) and saved.get("answerContent") is None
        ):
            raise RetryableCollectionError(
                "Chat did not return a persisted Markdown answer; nothing saved"
            )
        if not isinstance(saved, dict) or not isinstance(saved.get("answerContent"), str):
            raise CollectionError(
                "Chat returned an invalid persisted Markdown answer; nothing saved"
            )
        answer = saved["answerContent"]
        if not answer.strip():
            raise RetryableCollectionError(
                "Chat returned an empty persisted Markdown answer; nothing saved"
            )
        return saved

    def _recover_saved_answer(self, question, identifiers):
        """Follow KQA's browser fallback, with identity and persistence checks."""
        def lookup():
            saved = self._fetch_saved_answer(question, identifiers)
            answer_id = saved.get("id")
            duration = saved.get("time")
            if (saved.get("uuid", identifiers["uuid"]) != identifiers["uuid"]
                    or saved.get("questionContent", question.prompt) != question.prompt):
                raise CollectionError("Saved response identity differs from the interrupted request")
            if (type(answer_id) not in (str, int) or not str(answer_id).strip()
                    or type(duration) is not int or duration <= 0):
                raise CollectionError("Saved response lacks an answer ID or positive generation time")
            return saved

        try:
            return self.retry(f"Question {question.id:04d} same-attempt answer lookup", lookup)
        except CollectionError as exc:
            LOG.warning("Question %04d: saved-answer recovery failed (%s)", question.id, exc)
            return None

    def _fetch_verified_answer(self, question, streamed, identifiers):
        """Refetch uncertain persistence without repeating generation or moderation."""
        saved = self._fetch_saved_answer(question, identifiers)
        answer = saved["answerContent"]
        expected = _answer_for_comparison(streamed)
        actual = _answer_for_comparison(answer)
        if self.profile.key == "kqa" and actual != expected and expected.startswith(FAQ_STREAM_PREFIX):
            # KQA can stream an FAQ marker that the saved answer omits.
            expected = expected.removeprefix(FAQ_STREAM_PREFIX).lstrip("\n")
        if actual != expected:
            first_difference = next(
                (index for index, (left, right) in enumerate(zip(expected, actual))
                 if left != right),
                min(len(expected), len(actual)),
            )
            self._write_mismatch_diagnostic(question, streamed, answer, identifiers, first_difference)
            # Counts explain persistent mismatches without logging answers,
            # prompts, credentials, or arbitrary server response text.
            raise AnswerMismatchError(
                "Stream and saved answer differ "
                f"(stream_chars={len(streamed)}, saved_chars={len(answer)}, "
                f"first_difference={first_difference}); "
                "refusing a potentially incomplete answer"
            )
        return saved

    def _generate_with_retries(self, question):
        return self.retry(
            f"Question {question.id:04d} generation (new conversation each attempt; "
            "the previous request may still complete remotely)",
            lambda: self.generate(question),
        )

    def ask(self, question: Question) -> tuple[str, dict]:
        streamed, identifiers, recovered = self._generate_with_retries(question)
        checked = self.retry(
            f"Question {question.id:04d} answer check",
            lambda: self.api(
                "/api/check", {"content": streamed, "uuid": identifiers["uuid"]}
            ),
        )
        if not isinstance(checked, dict) or not isinstance(checked.get("status"), bool):
            raise CollectionError(
                "Chat answer check returned no boolean status; nothing saved"
            )
        metadata = {
            "source": MAIN_URL + "/chat",
            "mode": self.profile.mode,
            "robot_model": self.profile.robot_model,
            "stream_path": self.profile.stream_path,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "session_id": identifiers["sessionId"],
            "question_uuid": identifiers["uuid"],
            "answer_check_passed": checked["status"],
        }
        if recovered is not None:
            metadata["answer_source"] = "persisted_after_stream_failure"
        if not checked["status"]:
            # Exactly the replacement displayed by the frontend's answer check.
            return REFUSAL, metadata
        saved = recovered
        if saved is None:
            saved = self.retry(
                f"Question {question.id:04d} saved answer fetch/verification",
                lambda: self._fetch_verified_answer(question, streamed, identifiers),
            )
        answer = saved["answerContent"]
        metadata.update(answer_id=saved.get("id"), server_time=saved.get("time"))
        return answer, metadata


def _save_answer(store, question, answer, metadata):
    path = store.save(question, answer, metadata)
    LOG.info("Saved %04d (%d characters) -> %s", question.id, len(answer), path)


def _ask_worker(client, question):
    # Transport KeyboardInterrupt/SystemExit as outcomes too: raising them in an
    # asyncio task would interrupt the loop before other threads can be drained.
    try:
        answer, metadata = client.ask(question)
        return answer, metadata, None
    except BaseException as exc:
        return None, None, exc


def _failure_reason(error):
    """Collector errors are sanitized at origin; unknown exception text is not."""
    return str(error) if isinstance(error, CollectionError) else type(error).__name__


def _conversation_open_failed(error):
    return isinstance(error, ConversationTransportError) or (
        isinstance(error, ApiRejectedError)
        and error.path == "/api/session/saveOrUpdate" and error.code == "1"
    )


async def _collect_concurrently(clients, pending, store, delay):
    """Publish completed answers and report the failure and final worker limit."""
    idle = deque(clients)
    active = {}
    completed = asyncio.Queue()
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(
        max_workers=len(clients), thread_name_prefix="krl",
    ))
    next_start = loop.time()
    index = 0
    failure = None
    interrupted = False
    publishing = True
    retry_pending = deque()
    recovered = set()
    recovering = False
    concurrency = len(clients)

    async def run_question(client, question):
        try:
            outcome = await asyncio.to_thread(_ask_worker, client, question)
        except Exception as exc:
            outcome = None, None, exc
        # Queue before completing the task; the store inserts the result into
        # source order even when other questions finish first.
        completed.put_nowait((client, question, outcome))

    while active or (
        (index < len(pending) or retry_pending)
        and failure is None and not interrupted
    ):
        try:
            # Consume every available result before deciding to dispatch more.
            while not completed.empty():
                client, question, (answer, metadata, error) = completed.get_nowait()
                active.pop(client)
                idle.append(client)
                if error is not None:
                    recovery_key = (question.id, type(error))
                    needs_serial_recovery = concurrency > 1 and (
                        _conversation_open_failed(error) or isinstance(error, StreamRetryExhaustedError)
                    )
                    if (
                        (isinstance(error, UnverifiedAnswerError) or needs_serial_recovery)
                        and client.retries > 0 and recovery_key not in recovered
                        and failure is None and not interrupted
                    ):
                        # Finish active answers before trying another conversation.
                        recovered.add(recovery_key)
                        retry_pending.append(question)
                        recovering = True
                        LOG.warning(
                            "Question %04d deferred: %s; pausing new questions "
                            "and draining %d active calls before one fresh-conversation "
                            "retry with concurrency 1",
                            question.id, _failure_reason(error), len(active),
                        )
                        continue
                    if isinstance(error, (KeyboardInterrupt, EOFError)):
                        interrupted = True
                    if failure is None:
                        failure = error
                    LOG.error(
                        "Question %04d failed: %s; stopping new questions and draining %d active calls",
                        question.id, _failure_reason(error), len(active),
                    )
                elif publishing:
                    try:
                        _save_answer(store, question, answer, metadata)
                    except Exception as exc:
                        publishing = False
                        failure = exc
                        LOG.error(
                            "Output write failed (%s); stopping publication and draining %d active calls",
                            _failure_reason(exc), len(active),
                        )

            if failure is not None or interrupted:
                if active:
                    await asyncio.wait(active.values(), return_when=asyncio.FIRST_COMPLETED)
                continue

            if recovering:
                if active:
                    await asyncio.wait(active.values(), return_when=asyncio.FIRST_COMPLETED)
                    continue
                concurrency = 1
                recovering = False
                LOG.warning(
                    "Continuing with concurrency 1; retrying %d deferred question(s) "
                    "before starting new questions",
                    len(retry_pending),
                )

            remaining = max(0.0, next_start - loop.time())
            can_dispatch = bool(idle) and len(active) < concurrency and (
                index < len(pending) or bool(retry_pending)
            )
            if can_dispatch and remaining == 0:
                client = idle.popleft()
                if retry_pending:
                    question = retry_pending.popleft()
                    LOG.info(
                        "Retrying %04d %s in a fresh conversation (concurrency 1; %s)",
                        question.id, question.title, client.profile.mode,
                    )
                else:
                    question = pending[index]
                    index += 1
                    LOG.info(
                        "[%d/%d] Asking %04d %s (%s)",
                        index, len(pending), question.id, question.title, client.profile.mode,
                    )
                active[client] = asyncio.create_task(run_question(client, question))
                next_start = loop.time() + delay
                # Let the dispatched call start and expose any immediate failure.
                await asyncio.sleep(0)
            elif active:
                timeout = remaining if can_dispatch else None
                await asyncio.wait(
                    active.values(), timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            elif index < len(pending) or retry_pending:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            # asyncio.run translates the first Ctrl+C into coordinator
            # cancellation. wait() leaves child tasks running; never cancel
            # to_thread tasks or close their HTTP sessions before they finish.
            interrupted = True
            LOG.warning(
                "Stopped starting questions; finishing %d active calls, including configured retries",
                len(active),
            )

    if interrupted:
        raise KeyboardInterrupt
    return failure, concurrency


def _collect_with_workers(client, pending, store, concurrency, delay):
    import requests

    with ExitStack() as sessions:
        clients = []
        for _ in range(min(concurrency, len(pending))):
            session = sessions.enter_context(requests.Session())
            session.headers.update(client.session.headers)
            # Copy the complete jar, including cookie domains/paths/flags. Each
            # worker has its own cookies, adapters, and connection pools.
            session.cookies = client.session.cookies.copy()
            worker = type(client)(
                session, client.request_timeout, client.answer_timeout,
                retries=client.retries, retry_delay=client.retry_delay,
                profile=client.profile,
                diagnostics_dir=client.diagnostics_dir,
                recover_saved_answer=client.recover_saved_answer,
            )
            worker._conversation_lock = client._conversation_lock
            worker._used_conversations = client._used_conversations
            clients.append(worker)
        return asyncio.run(_collect_concurrently(clients, pending, store, delay))


def build_parser(*, profile=KRL_PROFILE):
    parser = argparse.ArgumentParser(
        description=(__doc__ if profile == KRL_PROFILE else
                     f"Collect original KUKA Center {profile.mode} answers into resumable Markdown batches.\n"
                     "Login uses utils/krl/.env and accepts the site's policies automatically.\n"
                     "Enter the site's image CAPTCHA when prompted, or use --auto-captcha.\n"
                     "Start with --dry-run to inspect pending questions offline."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("After a failed collection attempt, the CLI backs off and resumes from saved answers. "
                f"It stops after {MAX_FAILED_RUNS_WITHOUT_PROGRESS} consecutive failed attempts without a save."),
    )
    parser.add_argument(
        "--questions", type=Path, default=profile.questions
    )
    parser.add_argument(
        "--output", type=Path, default=profile.output
    )
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument(
        "--diagnostics-dir", type=Path,
        help="Private unverified-response snapshots (default: .<output stem>.diagnostics beside output)",
    )
    diagnostics.add_argument(
        "--no-diagnostics", action="store_true",
        help="Disable local capture of mismatching response texts",
    )
    parser.add_argument(
        "--username",
        help="Override KUKACENTER_ACCOUT from the environment or utils/krl/.env",
    )
    parser.add_argument(
        "--auto-captcha", action="store_true",
        help="Recognize and submit the four CAPTCHA characters locally (requires ddddocr)",
    )
    parser.add_argument(
        "--start", type=int, default=profile.start,
        help=f"First sample ID, inclusive (default: {profile.start})",
    )
    parser.add_argument(
        "--end", type=int, help="Last sample ID, inclusive (default: all)"
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--max-questions", type=int, help="Limit newly collected answers for this run"
    )
    parser.add_argument(
        "--concurrency", type=int,
        help="Maximum simultaneous questions; overrides COLLECT_CONCURRENCY from the environment or utils/krl/.env (fallback: 1)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds between questions; global start spacing with concurrency > 1 (default: 2)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=180.0,
        help="Non-streaming HTTP read idle timeout in seconds (default: 180)",
    )
    parser.add_argument(
        "--answer-timeout",
        type=float,
        default=900.0,
        help=("Stream read idle timeout and answer duration limit (default: 900 seconds)"
              + ("; also separately bounds each project's upload/training preparation"
                 if profile.key == "krlu" else "")),
    )
    parser.add_argument(
        "--retries", type=int, default=3,
        help=("Additional attempts after transient HTTP errors on login GETs and collection stages, "
              "or after saved-answer mismatches (default: 3)"),
    )
    if profile.key == "kqa":
        parser.add_argument(
            "--recover-saved-answer", action="store_true",
            help="After a zero-byte stream failure, try the saved KQA answer without stream comparison",
        )
    parser.add_argument(
        "--retry-delay", type=float, default=5.0,
        help="Initial retry delay in seconds; doubles up to 60 seconds (default: 5)",
    )
    parser.add_argument(
        "--accept-login-terms",
        action="store_true",
        default=True,
        help="Accept the site's login policies (also the default)",
    )
    parser.add_argument(
        "--repair-headings",
        action="store_true",
        help="Repair saved answer heading levels offline, then exit without login/collection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate files and show pending work without network/login",
    )
    return parser


@dataclass(frozen=True)
class CollectionAttempt:
    status: int
    saved: int = 0


def main(argv=None, *, profile=KRL_PROFILE, client_factory=None, retry_failed_runs=False):
    client_factory = KukaClient if client_factory is None else client_factory
    parser = build_parser(profile=profile)
    args = parser.parse_args(argv)
    if args.dry_run and args.repair_headings:
        parser.error("--repair-headings cannot be combined with --dry-run")
    if args.concurrency is None:
        try:
            args.concurrency = 1 if args.repair_headings else load_concurrency()
        except (CollectionError, OSError) as exc:
            parser.error(str(exc))
    if args.start < 1 or (args.end is not None and args.end < args.start):
        parser.error("--start must be positive and --end must be >= --start")
    if args.batch_size < 1 or (
        args.max_questions is not None and args.max_questions < 1
    ):
        parser.error("--batch-size and --max-questions must be positive")
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if not all(math.isfinite(value) for value in (
        args.delay, args.request_timeout, args.answer_timeout, args.retry_delay
    )):
        parser.error("timeouts and delays must be finite")
    if args.delay < 0 or min(args.request_timeout, args.answer_timeout) <= 0:
        parser.error("timeouts must be positive and --delay must be nonnegative")
    if args.retries < 0 or args.retry_delay < 0:
        parser.error("--retries and --retry-delay must be nonnegative")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    failures_without_progress = 0
    try:
        while True:
            attempt = _run_once(args, profile, client_factory)
            if attempt.status != 1 or not retry_failed_runs or args.dry_run or args.repair_headings:
                return attempt.status
            failures_without_progress = 0 if attempt.saved else failures_without_progress + 1
            if failures_without_progress >= MAX_FAILED_RUNS_WITHOUT_PROGRESS:
                LOG.error(
                    "No new answers after %d collection attempts. Resolve the last error and rerun to resume",
                    failures_without_progress,
                )
                return 1
            delay = 10 * 2 ** max(0, failures_without_progress - 1)
            LOG.warning(
                "Collection attempt failed after saving %d new answer(s). "
                "Resuming from saved answers in %d seconds with concurrency %d",
                attempt.saved, delay, args.concurrency,
            )
            time.sleep(delay)
    except KeyboardInterrupt:
        LOG.info("Stopped. Completed pairs are preserved")
        return 130


def _run_once(args, profile, client_factory):
    store = None
    initial_saved = None
    try:
        args.questions = args.questions.resolve()
        args.output = args.output.resolve()
        questions = parse_questions(args.questions)
        for question in questions:
            client_factory.validate_question(question)
        if not args.repair_headings and args.start not in {question.id for question in questions}:
            raise CollectionError(f"Starting question {args.start:04d} does not exist")
        if args.questions.resolve() == args.output.resolve():
            raise CollectionError("Question source and output must be different files")
        if args.dry_run:
            store = CollectionStore(
                args.output, questions, args.batch_size,
                title=profile.title, mode=profile.mode,
            )
            pending = store.pending(args.start, args.end)
            print(
                f"Source: {len(questions)} questions; saved: {len(store.completed_ids)}; pending: {len(pending)}"
            )
            if pending:
                print(
                    f"Next: {pending[0].id:04d} {pending[0].title}; last: {pending[-1].id:04d}"
                )
            print(f"Batch size: {args.batch_size}; output: {args.output}")
            print(f"Concurrency: {args.concurrency}")
            model = profile.robot_model or "not sent (project endpoint)"
            print(f"Mode: {profile.mode}; model: {model}; stream: {profile.stream_path}")
            return CollectionAttempt(0)
        if args.repair_headings:
            with output_lock(args.output):
                CollectionStore.recover(args.output)
                store = CollectionStore(
                    args.output, questions, args.batch_size,
                    title=profile.title, mode=profile.mode,
                )
                store.ensure_heading_hierarchy()
            LOG.info("Repaired heading hierarchy for %d saved Q&A pairs", len(store.completed_ids))
            return CollectionAttempt(0)
        with output_lock(args.output):
            CollectionStore.recover(args.output)
            store = CollectionStore(
                args.output, questions, args.batch_size,
                title=profile.title, mode=profile.mode,
            )
            store.ensure_heading_hierarchy()
            store.ensure_order()
            initial_saved = len(store.completed_ids)
            pending = store.pending(args.start, args.end)
            if args.max_questions and pending:
                pending = pending[: args.max_questions]
                # Keep the initial selection across retries so saved answers still count toward the limit.
                args.end = pending[-1].id
            if not pending:
                LOG.info("All selected questions are already collected")
                return CollectionAttempt(0)
            try:
                import requests
            except ImportError as exc:
                raise CollectionError(
                    "Install requests: python -m pip install requests"
                ) from exc
            LOG.info(
                "%d already saved; collecting %d pending questions from %04d",
                initial_saved,
                len(pending),
                pending[0].id,
            )
            username, password = load_credentials(args.username)
            with requests.Session() as session:
                session.headers.update(
                    {
                        "User-Agent": f"ribosome-{profile.key}-collector/1.0",
                        "Referer": MAIN_URL + "/chat",
                    }
                )
                client = client_factory(
                    session, args.request_timeout, args.answer_timeout,
                    retries=args.retries, retry_delay=args.retry_delay,
                    profile=profile,
                    diagnostics_dir=(
                        None if args.no_diagnostics else args.diagnostics_dir
                        or args.output.with_name(f".{args.output.stem}.diagnostics")
                    ),
                    recover_saved_answer=getattr(args, "recover_saved_answer", False),
                    auto_captcha=args.auto_captcha,
                )
                client.login(
                    username,
                    password,
                    accept_terms=args.accept_login_terms,
                )
                if args.concurrency > 1:
                    failure, args.concurrency = _collect_with_workers(
                        client, pending, store, args.concurrency, args.delay,
                    )
                    if failure is not None:
                        raise failure
                else:
                    for index, question in enumerate(pending):
                        LOG.info(
                            "[%d/%d] Asking %04d %s (%s)",
                            index + 1,
                            len(pending),
                            question.id,
                            question.title,
                            profile.mode,
                        )
                        try:
                            answer, metadata = client.ask(question)
                        except UnverifiedAnswerError:
                            if client.retries == 0:
                                raise
                            LOG.warning(
                                "Retrying %04d once in a fresh conversation after failed verification "
                                "(concurrency 1)", question.id,
                            )
                            time.sleep(min(client.retry_delay, 60.0))
                            answer, metadata = client.ask(question)
                        _save_answer(store, question, answer, metadata)
                        if index + 1 < len(pending):
                            time.sleep(args.delay)
            LOG.info(
                "Finished: %d Q&A pairs saved across output batches",
                len(store.completed_ids),
            )
        return CollectionAttempt(0)
    except (KeyboardInterrupt, EOFError, LoginCancelled) as exc:
        message = str(exc) if isinstance(exc, LoginCancelled) else "Stopped"
        LOG.info("%s. Completed pairs are preserved", message)
        return CollectionAttempt(130)
    except (CollectionError, OSError, ValueError) as exc:
        message = (
            f"{type(exc).__name__} during HTTP response"
            if type(exc).__module__.startswith(("requests.", "urllib3."))
            else str(exc)
        )
        LOG.error("%s. Completed pairs are preserved", message)
        if _conversation_open_failed(exc):
            args.concurrency = 1
    except Exception as exc:
        # Avoid printing a requests exception that embeds a signed login URL.
        LOG.error(
            "%s during collection. Current answer was not published; completed pairs are preserved",
            type(exc).__name__,
        )
    saved = len(store.completed_ids) - initial_saved if initial_saved is not None else 0
    return CollectionAttempt(1, saved)


if __name__ == "__main__":
    sys.exit(main(retry_failed_runs=True))
