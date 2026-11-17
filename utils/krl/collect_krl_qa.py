#!/usr/bin/env python3
"""Collect original KUKA Center KRL answers into resumable Markdown batches.

Run ``python utils/krl/collect_krl_qa.py --dry-run`` first. Live runs read the
KUKACENTER_ACCOUT and KUKACENTER_PASSWORD values from utils/krl/.env beside this
script. Existing environment values take precedence; --username overrides the
account. Login also requires agreement consent and the site's image CAPTCHA.

Uses the same signed login, KRL model, streaming, answer check, and saved-answer
endpoints as the site's chat frontend. No CAPTCHA solving or browser dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import getpass
import hashlib
import logging
import math
import os
import sys
import tempfile
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .krl_collection_store import CollectionStore, Question, parse_questions
else:
    from krl_collection_store import CollectionStore, Question, parse_questions


ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = Path(__file__).resolve().with_name(".env")
MAIN_URL = "https://www.kukacenter.com"
CHAT_URL = "https://chat.kukacenter.com"
MODEL = "krl_question_robot"  # The frontend's KRL编程 tab.
REFUSAL = "**输出答案** 非常抱歉！由于回答中可能涉及敏感词，无法提供答案。请重新输入问答，感谢您的理解和支持！"
TERMS = (
    f"{MAIN_URL}/policy/kuka_center_legal_statement.html",
    f"{MAIN_URL}/policy/kuka_center_privacy_policy.html",
    f"{MAIN_URL}/policy/kuka_bbs_policy.html",
)
LOG = logging.getLogger("collect_krl_qa")


class CollectionError(RuntimeError):
    """A failure for which the current answer must not be published."""


class RetryableCollectionError(CollectionError):
    """A transient HTTP failure; retry only the affected collection stage."""


def http_failure(exc: Exception, context: str) -> CollectionError | None:
    """Classify requests errors without exposing URLs, credentials, or bodies."""
    from requests.exceptions import (
        ChunkedEncodingError,
        ConnectionError,
        RequestException,
        SSLError,
        Timeout,
    )

    if not isinstance(exc, RequestException):
        return None
    # iter_content wraps urllib3's read idle timeout in requests.ConnectionError.
    # Inspect exception types, never their potentially sensitive message text.
    causes = [exc]
    seen = set()
    timed_out = False
    while causes:
        cause = causes.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, Timeout) or type(cause).__name__ == "ReadTimeoutError":
            timed_out = True
        causes.extend(arg for arg in cause.args if isinstance(arg, BaseException))
        causes.extend(
            nested
            for nested in (cause.__cause__, cause.__context__)
            if nested is not None
        )
    retryable = isinstance(
        exc, (ConnectionError, Timeout, ChunkedEncodingError)
    ) and not isinstance(exc, SSLError)
    error_type = RetryableCollectionError if retryable else CollectionError
    reason = "HTTP read/connect timeout" if timed_out else type(exc).__name__
    return error_type(f"{context}: {reason}")


def load_credentials(username=None):
    """Read login settings without exporting unrelated .env values or logging secrets."""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise CollectionError(
            "Install python-dotenv: python -m pip install python-dotenv"
        ) from exc
    # Credentials can contain literal ${...}; do not interpolate password text.
    settings = dotenv_values(ENV_FILE, interpolate=False)
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
    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type or "html" in content_type:
        raise CollectionError(
            "Chat returned an error/login document instead of an answer"
        )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    parts = []
    deadline = time.monotonic() + timeout
    try:
        for chunk in response.iter_content(chunk_size=4096):
            if time.monotonic() > deadline:
                raise CollectionError("Answer exceeded --answer-timeout; nothing saved")
            if chunk:
                parts.append(decoder.decode(chunk))
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
        retries=3, retry_delay=5.0,
    ):
        self.session = session
        self.request_timeout = request_timeout
        self.answer_timeout = answer_timeout
        self.retries = retries
        self.retry_delay = retry_delay

    def retry(self, operation, action):
        """Bound retries at stage boundaries, with a sanitized reason and backoff."""
        delay = min(self.retry_delay, 60.0)
        for attempt in range(self.retries + 1):
            try:
                return action()
            except RetryableCollectionError as exc:
                if attempt == self.retries:
                    raise CollectionError(
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
            failure = http_failure(exc, f"{method} {urlsplit(url).path}")
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

    def api(self, path, payload=None, *, main_site=False):
        # The main frontend's HTTP wrapper prefixes service paths such as
        # /api-b/... with /api. The unprefixed sign URL returns HTTP 463.
        # Chat has its own /api/... routes and does not use that proxy prefix.
        host = MAIN_URL + "/api" if main_site else CHAT_URL
        if main_site:
            response = self.request("GET", host + path, params=payload)
        else:
            response = self.request("POST", host + path, json=payload or {})
        with response:
            try:
                data = response.json()
            except ValueError as exc:
                raise CollectionError(
                    f"{path}: expected JSON; login may have expired"
                ) from exc
        if not isinstance(data, dict) or str(data.get("code")) not in (
            "0",
            "200",
            "ok",
        ):
            # Do not echo server bodies, signed URLs, or credentials to logs.
            raise CollectionError(
                f"{path}: API rejected request; stopping without saving"
            )
        return data.get("data")

    def login(self, username, password=None, *, accept_terms=False):
        with self.request("GET", MAIN_URL + "/chat") as response:
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
                    raise CollectionError("Login cancelled before sending credentials")
            if password is None:
                password = getpass.getpass("KUKA Center password: ")
            if not password:
                raise CollectionError("A password is required")
            captcha_uuid = str(uuid.uuid4())
            with tempfile.TemporaryDirectory(prefix="krl-login-") as temporary:
                captcha_path = Path(temporary) / "captcha.png"
                with self.request(
                    "GET",
                    "https://sso.kukacenter.com/cas/captcha",
                    params={"uuid": captcha_uuid},
                    headers={"Referer": login_url},
                ) as response:
                    if (
                        not response.headers.get("Content-Type", "")
                        .lower()
                        .startswith("image/")
                    ):
                        raise CollectionError(
                            "Login CAPTCHA endpoint did not return an image"
                        )
                    captcha_path.write_bytes(response.content)
                    captcha_path.chmod(0o600)
                print(
                    f"Open this image and read the site's CAPTCHA: {captcha_path}",
                    flush=True,
                )
                captcha = input("CAPTCHA (4 characters, or blank to cancel): ").strip()
                if len(captcha) != 4:
                    raise CollectionError(
                        "Login cancelled: a four-character CAPTCHA is required"
                    )
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
        signature = self.api(
            "/api-b/program_assistant/api/sign",
            {"nonceStr": str(uuid.uuid4())},
            main_site=True,
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

    def generate(self, question: Question) -> tuple[str, dict]:
        """Start a fresh conversation; a failed stream is never reused."""
        session = self.api("/api/session/saveOrUpdate", {"sessionTitle": "新建对话"})
        if not isinstance(session, dict) or not session.get("sessionId"):
            raise CollectionError("Chat did not create a session")
        session_id = session["sessionId"]
        question_uuid = str(uuid.uuid4())
        identifiers = {
            "sessionId": session_id,
            "uuid": question_uuid,
            "requestId": session_id,
        }
        payload = dict(
            identifiers,
            question=question.prompt,
            robotModel=MODEL,
            repeatFlag=False,
            networkSearchFlag=False,
        )
        with self.request(
            "POST",
            CHAT_URL + "/api/efs/stream",
            json=payload,
            stream=True,
            # Reasoning can be silent longer than ordinary API calls. Do not
            # cut off a 900-second answer at the 180-second API idle timeout.
            timeout=(20, self.answer_timeout),
        ) as response:
            streamed = read_stream(response, self.answer_timeout)
        return streamed, identifiers

    def ask(self, question: Question) -> tuple[str, dict]:
        streamed, identifiers = self.retry(
            f"Question {question.id:04d} generation (new conversation each attempt; "
            "the previous request may still complete remotely)",
            lambda: self.generate(question),
        )
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
            "mode": "KRL编程",
            "robot_model": MODEL,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "session_id": identifiers["sessionId"],
            "question_uuid": identifiers["uuid"],
            "answer_check_passed": checked["status"],
        }
        if not checked["status"]:
            # Exactly the replacement displayed by the frontend's answer check.
            return REFUSAL, metadata
        saved = self.retry(
            f"Question {question.id:04d} saved answer fetch",
            lambda: self.api(
                "/api/krl/selectQuestionInfo",
                dict(identifiers, question=question.prompt, content=question.prompt),
            ),
        )
        if not isinstance(saved, dict) or not isinstance(
            saved.get("answerContent"), str
        ):
            raise CollectionError(
                "Chat did not return a persisted Markdown answer; nothing saved"
            )
        answer = saved["answerContent"]
        if not answer.strip() or answer.strip("\r\n") != streamed.strip("\r\n"):
            raise CollectionError(
                "Stream and saved answer differ; refusing a potentially incomplete answer"
            )
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


async def _collect_concurrently(clients, pending, store, delay):
    """Bound whole ask calls; only this coordinator publishes completed answers."""
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

    async def run_question(client, question):
        try:
            outcome = await asyncio.to_thread(_ask_worker, client, question)
        except Exception as exc:
            outcome = None, None, exc
        # Queue before completing the task, preserving observed completion order.
        completed.put_nowait((client, question, outcome))

    while active or (index < len(pending) and failure is None and not interrupted):
        try:
            # Consume every available result before deciding to dispatch more.
            while not completed.empty():
                client, question, (answer, metadata, error) = completed.get_nowait()
                active.pop(client)
                idle.append(client)
                if error is not None:
                    if isinstance(error, (KeyboardInterrupt, EOFError)):
                        interrupted = True
                    if failure is None:
                        failure = error
                    LOG.error(
                        "Question %04d failed; stopping new questions and draining %d active calls",
                        question.id, len(active),
                    )
                elif publishing:
                    try:
                        _save_answer(store, question, answer, metadata)
                    except Exception as exc:
                        publishing = False
                        failure = exc
                        LOG.error(
                            "Output write failed; stopping publication and draining %d active calls",
                            len(active),
                        )

            if failure is not None or interrupted:
                if active:
                    await asyncio.wait(active.values(), return_when=asyncio.FIRST_COMPLETED)
                continue

            remaining = max(0.0, next_start - loop.time())
            if idle and index < len(pending) and remaining == 0:
                client = idle.popleft()
                question = pending[index]
                index += 1
                LOG.info(
                    "[%d/%d] Asking %04d %s (KRL编程)",
                    index, len(pending), question.id, question.title,
                )
                active[client] = asyncio.create_task(run_question(client, question))
                next_start = loop.time() + delay
                # Let the dispatched call start and expose any immediate failure.
                await asyncio.sleep(0)
            elif active:
                timeout = remaining if idle and index < len(pending) else None
                await asyncio.wait(
                    active.values(), timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            elif index < len(pending):
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
    if failure is not None:
        raise failure


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
            clients.append(KukaClient(
                session, client.request_timeout, client.answer_timeout,
                retries=client.retries, retry_delay=client.retry_delay,
            ))
        asyncio.run(_collect_concurrently(clients, pending, store, delay))


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--questions", type=Path, default=ROOT / "assets/KRL/krl_questions_1000.md"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "assets/KRL/krl_qa_collected.md"
    )
    parser.add_argument(
        "--username",
        help="Override KUKACENTER_ACCOUT from the environment or utils/krl/.env",
    )
    parser.add_argument(
        "--start", type=int, default=19, help="First sample ID, inclusive (default: 19)"
    )
    parser.add_argument(
        "--end", type=int, help="Last sample ID, inclusive (default: all)"
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--max-questions", type=int, help="Limit newly collected answers for this run"
    )
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="Maximum simultaneous questions; 1 keeps sequential collection (default: 1)",
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
        help="Stream read idle timeout and answer duration limit (default: 900 seconds)",
    )
    parser.add_argument(
        "--retries", type=int, default=3,
        help="Additional attempts per stage after transient HTTP errors (default: 3)",
    )
    parser.add_argument(
        "--retry-delay", type=float, default=5.0,
        help="Initial retry delay in seconds; doubles up to 60 seconds (default: 5)",
    )
    parser.add_argument(
        "--accept-login-terms",
        action="store_true",
        help="Explicitly accept the site's login policies without an interactive prompt",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate files and show pending work without network/login",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
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
    try:
        args.questions = args.questions.resolve()
        args.output = args.output.resolve()
        questions = parse_questions(args.questions)
        if args.start not in {question.id for question in questions}:
            raise CollectionError(f"Starting question {args.start:04d} does not exist")
        if args.questions.resolve() == args.output.resolve():
            raise CollectionError("Question source and output must be different files")
        if args.dry_run:
            store = CollectionStore(args.output, questions, args.batch_size)
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
            return 0
        # Lazy import keeps --help and offline planning dependency-free.
        try:
            import requests
        except ImportError as exc:
            raise CollectionError(
                "Install requests: python -m pip install requests"
            ) from exc
        with output_lock(args.output):
            store = CollectionStore(args.output, questions, args.batch_size)
            pending = store.pending(args.start, args.end)
            if args.max_questions:
                pending = pending[: args.max_questions]
            if not pending:
                LOG.info("All selected questions are already collected")
                return 0
            LOG.info(
                "%d already saved; collecting %d pending questions from %04d",
                len(store.completed_ids),
                len(pending),
                pending[0].id,
            )
            username, password = load_credentials(args.username)
            with requests.Session() as session:
                session.headers.update(
                    {
                        "User-Agent": "ribosome-krl-collector/1.0",
                        "Referer": MAIN_URL + "/chat",
                    }
                )
                client = KukaClient(
                    session, args.request_timeout, args.answer_timeout,
                    retries=args.retries, retry_delay=args.retry_delay,
                )
                client.login(
                    username,
                    password,
                    accept_terms=args.accept_login_terms,
                )
                if args.concurrency > 1:
                    _collect_with_workers(
                        client, pending, store, args.concurrency, args.delay,
                    )
                else:
                    for index, question in enumerate(pending):
                        LOG.info(
                            "[%d/%d] Asking %04d %s (KRL编程)",
                            index + 1,
                            len(pending),
                            question.id,
                            question.title,
                        )
                        answer, metadata = client.ask(question)
                        _save_answer(store, question, answer, metadata)
                        if index + 1 < len(pending):
                            time.sleep(args.delay)
            LOG.info(
                "Finished: %d Q&A pairs saved across output batches",
                len(store.completed_ids),
            )
        return 0
    except (KeyboardInterrupt, EOFError):
        LOG.error("Stopped. Completed pairs are preserved; rerun to resume")
        return 130
    except (CollectionError, OSError, ValueError) as exc:
        message = (
            f"{type(exc).__name__} during HTTP response"
            if type(exc).__module__.startswith(("requests.", "urllib3."))
            else str(exc)
        )
        LOG.error("%s. Completed pairs are preserved; rerun to resume", message)
        return 1
    except Exception as exc:
        # Avoid printing a requests exception that embeds a signed login URL.
        LOG.error(
            "%s during collection. Current answer was not published; completed pairs are preserved",
            type(exc).__name__,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
